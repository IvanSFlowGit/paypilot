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

If neither is available the module falls back to the tenant's card-update link
(:func:`app.safety.payment_update_url`) rather than raising. A link we could not
mint is a worse email, not a failed recovery, and an exception here would take
down the whole webhook for a cosmetic reason.

THE KEY IS PASSED ON EVERY CALL. ``api_key=`` is given to each SDK request and
the SDK's module-level ``stripe.api_key`` is never assigned. A process-global key
shared by threads is a cross-tenant credential race: two tenants' requests in
one threadpool would sign with whichever key was assigned last.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from app.safety import (
    allowed_link_hosts,
    host_of,
    payment_update_url,
)

_log = logging.getLogger("paypilot.stripe")


def api_key(config=None) -> str:
    """The configured Stripe secret key for this deployment, or '' in the keyless demo."""
    from app.tenant_config import current_config

    return (config or current_config()).stripe_api_key


def get_stripe(config=None):
    """Return the ``stripe`` SDK module, or None when there is no key to call with.

    The module is returned WITHOUT a key assigned to it. Callers pass the key
    with :func:`api_key` on each request. The single seam for outbound Stripe
    calls, so tests monkeypatch this rather than the SDK's internals.
    """
    if not api_key(config):
        return None
    import stripe  # imported lazily so the keyless demo never needs the SDK

    return stripe


def _return_url(config=None) -> str:
    """Where Stripe sends the customer back to after they update their card.

    Validated before it is handed to Stripe. This module re-checks the URL
    Stripe returns, so accepting an unchecked one on the way out was
    inconsistent: a misconfigured or injected ``javascript:`` return URL fires
    immediately after the customer types their card number, which is the
    highest-trust moment in the whole flow.
    """
    from app.tenant_config import current_config

    cfg = config or current_config()
    configured = cfg.portal_return_url
    if not configured:
        return payment_update_url(cfg)
    # _is_valid_recovery_link, not find_foreign_urls: this used to ask the
    # NEGATIVE question ("did the extractor find anything foreign?") while its
    # sibling asked the positive one, so the two disagreed about the same
    # string. Silence from the extractor is absence of evidence, not evidence
    # of absence - an apostrophe truncated the match before the real host in
    # "https://billing.stripe.com'@evil.test/", a sentence containing no URL at
    # all passed, and so did plain http. One check, one answer.
    if not _is_valid_recovery_link(configured, cfg):
        _log.error(
            "portal return URL is not an https URL on an allowed host "
            "(%s); using the default return URL instead",
            ", ".join(allowed_link_hosts(cfg)),
        )
        return payment_update_url(cfg)
    return configured


def create_portal_session(customer_id: str, config=None) -> str | None:
    """Mint a Customer Portal session URL for ``customer_id``.

    Returns None (never raises) when there is no key, no customer, or Stripe
    refuses - most often because no portal configuration exists on the account,
    which is a setup step rather than a bug.
    """
    if not customer_id:
        return None
    stripe = get_stripe(config)
    if stripe is None:
        return None
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=_return_url(config), api_key=api_key(config)
        )
        return getattr(session, "url", None) or session["url"]
    except Exception as exc:  # noqa: BLE001 - any SDK error degrades to fallback
        _log.warning(
            "billing portal session unavailable (%s); falling back to invoice link",
            type(exc).__name__,
        )
        return None


def _is_valid_recovery_link(candidate: str, config) -> bool:
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
    fallback = payment_update_url(config)
    text = (candidate or "").strip()
    if not text or text != fallback and any(c.isspace() for c in text):
        return text == fallback
    if text == fallback:
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
    return bool(host) and host in set(allowed_link_hosts(config))


def recovery_link(
    *, stripe_customer_id: str | None = None, hosted_invoice_url: str | None = None,
    config,
) -> str:
    """The one URL a dunning email may contain, resolved best-first.

    Portal session, then the invoice's hosted page, then the static fallback.
    Whatever comes back is re-checked against the link allowlist before it is
    returned: a URL from Stripe still has to be one we are willing to send, and
    silently trusting an upstream response is how an allowlist gets bypassed.
    """
    for candidate in (
        create_portal_session(stripe_customer_id or "", config),
        hosted_invoice_url,
    ):
        if not candidate:
            continue
        if not _is_valid_recovery_link(candidate, config):
            _log.warning(
                "discarding recovery link: not an https URL on an allowed host "
                "(%s)", ", ".join(allowed_link_hosts(config)),
            )
            continue
        return candidate
    return payment_update_url(config)
