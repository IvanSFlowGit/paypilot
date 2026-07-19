"""Outbound Stripe calls: the recovery link a dunning email points at.

PayPilot never collects card details. The single action a dunning email asks
for is "update your card", and that has to happen on a Stripe-hosted page, so
this module's whole job is producing a URL we are allowed to send.

Two sources, in order of preference:

* a **Customer Portal session** (``billing_portal.Session.create``), which lets
  the customer replace the card on file and is the right destination for an
  expired card, and
* the invoice's **``hosted_invoice_url``**, Stripe's own pay-this-invoice page,
  used when no portal configuration exists.

If neither is available the module falls back to
:data:`app.safety.PAYMENT_UPDATE_URL` rather than raising. A link we could not
mint is a worse email, not a failed recovery, and an exception here would take
down the whole webhook for a cosmetic reason.

The API key is read from the environment per call, never stored in code, and
the restricted-key scopes a client needs are documented in the README.
"""

from __future__ import annotations

import logging
import os

from app.safety import PAYMENT_UPDATE_URL, allowed_link_hosts, find_foreign_urls

_log = logging.getLogger("paypilot.stripe")


def api_key() -> str:
    """The configured Stripe secret key, or empty string in the keyless demo."""
    return (os.getenv("STRIPE_API_KEY") or os.getenv("STRIPE_SECRET_KEY") or "").strip()


def get_stripe():
    """Return the configured ``stripe`` module, or None when no key is set.

    The single seam for outbound Stripe calls, so tests monkeypatch this rather
    than the SDK's internals - matching ``app.nodes.get_llm`` and
    ``app.store.get_store``.
    """
    key = api_key()
    if not key:
        return None
    import stripe  # imported lazily so the keyless demo never needs the SDK

    stripe.api_key = key
    return stripe


def _return_url() -> str:
    """Where Stripe sends the customer back to after they update their card."""
    return os.getenv("PAYPILOT_PORTAL_RETURN_URL") or PAYMENT_UPDATE_URL


def create_portal_session(customer_id: str) -> str | None:
    """Mint a Customer Portal session URL for ``customer_id``.

    Returns None (never raises) when there is no key, no customer, or Stripe
    refuses - most often because no portal configuration exists on the account,
    which is a setup step rather than a bug.
    """
    stripe = get_stripe()
    if stripe is None or not customer_id:
        return None
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=_return_url()
        )
        return getattr(session, "url", None) or session["url"]
    except Exception as exc:  # noqa: BLE001 - any SDK error degrades to fallback
        _log.warning(
            "billing portal session unavailable (%s); falling back to invoice link",
            type(exc).__name__,
        )
        return None


def recovery_link(
    *, stripe_customer_id: str | None = None, hosted_invoice_url: str | None = None
) -> str:
    """The one URL a dunning email may contain, resolved best-first.

    Portal session, then the invoice's hosted page, then the static fallback.
    Whatever comes back is re-checked against the link allowlist before it is
    returned: a URL from Stripe still has to be one we are willing to send, and
    silently trusting an upstream response is how an allowlist gets bypassed.
    """
    for candidate in (
        create_portal_session(stripe_customer_id or ""),
        hosted_invoice_url,
    ):
        if not candidate:
            continue
        if find_foreign_urls(candidate, PAYMENT_UPDATE_URL):
            _log.warning(
                "discarding recovery link with a non-allowlisted host; "
                "allowed hosts are %s",
                ", ".join(allowed_link_hosts()),
            )
            continue
        return candidate
    return PAYMENT_UPDATE_URL
