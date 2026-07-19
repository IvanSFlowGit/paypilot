"""Speak Stripe: verify and translate ``invoice.payment_failed`` webhooks.

Two pure pieces so the webhook route stays thin and everything is unit-testable
with no network:

* :func:`verify_stripe_signature` - constant-time check of the ``Stripe-Signature``
  header against a webhook signing secret, following Stripe's scheme
  (``HMAC-SHA256`` over ``"{timestamp}.{payload}"``, compared to the ``v1`` value).
* :func:`stripe_event_to_internal` - map a Stripe event object to the flat event
  dict PayPilot's graph consumes, normalising Stripe decline codes to the three
  PayPilot failure codes and cents to a decimal amount.

Stripe doesn't put the decline reason on the invoice object itself, so we look in
the usual places (the expanded PaymentIntent's ``last_payment_error``, the
invoice's ``last_finalization_error``, the charge, or an explicit metadata hint)
and fall back to ``generic_decline``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time

from app.money import minor_to_major

# Stripe decline / error codes -> PayPilot failure codes. Anything unmapped is
# treated as a generic decline (the safe, recoverable default).
_STRIPE_CODE_MAP: dict[str, str] = {
    "expired_card": "card_expired",
    "insufficient_funds": "insufficient_funds",
    "card_declined": "generic_decline",
    "generic_decline": "generic_decline",
    "do_not_honor": "generic_decline",
    "transaction_not_allowed": "generic_decline",
    "processing_error": "generic_decline",
    "try_again_later": "generic_decline",
}


def verify_stripe_signature(
    payload: bytes, sig_header: str, secret: str, tolerance: int = 300
) -> bool:
    """Return True if ``sig_header`` is a valid Stripe signature for ``payload``.

    Mirrors Stripe's ``constructEvent`` check: parse ``t`` and ``v1`` out of the
    header, recompute ``HMAC-SHA256(secret, "{t}.{payload}")`` and compare in
    constant time. A ``tolerance`` of 0 skips the timestamp freshness check
    (useful in tests); otherwise the event must be within ``tolerance`` seconds.
    """
    if not sig_header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    timestamp, v1 = parts.get("t"), parts.get("v1")
    if not timestamp or not v1:
        return False
    signed_payload = timestamp.encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        return False
    if tolerance:
        try:
            if abs(time.time() - int(timestamp)) > tolerance:
                return False
        except ValueError:
            return False
    return True


def _extract_decline_code(obj: dict) -> str:
    """Pull the raw Stripe decline/error code from wherever it lives on the event."""
    payment_intent = obj.get("payment_intent")
    if isinstance(payment_intent, dict):
        err = payment_intent.get("last_payment_error") or {}
        code = err.get("decline_code") or err.get("code")
        if code:
            return code
    fin = obj.get("last_finalization_error") or {}
    if fin.get("decline_code") or fin.get("code"):
        return fin.get("decline_code") or fin.get("code")
    charge = obj.get("charge")
    if isinstance(charge, dict):
        code = charge.get("failure_code") or (charge.get("outcome") or {}).get("reason")
        if code:
            return code
    # Explicit hint, handy for wiring a real Stripe test event to a demo customer.
    return (obj.get("metadata") or {}).get("failure_code") or ""


# Invoice line descriptions read "1 × Pro Plan (at EUR 49.00 / month)". The
# quantity prefix and the price suffix are noise in an email, so both are cut.
_LINE_QTY_RE = re.compile(r"^\s*\d+\s*[x×]\s*", re.IGNORECASE)


def plan_name_from_invoice(obj: dict) -> str | None:
    """Best-effort human plan name for dunning copy.

    Stripe does not expose a plain "plan name" on the invoice in current API
    versions: the price nickname is usually unset and the product is a bare id,
    so the line description is the only place a human-readable name reliably
    appears. Returns None rather than a placeholder when nothing usable is
    found, so the caller decides what to say.
    """
    lines = (obj.get("lines") or {}).get("data") or []
    if not lines:
        return None
    description = (lines[0] or {}).get("description") or ""
    cleaned = _LINE_QTY_RE.sub("", description)
    cleaned = cleaned.split(" (at ")[0].strip()
    return cleaned or None


def stripe_event_to_internal(event: dict) -> dict:
    """Translate a Stripe ``invoice.payment_failed`` event into a PayPilot event.

    Reads the invoice object's amount (cents -> decimal), currency, and attempt
    count, resolves the customer id (a ``metadata.paypilot_customer_id`` hint wins
    so demo fixtures resolve, else the Stripe ``customer`` id), and normalises the
    decline reason to a PayPilot failure code.
    """
    obj = (event.get("data") or {}).get("object") or {}

    raw_code = _extract_decline_code(obj)
    failure_code = _STRIPE_CODE_MAP.get(raw_code, "generic_decline")

    metadata = obj.get("metadata") or {}
    customer_id = metadata.get("paypilot_customer_id") or obj.get("customer") or ""

    amount_minor = obj.get("amount_due")
    if amount_minor is None:
        amount_minor = obj.get("amount_paid", 0)
    # Per-currency exponent: JPY is whole yen, so a flat /100 under-reports a
    # Japanese invoice by 100x in the diagnosis, the email and the impact block.
    amount = minor_to_major(amount_minor or 0, obj.get("currency") or "usd")

    return {
        "customer_id": str(customer_id),
        "amount": amount,
        "currency": obj.get("currency") or "usd",
        "failure_code": failure_code,
        "attempt": int(obj.get("attempt_count") or 1),
        # Stripe's own view of who this is and what they pay for. A real
        # deployment has no local customer file, so without these the copy
        # degrades to "Hi there" about "your plan".
        "customer_name": obj.get("customer_name") or None,
        "customer_email": obj.get("customer_email") or None,
        "plan": plan_name_from_invoice(obj),
    }


def _subscription_id(obj: dict) -> str | None:
    """Subscription id off an invoice, whether expanded or a bare string."""
    sub = obj.get("subscription")
    if isinstance(sub, dict):
        return sub.get("id")
    return sub or None


def stripe_event_to_failure(event: dict) -> dict:
    """Map ``invoice.payment_failed`` to the row the store persists.

    Distinct from :func:`stripe_event_to_internal`, which feeds the graph a
    display-friendly decimal amount and a possibly-local customer id. What gets
    stored has to be exact and joinable instead:

    * ``amount_minor`` stays in Stripe's integer minor units - the decimal is a
      presentation concern, and rounding it into storage is how currency totals
      drift,
    * the Stripe customer and subscription ids are kept verbatim so a later
      ``customer.subscription.deleted`` can find these invoices.
    """
    obj = (event.get("data") or {}).get("object") or {}
    amount_minor = obj.get("amount_due")
    if amount_minor is None:
        amount_minor = obj.get("amount_paid", 0)

    metadata = obj.get("metadata") or {}
    return {
        "hosted_invoice_url": obj.get("hosted_invoice_url") or None,
        "customer_email": obj.get("customer_email") or None,
        "invoice_id": obj.get("id") or "",
        "customer_id": str(
            metadata.get("paypilot_customer_id") or obj.get("customer") or ""
        ),
        "stripe_customer_id": obj.get("customer") or None,
        "subscription_id": _subscription_id(obj),
        "amount_minor": int(amount_minor or 0),
        "currency": (obj.get("currency") or "usd").lower(),
        "failure_code": _STRIPE_CODE_MAP.get(_extract_decline_code(obj), "generic_decline"),
        "attempt_count": int(obj.get("attempt_count") or 1),
    }


def stripe_closing_event(event: dict) -> dict:
    """Map a loop-closing Stripe event to what the state machine needs.

    Covers ``invoice.paid`` / ``invoice.payment_succeeded`` (an invoice paid,
    which may or may not be one we were dunning) and
    ``customer.subscription.deleted`` (the customer gave up).

    ``amount_paid`` is deliberately preferred over ``amount_due`` here: a
    partially paid invoice must record what actually arrived, not what was
    billed, or the recovered total overstates the money in the bank.
    """
    obj = (event.get("data") or {}).get("object") or {}
    event_type = event.get("type") or ""

    if event_type == "customer.subscription.deleted":
        return {
            "kind": "churn",
            "invoice_id": None,
            "stripe_customer_id": obj.get("customer") or None,
            "subscription_id": obj.get("id") or None,
            "amount_minor": None,
            "currency": None,
        }

    return {
        "kind": "recovery",
        "invoice_id": obj.get("id") or "",
        "stripe_customer_id": obj.get("customer") or None,
        "subscription_id": _subscription_id(obj),
        "amount_minor": int(obj.get("amount_paid") or 0),
        "currency": (obj.get("currency") or "usd").lower(),
    }
