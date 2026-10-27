# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The mailbox-draft approval flow, independent of which mailbox it is.

PayPilot puts the message in the approver's own Drafts folder and never sends
it. The person reads it in their mail app, presses Send, and PayPilot notices.
Whether the mailbox is spoken to over the Gmail API or over IMAP changes only
the four calls a client makes:

    create_draft(raw) -> external id        make the draft
    draft_exists(external id) -> bool       is it still in Drafts
    delete_draft(external id)               remove it (expiry)
    sent_since(to=, after_epoch=) -> list   what was sent to this address since

Each returned sent message is ``{"id", "subject", "to", "marker"}``. ``marker``
is the value of the ``X-PayPilot-Approval`` header, ``""`` if the mailbox did
not keep it.

Why a header and a search rather than an id: Google's documentation says that
on send "the draft is automatically deleted and a new message with an updated
ID is created", so a draft cannot be followed to its sent message by id. IMAP
has the same property in practice, since a client sends by composing a new
message and deleting the draft.

A vanished draft is NOT assumed to be a refusal. A just-sent message may not be
searchable yet, and a draft can be removed for a dozen reasons. It is held as
pending for a grace period, and only then counted as skipped. That ordering is
the whole reason a skip is trustworthy: a skip suppresses every later request
for the invoice.
"""

from __future__ import annotations

import base64
import imaplib
import os
from datetime import datetime, timedelta
from email.message import EmailMessage

import httpx

from app.audit import audit_security_event
from app.mailer import STATUS_SENT, STATUS_SUPPRESSED

HEADER = "X-PayPilot-Approval"
from app.tenant_config import config_of  # noqa: E402


class MailboxError(RuntimeError):
    """A mailbox call failed. The message carries a status, never a body."""


#: Everything a mailbox transport can raise that means "could not talk to it".
#: OSError covers sockets, timeouts and TLS failures.
TRANSPORT_ERRORS = (MailboxError, httpx.HTTPError, OSError, imaplib.IMAP4.error)


def grace_minutes(config=None) -> int:
    """The tenant's Gmail grace window, parsed once in app/tenant_config.py."""
    from app.tenant_config import current_config

    return (config or current_config()).gmail_grace_minutes


def build_raw(*, recipient: str, subject: str, body: str, marker: str) -> str:
    """The RFC 5322 message, base64url encoded (the form the Gmail API takes)."""
    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject
    msg[HEADER] = marker
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def marker_for(approval_id: int, body_sha256: str) -> str:
    return f"{approval_id}:{body_sha256[:16]}"


def open_draft(
    *, channel: str, client, invoice_id: str, recipient: str, subject: str,
    body: str, store, where: str,
) -> dict:
    """Open the approval row, create the draft, tell the approver. Fails closed.

    The row is opened FIRST because its id is part of the draft's marker, and a
    draft must never exist without a row that can account for it.
    """
    from app import approval as ap
    from app.mailer import send_operator_alert

    expires = ap._iso(ap._now_dt() + timedelta(hours=ap.ttl_hours(config_of(store))))
    approval_id = store.create_approval(
        invoice_id=invoice_id, channel=channel, recipient=recipient, subject=subject,
        body=body, token_hash=ap.hash_token(os.urandom(16).hex()), expires_at=expires,
    )
    if approval_id is None:
        existing = [a for a in store.approvals_for(invoice_id) if a["status"] == ap.A_PENDING]
        return {"status": "pending_approval",
                "approval_id": existing[0]["id"] if existing else None, "duplicate": True}

    row = store.get_approval(approval_id)
    try:
        external_id = client.create_draft(build_raw(
            recipient=recipient, subject=subject, body=body,
            marker=marker_for(approval_id, row["body_sha256"])))
    except TRANSPORT_ERRORS as exc:
        store.resolve_approval(approval_id, ap.A_FAILED, by="system",
                               error=f"draft_create_failed:{type(exc).__name__}")
        audit_security_event(event=f"{channel}_draft_create_failed",
                             detail=f"invoice {invoice_id}: {exc}", severity="error")
        return ap._record_refusal(store, invoice_id, f"{channel}_draft_create_failed")
    except BaseException as exc:
        # Anything else (a bug, a closed client, a cancelled task) must not leave a PENDING row with
        # no draft behind it: reconcile would later read the missing draft as "skipped by the
        # approver" and refuse the invoice for good. Resolve it FAILED and let the error surface.
        store.resolve_approval(approval_id, ap.A_FAILED, by="system",
                               error=f"draft_create_failed:{type(exc).__name__}",
                               only_from=(ap.A_PENDING,))
        raise

    store.set_approval_external_id(approval_id, external_id)

    # Best effort and link-free, so it needs no guard exception. A failure here
    # does not undo the draft.
    for address in ap.approver_emails(config_of(store)):
        send_operator_alert(
            config=config_of(store),
            to=address,
            subject=f"A payment recovery email is waiting in your {where} drafts ({invoice_id})",
            body=("PayPilot put a draft in your Drafts folder. Nothing has been sent. "
                  "Open the draft, read it, and press Send, or delete it to skip.\n"
                  f"To: {recipient}\nSubject: {subject}\n"),
        )
    return {"status": "pending_approval", "approval_id": approval_id, "expires_at": expires}


def reconcile(store, client, channel: str) -> dict:
    """Settle every pending approval on this channel: sent, skipped, expired, waiting."""
    from app import approval as ap
    from app.store import STATE_MESSAGED, TERMINAL_STATES

    summary = {"checked": 0, "sent": 0, "skipped": 0, "waiting": 0, "errors": 0}
    now = ap._now_dt()

    for a in store.list_approvals(status=ap.A_PENDING, limit=500):
        row = store.get_approval(a["id"])
        if row["channel"] != channel:
            continue
        summary["checked"] += 1
        try:
            if row["expires_at"] <= ap._iso(now):
                if row["external_id"]:
                    client.delete_draft(row["external_id"])  # a stale draft must not be sendable
                store.resolve_approval(row["id"], ap.A_EXPIRED, by="system")
                store.record_message(invoice_id=row["invoice_id"], status=STATUS_SUPPRESSED,
                                     error="approval_expired")
                continue
            if row["external_id"] and client.draft_exists(row["external_id"]):
                store.mark_approval_gone(row["id"], False)
                summary["waiting"] += 1
                continue

            gone_since = store.mark_approval_gone(row["id"], True)
            created = datetime.fromisoformat(row["created_at"])
            sent = client.sent_since(to=row["recipient"], after_epoch=int(created.timestamp()) - 60)
            want = marker_for(row["id"], row["body_sha256"])
            match, by = None, None
            for m in sent:
                if m["marker"] == want:
                    match, by = m, "header"
                    break
            if match is None:
                for m in sent:
                    if m["subject"] == row["subject"] and row["recipient"].lower() in m["to"].lower():
                        match, by = m, "subject"
                        break

            if match:
                failure = store.get_failure(row["invoice_id"])
                if failure and failure["state"] not in TERMINAL_STATES:
                    store.record_message(invoice_id=row["invoice_id"], status=STATUS_SENT,
                                         provider_message_id=match["id"])
                    store.transition(row["invoice_id"], STATE_MESSAGED,
                                     reason=f"dunning_email_sent_from_{channel}_matched_by_{by}")
                store.resolve_approval(row["id"], ap.A_SENT, by=f"{channel}:{by}")
                summary["sent"] += 1
            elif datetime.fromisoformat(gone_since) + timedelta(minutes=grace_minutes(config_of(store))) <= now:
                store.resolve_approval(row["id"], ap.A_SKIPPED, by=f"{channel}:draft_deleted")
                store.record_message(invoice_id=row["invoice_id"], status=STATUS_SUPPRESSED,
                                     error="skipped_by_approver")
                summary["skipped"] += 1
            else:
                summary["waiting"] += 1
        except TRANSPORT_ERRORS as exc:
            summary["errors"] += 1
            audit_security_event(event=f"{channel}_reconcile_error",
                                 detail=f"approval {row['id']}: {exc}", severity="error")
    return summary
