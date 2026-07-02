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
import time

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

    amount_cents = obj.get("amount_due")
    if amount_cents is None:
        amount_cents = obj.get("amount_paid", 0)
    amount = round((amount_cents or 0) / 100.0, 2)

    return {
        "customer_id": str(customer_id),
        "amount": amount,
        "currency": obj.get("currency") or "usd",
        "failure_code": failure_code,
        "attempt": int(obj.get("attempt_count") or 1),
    }
