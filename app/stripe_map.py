# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
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
from datetime import UTC, datetime

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

#: Stripe ``advice_code`` values that override the decline-code mapping. The
#: advice code is the issuer's instruction for THIS charge, so it outranks any
#: static reading of the decline code. ``do_not_try_again`` means the card will
#: not work however the retry is timed: Stripe stops retrying until the
#: customer adds a new payment method, so PayPilot must ask for one rather than
#: promise a retry. See docs.stripe.com/declines ("advice codes").
_ADVICE_OVERRIDES: dict[str, str] = {
    "do_not_try_again": "issuer_do_not_retry",
    # The card data on file is wrong (number, expiry or CVC). Retrying the same
    # details fails the same way; the customer has to correct them.
    "confirm_card_data": "card_details_invalid",
}

#: Payment method types Stripe DOES retry by default. Per Stripe's automatic
#: collection docs, it "doesn't automatically retry failed non-card payment
#: methods and Direct Debit payments except for ACH Direct Debit" unless the
#: account joined the preview. Everything outside this set is only retried by
#: Stripe when the invoice says so via ``next_payment_attempt``.
_STRIPE_RETRIED_METHOD_TYPES = frozenset({"card", "us_bank_account"})


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
    # ALL v1 values, not just the last. Stripe sends several while an endpoint
    # secret is being rotated, and every official library accepts if ANY match.
    # Collapsing them into a dict made ordering decide, silently dropping real
    # invoice.payment_failed and invoice.paid events mid-rotation.
    timestamp = None
    signatures: list[str] = []
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key == "t" and timestamp is None:
            timestamp = value.strip()
        elif key == "v1":
            signatures.append(value.strip())
    if not timestamp or not signatures:
        return False
    signed_payload = timestamp.encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    # Bytes: a non-ASCII Stripe-Signature header otherwise raises TypeError
    # and turns a rejected forgery into an unhandled 500 with no audit event.
    if not any(
        hmac.compare_digest(expected.encode("utf-8"), s.encode("utf-8"))
        for s in signatures
    ):
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


def _extract_advice_code(obj: dict) -> str:
    """Pull Stripe's per-charge ``advice_code`` from wherever it lives on the event.

    Stripe puts it on the PaymentIntent's ``last_payment_error`` and on the
    charge's ``outcome``. Empty string when absent, which is common: most
    declines carry no advice and the decline code then decides.
    """
    payment_intent = obj.get("payment_intent")
    if isinstance(payment_intent, dict):
        advice = (payment_intent.get("last_payment_error") or {}).get("advice_code")
        if advice:
            return advice
    charge = obj.get("charge")
    if isinstance(charge, dict):
        advice = (charge.get("outcome") or {}).get("advice_code")
        if advice:
            return advice
    return ""


def payment_method_type(obj: dict) -> str:
    """The failed payment's method type (``card``, ``sepa_debit``...), or ``""``."""
    payment_intent = obj.get("payment_intent")
    if isinstance(payment_intent, dict):
        err = payment_intent.get("last_payment_error") or {}
        method = err.get("payment_method")
        if isinstance(method, dict) and method.get("type"):
            return str(method["type"])
    charge = obj.get("charge")
    if isinstance(charge, dict):
        details = charge.get("payment_method_details") or {}
        if details.get("type"):
            return str(details["type"])
    return ""


def resolve_failure_code(obj: dict) -> str:
    """The PayPilot failure code for a Stripe invoice object.

    ``advice_code`` first, because it is the issuer's instruction for this
    exact charge. Then a direct debit or other non-card method that Stripe is
    not retrying (no ``next_payment_attempt``): nobody will re-attempt it, so
    it needs the customer rather than a schedule. Then the decline-code map,
    and ``generic_decline`` last.
    """
    override = _ADVICE_OVERRIDES.get(_extract_advice_code(obj))
    if override:
        return override
    method = payment_method_type(obj)
    if (
        method
        and method not in _STRIPE_RETRIED_METHOD_TYPES
        and stripe_next_attempt(obj) is None
    ):
        return "direct_debit_not_retried"
    return _STRIPE_CODE_MAP.get(_extract_decline_code(obj), "generic_decline")


def stripe_next_attempt(obj: dict) -> str | None:
    """Stripe's own next retry instant for this invoice, as ISO 8601 UTC.

    ``next_payment_attempt`` is a unix timestamp Stripe sets from the account's
    Smart Retries or custom retry settings. When it is present, Stripe owns the
    timing and PayPilot reports it instead of inventing a schedule. It is null
    when Stripe will not retry, and on ``invoice.payment_failed`` for accounts
    using Billing automations (Stripe sets it on ``invoice.updated`` there).
    """
    value = obj.get("next_payment_attempt")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")


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

    failure_code = resolve_failure_code(obj)

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
        # degrades to "Hello there" about "your plan".
        "customer_name": obj.get("customer_name") or None,
        "customer_email": obj.get("customer_email") or None,
        "plan": plan_name_from_invoice(obj),
        "advice_code": _extract_advice_code(obj) or None,
        "payment_method_type": payment_method_type(obj) or None,
        "stripe_next_payment_attempt": stripe_next_attempt(obj),
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
        "failure_code": resolve_failure_code(obj),
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
