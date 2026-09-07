# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
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
4. **The text is checked at the transport.** Every send, dunning or operator
   alert, goes through :func:`_post_to_resend`, and that is where the output
   guard runs: a body carrying a link nobody sanctioned or anything
   secret-shaped is refused. One enforcement point, so the two paths cannot
   drift apart. Callers may be stricter (``app/loop.py`` pins the exact link it
   minted); none of them can be laxer, and none can skip it.

Every attempt is written to the store whether it sent, was suppressed, or
failed, because a dunning tool that cannot say what it sent to whom is not
auditable. Transient provider failures are retried with backoff; a refusal
(4xx) is not, because retrying a rejected address just annoys Resend.

Uses ``httpx`` against Resend's REST API rather than adding the SDK: httpx is
already a pinned dependency for the test client.
"""

from __future__ import annotations

import json
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

def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Config that refuses to crash at import or silently disable sending.

    A non-numeric value used to raise at import; a value of 0 turned every
    send into a recorded failure with no provider call.
    """
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


_MAX_ATTEMPTS = _int_env("PAYPILOT_SEND_MAX_ATTEMPTS", 3)
try:
    _BACKOFF_SECONDS = max(0.0, float(os.getenv("PAYPILOT_SEND_BACKOFF_SECONDS", "0.5")))
except (TypeError, ValueError):
    _BACKOFF_SECONDS = 0.5


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
    return os.getenv("PAYPILOT_FROM_EMAIL") or "PayPilot <billing@streamflow.solutions>"


#: Returned as the error when the output guard refuses a message. A refusal,
#: not a failure: nothing was attempted, and retrying would refuse again.
ERROR_FAILED_OUTPUT_GUARD = "failed_output_guard"


def _post_to_resend(payload: dict, api_key: str) -> tuple[bool, str | None, str | None]:
    """One Resend call, output-guarded. Returns ``(sent, provider_message_id, error)``.

    The output guard runs HERE, on the exact subject and body about to be
    posted, because this is the single function every send in this process
    passes through. It used to run per caller, and the two callers disagreed:
    the alert path checked its body, the dunning path did not, so anything
    calling :func:`send_dunning_email` other than ``app/loop.py`` inherited no
    output check at all. A guard a caller can forget is a guard that will be
    forgotten.

    The floor is the host allowlist, and it takes no arguments on purpose: a
    caller cannot pass in extra permitted links and so cannot weaken it.
    ``app/loop.py`` is *stricter* - it pins the exact link it just minted before
    it calls, which only a caller can do, because only it knows which URL
    belongs in that message. That belt stays where it is. The floor here has to
    pass a legitimate dunning body carrying that minted Stripe portal link, or
    the guard silently stops real mail, which is worse than the hole it closes.

    Retries only what is worth retrying: network errors and 5xx. A 4xx is the
    provider telling us the request is wrong, and repeating it will not fix it.
    A guard refusal is not retried either, and never reaches the provider.
    """
    from app.safety import message_violations

    violations = message_violations(
        f"{payload.get('subject') or ''}\n{payload.get('text') or ''}"
    )
    if violations:
        # Loud, and without the offending text: the body may carry the very
        # link or token the guard just caught, and an audit record is not a
        # place to reprint it.
        audit_security_event(
            event="outbound_message_failed_output_guard",
            detail=f"blocked outbound message: {len(violations)} violation(s)",
        )
        return False, None, ERROR_FAILED_OUTPUT_GUARD

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = "no attempt made"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = httpx.post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            last_error = f"transport_error: {type(exc).__name__}"
        else:
            if response.status_code < 300:
                # Parsing sits INSIDE the try. A proxy or CDN interstitial
                # returning 200 with HTML would otherwise raise past the
                # caller, losing the record of a message that may well have
                # been delivered - an unauditable send is exactly the failure
                # this module exists to prevent, and it also hides the touch
                # from the sequence cap.
                try:
                    body = response.json() if response.content else {}
                    provider_id = body.get("id")
                except (ValueError, AttributeError):
                    provider_id = None
                return True, provider_id, None
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
        # json.dumps, like every other log site: invoice_id comes unvalidated
        # from the webhook, and raw interpolation let one call emit a second
        # physical line forging an audit record on the same stream.
        _log.info(json.dumps({"event": "dry_run_send", "invoice_id": invoice_id}))
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
    if error == ERROR_FAILED_OUTPUT_GUARD:
        # Suppressed, not failed: nobody rejected this, we refused to send it.
        # Recording it as a failure would put it in the same bucket as a Resend
        # outage and invite a retry of a message that must never go out.
        return _record(STATUS_SUPPRESSED, error=error)
    return _record(STATUS_FAILED, error=error)


def send_operator_alert(*, to: str, subject: str, body: str, api_key: str | None = None,
                        sender_address: str | None = None) -> dict:
    """Send one alert to the operator, through the same transport as everything else.

    Exists so that scripts which need to mail the owner (copy detection, and
    anything after it) do not each grow their own httpx call. A second sender is
    how a guard gets skipped: ``scripts/canary.py`` had one, and it posted a body
    assembled from GitHub code-search results - repo names, paths and URLs chosen
    by whoever published the matching repo - with no output check at all.

    The output guard is not applied here. It runs inside :func:`_post_to_resend`,
    which both this and :func:`send_dunning_email` go through, so both paths get
    the identical check on the identical text and a future caller of either
    cannot forget it. This function used to check the body itself, which made it
    the *only* guarded path: the dunning path had no output check of its own.

    Two of :func:`send_dunning_email`'s guards deliberately do NOT apply, because
    they protect a different thing. ``PAYPILOT_SEND_EMAIL`` and
    ``PAYPILOT_ALLOWED_RECIPIENTS`` exist because a dunning recipient arrives in
    a Stripe webhook and may be a real person who should never hear from us. An
    operator alert goes to one address the operator wrote into the environment
    themselves (``CANARY_ALERT_EMAIL``), so the recipient is not
    attacker-influenced. Gating it on the dunning switch would silence copy
    detection on every install that has live dunning off, which is most of them.

    The recipient allowlist is the weaker of the two exemptions, and skipping it
    is a decision rather than an oversight: an operator may reasonably read
    ``PAYPILOT_ALLOWED_RECIPIENTS`` as a hard cap on every address this process
    may ever mail, and this function will mail out of an install whose dunning
    switch is off. It is skipped because the two lists would then have to agree -
    an operator adding an alert address to the *dunning* allowlist is a confusing
    edit, and forgetting it fails silently, which is how a copy detection gets
    lost. The blast radius is bounded elsewhere: one operator-set address, one
    fixed subject, and a body that still has to clear the transport guard.

    Returns ``{"status", "provider_message_id", "error"}``. Nothing is recorded
    in the messages table: that ledger is per invoice and an alert has none.
    """
    key = (api_key if api_key is not None else os.getenv("RESEND_API_KEY") or "").strip()
    address = (to or "").strip()
    if not (key and address):
        return {"status": STATUS_SUPPRESSED, "provider_message_id": None,
                "error": "alert_not_configured"}

    sent, provider_message_id, error = _post_to_resend(
        {"from": sender_address or sender(), "to": [address],
         "subject": subject, "text": body},
        key,
    )
    if sent:
        return {"status": STATUS_SENT, "provider_message_id": provider_message_id,
                "error": None}
    if error == ERROR_FAILED_OUTPUT_GUARD:
        # A refusal, not a provider failure: either a caller is composing from
        # untrusted data, or something upstream is trying to use the alert as a
        # delivery channel. Same status the dunning path records for the same
        # reason, so the two do not disagree about what happened.
        return {"status": STATUS_SUPPRESSED, "provider_message_id": None, "error": error}
    return {"status": STATUS_FAILED, "provider_message_id": None, "error": error}


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
