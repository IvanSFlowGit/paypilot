# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Gmail Drafts as the approval step: the approver presses Send in Gmail.

PayPilot creates the message as a DRAFT in the approver's own mailbox. It never
sends. The person opens Gmail (web or phone), reads it, edits it if they want,
and presses Send. Deleting the draft is a refusal. PayPilot then notices what
happened and records it.

What Google's own documentation says, read 2026-10-27:

* ``gmail.compose`` ("Manage drafts and send emails") is a RESTRICTED scope, as
  are ``gmail.readonly`` and ``gmail.metadata``. Detecting a send needs one of
  the read scopes as well as compose. A public app using restricted scopes has
  to pass Google's app verification. Until it has, only a small set of listed
  test users can authorise it. The default email mode needs none of this.
* "the draft is automatically deleted and a new message with an updated ID is
  created" on send. So a draft cannot be tracked to its sent message by id.
  Detection works by search plus a header instead.

NOT VERIFIED AGAINST A LIVE MAILBOX, and the code says so rather than hoping:

* Whether Gmail keeps the custom ``X-PayPilot-Approval`` header when a person
  sends a draft from the app. If it does not, matching falls back to recipient,
  subject and time, and the match is recorded as ``subject`` so a reviewer can
  see which kind of evidence it was.
* How long after a send the message appears in search. A draft that has
  vanished and has no matching sent message is therefore NOT treated as
  skipped until a grace period has passed.

The HTTP layer is a small injectable class so the logic is tested without a
network. Running it against a real mailbox is a separate, human-run step: see
``docs/approval.md``.
"""

from __future__ import annotations

import base64
import os
import time
from datetime import datetime, timedelta
from email.message import EmailMessage

import httpx

from app.audit import audit_security_event
from app.mailer import STATUS_SENT, STATUS_SUPPRESSED

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
HEADER = "X-PayPilot-Approval"
DEFAULT_GRACE_MINUTES = 15


class GmailNotConfigured(RuntimeError):
    pass


class GmailError(RuntimeError):
    pass


def configured() -> bool:
    # Literal os.getenv calls, not a helper taking a name: scripts/env_surface.py
    # finds the variables the app reads by looking for them, and the README
    # documents only what that probe sees.
    return all((
        (os.getenv("PAYPILOT_GOOGLE_CLIENT_ID") or "").strip(),
        (os.getenv("PAYPILOT_GOOGLE_CLIENT_SECRET") or "").strip(),
        (os.getenv("PAYPILOT_GMAIL_REFRESH_TOKEN") or "").strip(),
    ))


def grace_minutes() -> int:
    try:
        raw = (os.getenv("PAYPILOT_GMAIL_GRACE_MINUTES") or "").strip()
        return max(1, int(raw or DEFAULT_GRACE_MINUTES))
    except ValueError:
        return DEFAULT_GRACE_MINUTES


class GmailClient:
    """The four Gmail calls this module needs, with a cached access token."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        if not configured():
            raise GmailNotConfigured("Google client id, secret and refresh token are required")
        self._http = http or httpx.Client(timeout=15.0)
        self._token: str | None = None
        self._token_until = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_until - 30:
            return self._token
        r = self._http.post(TOKEN_URL, data={
            "client_id": (os.getenv("PAYPILOT_GOOGLE_CLIENT_ID") or "").strip(),
            "client_secret": (os.getenv("PAYPILOT_GOOGLE_CLIENT_SECRET") or "").strip(),
            "refresh_token": (os.getenv("PAYPILOT_GMAIL_REFRESH_TOKEN") or "").strip(),
            "grant_type": "refresh_token",
        })
        if r.status_code != 200:
            # Never include the body: it can echo the client id.
            raise GmailError(f"token_refresh_http_{r.status_code}")
        data = r.json()
        self._token = data["access_token"]
        self._token_until = time.time() + float(data.get("expires_in", 3000))
        return self._token

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        return self._http.request(method, f"{API}{path}", headers=headers, **kw)

    def create_draft(self, raw_b64url: str) -> str:
        r = self._call("POST", "/drafts", json={"message": {"raw": raw_b64url}})
        if r.status_code != 200:
            raise GmailError(f"create_draft_http_{r.status_code}")
        return r.json()["id"]

    def draft_exists(self, draft_id: str) -> bool:
        r = self._call("GET", f"/drafts/{draft_id}", params={"format": "minimal"})
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise GmailError(f"get_draft_http_{r.status_code}")
        return True

    def delete_draft(self, draft_id: str) -> None:
        r = self._call("DELETE", f"/drafts/{draft_id}")
        if r.status_code not in (200, 204, 404):
            raise GmailError(f"delete_draft_http_{r.status_code}")

    def sent_since(self, *, to: str, after_epoch: int) -> list[dict]:
        """Metadata of recent sent messages to ``to``: id, subject, to, header."""
        r = self._call("GET", "/messages", params={
            "q": f"in:sent to:{to} after:{after_epoch}", "maxResults": 10})
        if r.status_code != 200:
            raise GmailError(f"list_sent_http_{r.status_code}")
        out = []
        for item in r.json().get("messages", []):
            m = self._call("GET", f"/messages/{item['id']}", params=[
                ("format", "metadata"), ("metadataHeaders", "Subject"),
                ("metadataHeaders", "To"), ("metadataHeaders", HEADER)])
            if m.status_code != 200:
                raise GmailError(f"get_message_http_{m.status_code}")
            hdrs = {h["name"].lower(): h["value"]
                    for h in m.json().get("payload", {}).get("headers", [])}
            out.append({"id": item["id"], "subject": hdrs.get("subject", ""),
                        "to": hdrs.get("to", ""), "marker": hdrs.get(HEADER.lower(), "")})
        return out


_client: GmailClient | None = None


def get_client() -> GmailClient:
    """Process-wide client. Tests replace it with :func:`set_client`."""
    global _client
    if _client is None:
        _client = GmailClient()
    return _client


def set_client(client: GmailClient | None) -> None:
    global _client
    _client = client


def build_raw(*, recipient: str, subject: str, body: str, marker: str) -> str:
    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject
    msg[HEADER] = marker
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _marker(approval_id: int, body_sha256: str) -> str:
    return f"{approval_id}:{body_sha256[:16]}"


def request_draft(*, invoice_id: str, recipient: str, subject: str, body: str, store) -> dict:
    """Create the draft and open the approval row. Fails closed, loudly."""
    from app import approval as ap

    if not configured():
        return ap._record_refusal(store, invoice_id, "gmail_not_configured")
    try:
        client = get_client()
    except GmailNotConfigured:
        return ap._record_refusal(store, invoice_id, "gmail_not_configured")

    expires = ap._iso(ap._now_dt() + timedelta(hours=ap.ttl_hours()))
    # Open the row first with a throwaway token hash: the approval id is part of
    # the draft's marker header, and a draft must never exist without a row.
    approval_id = store.create_approval(
        invoice_id=invoice_id, channel="gmail", recipient=recipient, subject=subject,
        body=body, token_hash=ap.hash_token(os.urandom(16).hex()), expires_at=expires,
    )
    if approval_id is None:
        existing = [a for a in store.approvals_for(invoice_id) if a["status"] == ap.A_PENDING]
        return {"status": "pending_approval",
                "approval_id": existing[0]["id"] if existing else None, "duplicate": True}

    row = store.get_approval(approval_id)
    try:
        draft_id = client.create_draft(build_raw(
            recipient=recipient, subject=subject, body=body,
            marker=_marker(approval_id, row["body_sha256"])))
    except (GmailError, httpx.HTTPError) as exc:
        store.resolve_approval(approval_id, ap.A_FAILED, by="system",
                               error=f"draft_create_failed:{type(exc).__name__}")
        audit_security_event(event="gmail_draft_create_failed",
                             detail=f"invoice {invoice_id}: {exc}", severity="error")
        return ap._record_refusal(store, invoice_id, "gmail_draft_create_failed")

    store._conn.execute("UPDATE approvals SET external_id = ? WHERE id = ?",
                        (draft_id, approval_id))
    store._conn.commit()

    # Best effort: tell the approver a draft is waiting. No link, so it needs no
    # guard exception, and a failure here does not undo the draft.
    from app.mailer import send_operator_alert

    for address in ap.approver_emails():
        send_operator_alert(
            to=address,
            subject=f"A payment recovery email is waiting in your Gmail drafts ({invoice_id})",
            body=("PayPilot put a draft in your Gmail Drafts folder. Nothing has been "
                  "sent. Open the draft, read it, and press Send, or delete it to skip.\n"
                  f"To: {recipient}\nSubject: {subject}\n"),
        )
    return {"status": "pending_approval", "approval_id": approval_id, "expires_at": expires}


def reconcile(store, client: GmailClient | None = None) -> dict:
    """Settle every pending Gmail approval: sent, skipped, expired or still waiting."""
    from app import approval as ap
    from app.store import STATE_MESSAGED, TERMINAL_STATES

    if not configured() and client is None:
        return {"checked": 0, "error": "gmail_not_configured"}
    client = client or get_client()
    summary = {"checked": 0, "sent": 0, "skipped": 0, "waiting": 0, "errors": 0}
    now = ap._now_dt()

    for a in store.list_approvals(status=ap.A_PENDING, limit=500):
        row = store.get_approval(a["id"])
        if row["channel"] != "gmail":
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
            want = _marker(row["id"], row["body_sha256"])
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
                                     reason=f"dunning_email_sent_from_gmail_matched_by_{by}")
                store.resolve_approval(row["id"], ap.A_SENT, by=f"gmail:{by}")
                summary["sent"] += 1
            elif datetime.fromisoformat(gone_since) + timedelta(minutes=grace_minutes()) <= now:
                store.resolve_approval(row["id"], ap.A_SKIPPED, by="gmail:draft_deleted")
                store.record_message(invoice_id=row["invoice_id"], status=STATUS_SUPPRESSED,
                                     error="skipped_by_approver")
                summary["skipped"] += 1
            else:
                summary["waiting"] += 1
        except (GmailError, httpx.HTTPError) as exc:
            summary["errors"] += 1
            audit_security_event(event="gmail_reconcile_error",
                                 detail=f"approval {row['id']}: {exc}", severity="error")
    return summary
