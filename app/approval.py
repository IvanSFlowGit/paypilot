# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Human approval of outbound dunning mail.

Nothing here decides what to say or who to say it to. The graph and the rules
table do that. This module decides only WHEN a drafted message may leave: after
a named person has read the exact text and said yes.

Modes (``PAYPILOT_APPROVAL_MODE``):

* ``email`` (the default) - the approver gets an email carrying the full draft
  and one link. The link opens a page showing the exact text with Send and Skip
  buttons. Works in the Gmail app, Proton, or any client; no mailbox access.
* ``gmail`` - the draft is created in the approver's own Gmail Drafts folder and
  the approver presses Send there. See :mod:`app.gmail_drafts`.
* ``imap`` - the same, over IMAP: Proton Mail through Bridge, or any IMAP
  mailbox. See :mod:`app.imap_drafts`.
* ``off`` - unattended sending, as before. Must be written out. An unset or
  misspelled value falls back to ``email``, never to ``off``.

Design rules, each earned by a way this goes wrong:

**A link is not a click.** Mail scanners and link previewers fetch every URL in
a message. A GET that sent mail would send it the moment the approval email was
delivered. So the link only SHOWS the draft; sending needs a POST from the page.

**Approve what was read.** The body is frozen when the request is opened and
hashed. The send uses that stored text, never a regeneration.

**One decision, once.** The claim is one atomic UPDATE (pending -> approved,
unexpired). A double tap, a replayed POST and two browser tabs all lose after
the first.

**Re-check at the moment of sending.** Hours pass between the request and the
tap. The customer may have paid, so the invoice state, the touch cap and the
cooldown are checked again, and a paid invoice is never mailed.

**Only the hash of the token is stored.** The raw token exists in the email and
nowhere else.

**Fail closed.** No approver, no public base URL, or an undeliverable request
means the message is suppressed with a reason, not sent unreviewed.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from app import tenant_config
from app.audit import audit_security_event
from app.mailer import (
    STATUS_DRY_RUN,
    STATUS_SENT,
    STATUS_SUPPRESSED,
    is_allowed_recipient,
    send_dunning_email,
    send_operator_alert,
    sending_enabled,
)

_log = logging.getLogger("paypilot.approval")

MODE_OFF = "off"
MODE_EMAIL = "email"
MODE_GMAIL = "gmail"
MODE_IMAP = "imap"
_MODES = frozenset({MODE_OFF, MODE_EMAIL, MODE_GMAIL, MODE_IMAP})

# Approval statuses (the ``approvals.status`` column).
A_PENDING = "pending"
A_APPROVED = "approved"
A_SENT = "sent"
A_SKIPPED = "skipped"
A_EXPIRED = "expired"
A_FAILED = "failed"
A_BLOCKED = "blocked"

DEFAULT_TTL_HOURS = tenant_config.DEFAULT_TTL_HOURS


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def approval_mode(config=None) -> str:
    """The tenant's mode. Unknown values resolve to ``email`` in the config parser."""
    from app.tenant_config import current_config

    return (config or current_config()).approval_mode


def approver_emails(config=None) -> tuple[str, ...]:
    from app.tenant_config import current_config

    return (config or current_config()).approver_emails


def public_base_url() -> str:
    """Where approval links point. https only, except for localhost."""
    raw = (os.getenv("PAYPILOT_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme == "https" and parts.hostname:
        return raw
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1"):
        return raw
    return ""


def ttl_hours(config=None) -> int:
    from app.tenant_config import current_config

    return (config or current_config()).approval_ttl_hours


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Opening a request
# ---------------------------------------------------------------------------


def _record_refusal(store, invoice_id: str, error: str) -> dict:
    store.record_message(invoice_id=invoice_id, status=STATUS_SUPPRESSED, error=error)
    return {"status": STATUS_SUPPRESSED, "error": error}


def request_approval(
    *,
    invoice_id: str,
    recipient: str,
    subject: str,
    body: str,
    link: str,
    store,
    attempt: int = 1,
    config=None,
) -> dict:
    """Open an approval request for one composed message.

    Returns a dict with ``status``: ``pending_approval`` when a person now has
    something to decide, otherwise the same suppressed or dry-run result a
    direct send would have produced. Nothing reaches ``recipient`` from here.
    """
    config = config or tenant_config.config_of(store)
    mode = approval_mode(config)

    # The switches that exist for the plain path still apply, and they apply
    # FIRST: with sending off, or a recipient not cleared, there is nothing for
    # a human to approve, and asking them would train them to approve noise.
    if not sending_enabled(config):
        return send_dunning_email(
            invoice_id=invoice_id, to=recipient, subject=subject, body=body,
            store=store, attempt=attempt, config=config,
        )
    if not is_allowed_recipient(recipient, config):
        return send_dunning_email(
            invoice_id=invoice_id, to=recipient, subject=subject, body=body,
            store=store, attempt=attempt, config=config,
        )

    # A person who skipped this invoice has answered. Stripe retries emit a new
    # failure event each time; asking again every time is how an approver
    # learns to ignore the requests.
    for earlier in store.approvals_for(invoice_id):
        if earlier["status"] == A_SKIPPED:
            return _record_refusal(store, invoice_id, "approver_skipped")

    store.expire_approvals()

    if mode == MODE_GMAIL:
        from app import gmail_drafts

        return gmail_drafts.request_draft(
            invoice_id=invoice_id, recipient=recipient, subject=subject,
            body=body, store=store, config=config,
        )

    if mode == MODE_IMAP:
        from app import imap_drafts

        return imap_drafts.request_draft(
            invoice_id=invoice_id, recipient=recipient, subject=subject,
            body=body, store=store, config=config,
        )

    approvers = approver_emails(config)
    if not approvers:
        return _record_refusal(store, invoice_id, "no_approver_configured")
    base = public_base_url()
    if not base:
        return _record_refusal(store, invoice_id, "no_public_base_url")

    token = secrets.token_hex(24)  # hex: cannot collide with the secret-shape guard
    expires = _iso(_now_dt() + timedelta(hours=ttl_hours(config)))
    approval_id = store.create_approval(
        invoice_id=invoice_id, channel="email", recipient=recipient,
        subject=subject, body=body, token_hash=hash_token(token), expires_at=expires,
    )
    if approval_id is None:
        # Already waiting on a person. Do not email them a second copy.
        existing = [a for a in store.approvals_for(invoice_id) if a["status"] == A_PENDING]
        return {
            "status": "pending_approval",
            "approval_id": existing[0]["id"] if existing else None,
            "duplicate": True,
        }

    url = f"{base}/approve/{approval_id}?t={token}"
    notice = compose_request_email(
        recipient=recipient, subject=subject, body=body, invoice_id=invoice_id,
        expires_at=expires, url=url,
    )
    delivered = 0
    for address in approvers:
        result = send_operator_alert(
            config=config,
            to=address,
            subject=f"Approve before it sends: payment recovery email ({invoice_id})",
            body=notice,
            allow_exact_urls=(url, link),
        )
        if result["status"] == STATUS_SENT:
            delivered += 1
    if not delivered:
        store.resolve_approval(approval_id, A_FAILED, by="system",
                               error="approval_request_undeliverable")
        audit_security_event(
            event="approval_request_undeliverable",
            detail=f"invoice {invoice_id}: no approver could be reached",
            severity="error",
        )
        return _record_refusal(store, invoice_id, "approval_request_undeliverable")

    return {"status": "pending_approval", "approval_id": approval_id, "expires_at": expires}


def compose_request_email(*, recipient, subject, body, invoice_id, expires_at, url) -> str:
    return (
        "PayPilot drafted a payment recovery email. Nothing has been sent, and "
        "nothing will be sent until you approve it.\n\n"
        f"To: {recipient}\n"
        f"Subject: {subject}\n"
        f"Invoice: {invoice_id}\n"
        f"Decide by: {expires_at} UTC\n\n"
        "--- the exact message ---\n"
        f"{body}\n"
        "--- end of message ---\n\n"
        "Open this page to send it or skip it:\n"
        f"{url}\n\n"
        "Opening the page sends nothing. You press Send on the page.\n"
    )


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def _token_ok(approval: dict | None, token: str) -> bool:
    if not approval or not token:
        return False
    return hmac.compare_digest(approval["token_hash"], hash_token(token))


def view(approval_id: int, token: str, store) -> dict | None:
    """The approval as the approver may see it, or None for a bad token.

    A wrong id and a wrong token are indistinguishable on purpose.
    """
    approval = store.get_approval(approval_id)
    if not _token_ok(approval, token):
        return None
    return approval


def _recheck_before_sending(approval: dict, store) -> str | None:
    """Reasons not to send, evaluated NOW. None means clear."""
    from app.loop import _cooldown_remaining, max_touches
    from app.store import TERMINAL_STATES

    invoice_id = approval["invoice_id"]
    cfg = tenant_config.config_of(store)
    current = store.get_failure(invoice_id)
    if current and current["state"] in TERMINAL_STATES:
        return f"invoice_already_{current['state']}"
    if store.sent_message_count(invoice_id) >= max_touches(cfg):
        return "max_touches_reached"
    row = current or {}
    if _cooldown_remaining(
        store, invoice_id=invoice_id,
        stripe_customer_id=row.get("stripe_customer_id") or row.get("customer_id"),
        config=cfg,
    ) > 0:
        return "cooldown_active"
    expected = hashlib.sha256(approval["body"].encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected, approval["body_sha256"]):
        return "body_changed_since_request"
    return None


def decide(approval_id: int, token: str, action: str, store, *, by: str = "link") -> dict:
    """Apply a person's decision. ``action`` is ``approve`` or ``skip``.

    Returns ``{"ok", "outcome", ...}``. ``outcome`` is one of ``sent``,
    ``skipped``, ``blocked``, ``failed``, ``dry_run``, ``expired``,
    ``already_decided``, ``invalid``.
    """
    approval = store.get_approval(approval_id)
    if not _token_ok(approval, token):
        audit_security_event(
            event="approval_token_rejected",
            detail=f"approval {approval_id}: token missing or wrong",
        )
        return {"ok": False, "outcome": "invalid"}

    if action == "skip":
        if store.resolve_approval(approval_id, A_SKIPPED, by=by, only_from=(A_PENDING,)):
            store.record_message(
                invoice_id=approval["invoice_id"], status=STATUS_SUPPRESSED,
                error="skipped_by_approver",
            )
            return {"ok": True, "outcome": "skipped"}
        return {"ok": False, "outcome": "already_decided", "status": approval["status"]}

    if action != "approve":
        return {"ok": False, "outcome": "invalid"}

    if not store.claim_approval(approval_id, by=by):
        fresh = store.get_approval(approval_id)
        if fresh and fresh["status"] == A_PENDING:
            # Still pending but not claimable: past its deadline.
            store.expire_approvals()
            return {"ok": False, "outcome": "expired"}
        return {"ok": False, "outcome": "already_decided",
                "status": fresh["status"] if fresh else None}

    # The claim moved the row to APPROVED. From here every path must leave it in a terminal
    # state. An exception (a tenant that cannot be resolved, a store error, a transport
    # failure) used to leave it APPROVED forever, never sent and never retried.
    try:
        return _decide_after_claim(approval_id, approval, store)
    except BaseException as exc:
        store.resolve_approval(
            approval_id, A_FAILED, by="system", error=f"exception:{type(exc).__name__}",
            only_from=(A_APPROVED,),
        )
        raise


def _decide_after_claim(approval_id: int, approval: dict, store) -> dict:
    blocker = _recheck_before_sending(approval, store)
    if blocker:
        store.resolve_approval(approval_id, A_BLOCKED, by="system", error=blocker,
                               only_from=(A_APPROVED,))
        store.record_message(invoice_id=approval["invoice_id"],
                             status=STATUS_SUPPRESSED, error=blocker)
        return {"ok": False, "outcome": "blocked", "reason": blocker}

    result = send_dunning_email(
        invoice_id=approval["invoice_id"], to=approval["recipient"],
        subject=approval["subject"], body=approval["body"], store=store,
        config=tenant_config.config_of(store),
    )
    if result["status"] == STATUS_SENT:
        from app.store import STATE_MESSAGED

        # The mail has left. Record that FIRST: if the state transition below fails, the approval
        # must still read as sent, never as failed (the outer handler only fails APPROVED rows).
        store.resolve_approval(approval_id, A_SENT, only_from=(A_APPROVED,))
        store.transition(approval["invoice_id"], STATE_MESSAGED,
                         reason="dunning_email_sent_after_approval")
        return {"ok": True, "outcome": "sent"}
    if result["status"] == STATUS_DRY_RUN:
        store.resolve_approval(approval_id, A_FAILED, by="system", error="dry_run",
                               only_from=(A_APPROVED,))
        return {"ok": False, "outcome": "dry_run"}
    store.resolve_approval(
        approval_id, A_FAILED, by="system",
        error=str(result.get("error") or result["status"]), only_from=(A_APPROVED,),
    )
    return {"ok": False, "outcome": "failed", "reason": result.get("error")}


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def sweep(store) -> dict:
    """Expire overdue requests and reconcile Gmail drafts. Safe to call often."""
    expired = store.expire_approvals()
    for row in expired:
        store.record_message(invoice_id=row["invoice_id"], status=STATUS_SUPPRESSED,
                             error="approval_expired")
        audit_security_event(
            event="approval_expired",
            detail=f"invoice {row['invoice_id']}: nobody decided in time; nothing was sent",
        )
    reconciled = {}
    mode = approval_mode(tenant_config.config_of(store))
    if mode == MODE_GMAIL:
        from app import gmail_drafts

        reconciled = gmail_drafts.reconcile(store)
    elif mode == MODE_IMAP:
        from app import imap_drafts

        reconciled = imap_drafts.reconcile(store)
    return {"expired": len(expired), "gmail": reconciled if mode == MODE_GMAIL else {},
            "imap": reconciled if mode == MODE_IMAP else {}}


_reconciler_started = False
_reconciler_lock = threading.Lock()


def start_background_sweep(interval_seconds: int = 60) -> bool:
    """Start the sweep thread once, only in gmail mode. True if it started.

    Email mode needs no polling: the decision arrives as an HTTP request. The
    mailbox modes have to ask the mailbox whether the draft was sent, so something must
    keep asking.
    """
    global _reconciler_started
    from app.store import tenancy_mode

    if tenancy_mode() == "multi":
        # No ambient tenant, so no single mode to read. A normal multi-mode boot is quiet here; the
        # per-tenant sweep is M4/M5 work and is not started by this function.
        return False
    if approval_mode() not in (MODE_GMAIL, MODE_IMAP):
        return False
    with _reconciler_lock:
        if _reconciler_started:
            return False
        _reconciler_started = True

    def _loop() -> None:
        import time

        from app.store import get_store

        while True:
            try:
                sweep(get_store())
            except Exception as exc:  # noqa: BLE001 - a sweep must never die silently
                _log.error(json.dumps({"event": "sweep_failed", "error": type(exc).__name__}))
            time.sleep(interval_seconds)

    threading.Thread(target=_loop, name="paypilot-approval-sweep", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Approve payment recovery email</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#14181f}}
main{{max-width:640px;margin:0 auto;padding:20px 16px 48px}}
h1{{font-size:1.25rem;margin:0 0 12px}}
dl{{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0 0 16px}}
dt{{color:#5b6472}}dd{{margin:0;word-break:break-word}}
pre{{white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #d6dae1;
border-radius:8px;padding:14px;font:inherit}}
.row{{display:flex;gap:12px;margin-top:16px}}
button{{flex:1;min-height:48px;font:inherit;font-weight:600;border-radius:8px;border:0;cursor:pointer}}
.send{{background:#0b5fff;color:#fff}}.skip{{background:#e8ebf0;color:#14181f}}
.note{{color:#5b6472;font-size:.9rem}}
</style></head><body><main>
{content}
</main></body></html>
"""


def render_page(approval: dict | None, token: str, *, notice: str | None = None) -> str:
    """The approval page. Every interpolated value is escaped."""
    esc = html.escape
    if approval is None:
        content = "<h1>This link is not valid</h1><p class=\"note\">Nothing was sent.</p>"
        return _PAGE.format(content=content)
    head = f"<h1>{esc(notice)}</h1>" if notice else "<h1>Approve this email?</h1>"
    details = (
        "<dl>"
        f"<dt>To</dt><dd>{esc(approval['recipient'])}</dd>"
        f"<dt>Subject</dt><dd>{esc(approval['subject'])}</dd>"
        f"<dt>Invoice</dt><dd>{esc(approval['invoice_id'])}</dd>"
        f"<dt>Status</dt><dd>{esc(approval['status'])}</dd>"
        f"<dt>Decide by</dt><dd>{esc(approval['expires_at'])} UTC</dd>"
        "</dl>"
        f"<pre>{esc(approval['body'])}</pre>"
    )
    actions = ""
    if approval["status"] == A_PENDING:
        t = esc(token, quote=True)
        actions = (
            f'<div class="row"><form method="post" action="/approve/{int(approval["id"])}" style="display:contents">'
            f'<input type="hidden" name="t" value="{t}">'
            '<button class="send" name="action" value="approve" type="submit">Send this email</button>'
            '<button class="skip" name="action" value="skip" type="submit">Skip, send nothing</button>'
            "</form></div>"
            '<p class="note">This exact text is what will be sent. Opening this page sent nothing.</p>'
        )
    return _PAGE.format(content=head + details + actions)


OUTCOME_NOTICE = {
    "sent": "Sent",
    "skipped": "Skipped. Nothing was sent",
    "blocked": "Not sent: the situation changed since the request",
    "failed": "Not sent: the mail provider refused it",
    "dry_run": "Not sent: live sending is switched off",
    "expired": "This request expired. Nothing was sent",
    "already_decided": "Already decided. Nothing more was done",
    "invalid": "This link is not valid",
}
