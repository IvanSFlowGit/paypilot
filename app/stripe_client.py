# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
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
from urllib.parse import urlsplit

from app.safety import (
    PAYMENT_UPDATE_URL,
    allowed_link_hosts,
    host_of,
)

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
    """Where Stripe sends the customer back to after they update their card.

    Validated before it is handed to Stripe. This module re-checks the URL
    Stripe returns, so accepting an unchecked one on the way out was
    inconsistent: a misconfigured or injected ``javascript:`` return URL fires
    immediately after the customer types their card number, which is the
    highest-trust moment in the whole flow.
    """
    configured = (os.getenv("PAYPILOT_PORTAL_RETURN_URL") or "").strip()
    if not configured:
        return PAYMENT_UPDATE_URL
    # _is_valid_recovery_link, not find_foreign_urls: this used to ask the
    # NEGATIVE question ("did the extractor find anything foreign?") while its
    # sibling asked the positive one, so the two disagreed about the same
    # string. Silence from the extractor is absence of evidence, not evidence
    # of absence - an apostrophe truncated the match before the real host in
    # "https://billing.stripe.com'@evil.test/", a sentence containing no URL at
    # all passed, and so did plain http. One check, one answer.
    if not _is_valid_recovery_link(configured):
        _log.error(
            "PAYPILOT_PORTAL_RETURN_URL is not an https URL on an allowed host "
            "(%s); using the default return URL instead",
            ", ".join(allowed_link_hosts()),
        )
        return PAYMENT_UPDATE_URL
    return configured


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


def _is_valid_recovery_link(candidate: str) -> bool:
    """Whether ``candidate`` is a link we are willing to put in an email.

    A POSITIVE check. The previous version asked "did the guard find anything
    foreign?" and treated silence as approval, which is absence of evidence,
    not evidence of absence. Anything the URL pattern did not recognise passed
    straight through and became the sanctioned link: a bare IP with a path, an
    address-shaped string, even a sentence containing no URL at all
    ("Call 0800-555-0199 to update your card") was returned verbatim and
    rendered as the email's link line.

    Now it must be a single https URL whose host is on the allowlist, or the
    configured fallback exactly.
    """
    text = (candidate or "").strip()
    if not text or text != PAYMENT_UPDATE_URL and any(c.isspace() for c in text):
        return text == PAYMENT_UPDATE_URL
    if text == PAYMENT_UPDATE_URL:
        return True
    try:
        parts = urlsplit(text.replace("\\", "/"))
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    # host_of, not parts.hostname: it normalises the shapes browsers and Python
    # disagree about. Parsing here independently is what let
    # "https://evil.test\\@billing.stripe.com/" through as allowlisted.
    host = host_of(text)
    return bool(host) and host in set(allowed_link_hosts())


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
        if not _is_valid_recovery_link(candidate):
            _log.warning(
                "discarding recovery link: not an https URL on an allowed host "
                "(%s)", ", ".join(allowed_link_hosts()),
            )
            continue
        return candidate
    return PAYMENT_UPDATE_URL
