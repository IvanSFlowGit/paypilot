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

import os
import time

import httpx

from app.mailbox_approval import (  # noqa: F401 - re-exported for callers and tests
    HEADER,
    MailboxError,
    build_raw,
    grace_minutes,
    marker_for,
    open_draft,
)
from app.mailbox_approval import reconcile as _reconcile

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailNotConfigured(RuntimeError):
    pass


class GmailError(MailboxError):
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


def request_draft(*, invoice_id: str, recipient: str, subject: str, body: str, store) -> dict:
    """Create the draft and open the approval row. Fails closed, loudly."""
    from app import approval as ap

    if not configured():
        return ap._record_refusal(store, invoice_id, "gmail_not_configured")
    try:
        client = get_client()
    except GmailNotConfigured:
        return ap._record_refusal(store, invoice_id, "gmail_not_configured")
    return open_draft(channel="gmail", client=client, invoice_id=invoice_id,
                      recipient=recipient, subject=subject, body=body, store=store,
                      where="Gmail")


def reconcile(store, client: GmailClient | None = None) -> dict:
    """Settle every pending Gmail approval: sent, skipped, expired or waiting."""
    if not configured() and client is None:
        return {"checked": 0, "error": "gmail_not_configured"}
    return _reconcile(store, client or get_client(), "gmail")
