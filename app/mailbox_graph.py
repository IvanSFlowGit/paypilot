# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Read-only Outlook / Microsoft 365 reader over Microsoft Graph, yielding :class:`Message` objects.

Delegated ``Mail.Read`` only. ENFORCED AND TESTED BY RECORDING EVERY REQUEST: the only calls are
``GET https://graph.microsoft.com/v1.0/me/messages`` (and its paging links) and one ``POST`` to the
Microsoft identity token endpoint. A token whose granted scope includes anything write or send is
refused. Graph pages with ``@odata.nextLink``, a FULL URL taken from the response, so a link that is
not under the Graph messages path is refused and the read is marked incomplete: following it would
send the bearer token to whoever controls that host.

NOT VERIFIED against a live tenant: Graph's error bodies, throttling (429), and whether an
organisation's conditional-access policy blocks the app. Tests use an injected transport.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime

from app.mailbox_facts import Message
from app.mailbox_gmail import real_get, real_post  # one HTTP helper, one outbound call site
from app.mailbox_imap import _KEEP_HEADERS, _TAGS, BODY_CAP, ReadResult

BASE = "https://graph.microsoft.com/v1.0/me/messages"
SCOPE = "https://graph.microsoft.com/Mail.Read offline_access"
_TENANT = re.compile(r"^[A-Za-z0-9.-]{1,64}$")
_SELECT = (
    "id,internetMessageId,from,toRecipients,subject,receivedDateTime,body,internetMessageHeaders"
)
# An ALLOWLIST, not a blocklist: any granted scope outside this set (Mail.Read.Shared, Mail.ReadWrite, Mail.Send,
# Files.*, anything new Microsoft invents) refuses the token.
_ALLOWED_SCOPES = {"mail.read", "offline_access", "openid", "profile", "email", "user.read"}

Get = Callable[[str, dict, dict], tuple[int, dict]]
Post = Callable[[str, dict], tuple[int, dict]]
__all__ = ["real_get", "real_post"]


class GraphAuthError(Exception):
    """Token exchange failed. Never carries the secret or the response body."""


def access_token(
    post: Post, tenant: str, client_id: str, client_secret: str, refresh_token: str
) -> str:
    if not _TENANT.match(tenant or ""):
        raise GraphAuthError("tenant_invalid")
    status, body = post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": SCOPE,
        },
    )
    tok = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not isinstance(tok, str) or not tok:
        raise GraphAuthError("token_exchange_failed")
    granted = {
        x.lower().removeprefix("https://graph.microsoft.com/")
        for x in str(body.get("scope") or "").split()
    }
    if granted and not granted <= _ALLOWED_SCOPES:
        raise GraphAuthError("token_has_more_than_the_read_only_scope")
    return tok


def _to_message(item: dict) -> Message | None:
    try:
        sent = datetime.fromisoformat(str(item["receivedDateTime"]).replace("Z", "+00:00"))
        if sent.tzinfo is not None:
            sent = sent.astimezone(UTC).replace(tzinfo=None)
        frm = ((item.get("from") or {}).get("emailAddress") or {}).get("address") or ""
        tos = tuple(
            str((r.get("emailAddress") or {}).get("address") or "")
            for r in item.get("toRecipients") or []
            if isinstance(r, dict)
        )
        body = item.get("body") or {}
        text = str(body.get("content") or "")
        if str(body.get("contentType") or "").lower() == "html":
            text = _TAGS.sub(" ", text)
        headers = {}
        for h in item.get("internetMessageHeaders") or []:
            name = str((h or {}).get("name") or "").lower()
            if name in _KEEP_HEADERS:
                headers[name] = str(h.get("value") or "")
        mid = str(item.get("internetMessageId") or item.get("id") or "")
        if not mid:
            return None
        return Message(
            mid, str(frm), tos, sent, str(item.get("subject") or ""), text[:BODY_CAP], headers
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def read_messages_graph(get: Get, token: str, since: date, limit: int = 500) -> ReadResult:
    hdr = {"Authorization": "Bearer " + token, "Prefer": 'outlook.body-content-type="text"'}
    out, skipped, truncated, ok = [], 0, False, True
    url = BASE
    params: dict | None = {
        "$filter": f"receivedDateTime ge {since.isoformat()}T00:00:00Z",
        "$select": _SELECT,
        "$top": "50",
    }
    try:
        while url:
            status, page = get(url, params or {}, hdr)
            if status != 200 or not isinstance(page, dict):
                raise OSError("list_failed")
            for item in page.get("value") or []:
                if len(out) >= limit:
                    truncated = True
                    break
                m = _to_message(item) if isinstance(item, dict) else None
                if m is None:
                    skipped += 1
                elif m.sent.date() >= since:
                    out.append(m)
            nxt = page.get("@odata.nextLink")
            if truncated or not nxt:
                break
            if not isinstance(nxt, str) or not (
                nxt == BASE or nxt.startswith((BASE + "?", BASE + "/"))
            ):
                raise OSError("next_link_off_graph")
            url, params = nxt, None
    except OSError:
        ok = False
    return ReadResult(out, ("graph",) if ok else (), () if ok else ("graph",), skipped, truncated)
