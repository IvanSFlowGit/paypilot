"""Email delivery for dunning messages, via Resend.

This is the module where PayPilot stops being a simulation: everything else
produces text, and this sends it to a human. So the guards run *before* the
provider call, not after, and they fail closed:

1. **Sending is off unless explicitly enabled.** ``PAYPILOT_SEND_EMAIL=1``.
   Default is a dry run that records exactly what would have gone out.
2. **Recipients must be on an allowlist.** ``PAYPILOT_ALLOWED_RECIPIENTS``.
   A Stripe *test-mode* customer can carry any address, including a real one
   belonging to a real person, and "it was only test mode" is no comfort to
   someone who received a dunning email about a subscription they do not have.
   An empty allowlist blocks every live send rather than permitting all of
   them - the safe reading of an unset variable.
3. **No key, no send.** A missing ``RESEND_API_KEY`` is a blocked send, not a
   crash.

Every attempt is written to the store whether it sent, was suppressed, or
failed, because a dunning tool that cannot say what it sent to whom is not
auditable. Transient provider failures are retried with backoff; a refusal
(4xx) is not, because retrying a rejected address just annoys Resend.

Uses ``httpx`` against Resend's REST API rather than adding the SDK: httpx is
already a pinned dependency for the test client.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from app.audit import audit_security_event

_log = logging.getLogger("paypilot.mailer")

RESEND_ENDPOINT = "https://api.resend.com/emails"

# Delivery attempt outcomes recorded in the messages table.
STATUS_SENT = "sent"
STATUS_DRY_RUN = "dry_run"
STATUS_SUPPRESSED = "suppressed"
STATUS_FAILED = "failed"
STATUS_BOUNCED = "bounced"

_MAX_ATTEMPTS = int(os.getenv("PAYPILOT_SEND_MAX_ATTEMPTS", "3"))
_BACKOFF_SECONDS = float(os.getenv("PAYPILOT_SEND_BACKOFF_SECONDS", "0.5"))


def sending_enabled() -> bool:
    """True only when live sending has been switched on deliberately."""
    return (os.getenv("PAYPILOT_SEND_EMAIL") or "").strip() in ("1", "true", "yes")


def allowed_recipients() -> frozenset[str]:
    """Addresses cleared to receive real mail (comma separated, lowercased)."""
    raw = (os.getenv("PAYPILOT_ALLOWED_RECIPIENTS") or "").strip()
    return frozenset(a.strip().lower() for a in raw.split(",") if a.strip())


def is_allowed_recipient(address: str) -> bool:
    """Whether ``address`` may receive a live send.

    An unset allowlist means nobody, not everybody. The wildcard has to be
    written out (``PAYPILOT_ALLOWED_RECIPIENTS=*``) so that turning off the
    safety catch is a visible, deliberate act in the deployment config.
    """
    allowed = allowed_recipients()
    if not allowed:
        return False
    if "*" in allowed:
        return True
    return (address or "").strip().lower() in allowed


def sender() -> str:
    """The From address. Must be a Resend-verified domain to actually deliver."""
    return os.getenv("PAYPILOT_FROM_EMAIL") or "PayPilot <billing@paypilot.dev>"


def _post_to_resend(payload: dict, api_key: str) -> tuple[bool, str | None, str | None]:
    """One Resend call. Returns ``(sent, provider_message_id, error)``.

    Retries only what is worth retrying: network errors and 5xx. A 4xx is the
    provider telling us the request is wrong, and repeating it will not fix it.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = "no attempt made"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = httpx.post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            last_error = f"transport_error: {type(exc).__name__}"
        else:
            if response.status_code < 300:
                body = response.json() if response.content else {}
                return True, body.get("id"), None
            # Never log the response body: it echoes the recipient address.
            last_error = f"http_{response.status_code}"
            if response.status_code < 500:
                return False, None, last_error

        if attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_SECONDS * attempt)

    return False, None, last_error


def send_dunning_email(
    *,
    invoice_id: str,
    to: str,
    subject: str,
    body: str,
    store=None,
    attempt: int = 1,
) -> dict:
    """Send (or deliberately not send) one dunning email, recording the attempt.

    Returns ``{"status", "provider_message_id", "error", "message_id"}``. The
    caller decides what a status means for the state machine; only
    :data:`STATUS_SENT` represents a message that actually reached a person.
    """
    from app.store import get_store

    store = store or get_store()
    api_key = (os.getenv("RESEND_API_KEY") or "").strip()

    def _record(status: str, provider_message_id=None, error=None) -> dict:
        message_id = store.record_message(
            invoice_id=invoice_id,
            channel="email",
            status=status,
            provider_message_id=provider_message_id,
            attempt=attempt,
            error=error,
        )
        return {
            "status": status,
            "provider_message_id": provider_message_id,
            "error": error,
            "message_id": message_id,
        }

    if not sending_enabled():
        _log.info("dry run: would send dunning email for invoice %s", invoice_id)
        return _record(STATUS_DRY_RUN)

    if not is_allowed_recipient(to):
        # Loud, because a blocked send in production is either a misconfigured
        # allowlist or a near miss where a real person almost got mailed.
        audit_security_event(
            event="recipient_not_allowlisted",
            detail=(
                f"blocked dunning send for invoice {invoice_id}: recipient is not "
                "in PAYPILOT_ALLOWED_RECIPIENTS"
            ),
        )
        return _record(STATUS_SUPPRESSED, error="recipient_not_allowlisted")

    if not api_key:
        return _record(STATUS_SUPPRESSED, error="missing_resend_api_key")

    sent, provider_message_id, error = _post_to_resend(
        {"from": sender(), "to": [to], "subject": subject, "text": body}, api_key
    )
    if sent:
        return _record(STATUS_SENT, provider_message_id=provider_message_id)
    return _record(STATUS_FAILED, error=error)


def mark_bounced(provider_message_id: str, store=None, error: str = "bounced") -> bool:
    """Mark a previously sent message as bounced.

    Driven by a Resend bounce webhook. A bounced address must stop counting as
    a delivered touch, or the sequence keeps mailing a dead mailbox and the
    dashboard reports touches nobody received.
    """
    from app.store import get_store

    store = store or get_store()
    row = store.find_message_by_provider_id(provider_message_id)
    if row is None:
        return False
    store.update_message(row["id"], status=STATUS_BOUNCED, error=error)
    return True
