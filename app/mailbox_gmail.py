# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Read-only Gmail API reader that yields :class:`app.mailbox_facts.Message` objects.

Uses ONLY the ``gmail.readonly`` scope, which Google classes as RESTRICTED. For a single known client
running its own Google project this works in testing mode (refresh tokens expire after 7 days there);
for anything public it needs Google's verification and a security assessment, which is why the IMAP
reader (``mailbox_imap``) is the default and this is the second route.

ENFORCED, AND TESTED BY RECORDING EVERY REQUEST: the only requests are ``GET`` on the messages list
and messages get endpoints, plus one ``POST`` to Google's token endpoint to turn a refresh token into
an access token. No modify, send, delete, trash, label or batch endpoint is ever built into a URL. The
page cursor is a ``pageToken`` value, never a URL, so a hostile response cannot redirect the bearer
token somewhere else. A message whose reported size is over the cap is not downloaded. Any non-200
makes the read incomplete; nothing retries, because a half-retried read is a partial read.

NOT VERIFIED against a live account: the exact shape of Google's error bodies and rate limiting. The
transport is injected, so tests prove the request shapes and the refusals only.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime

from app.mailbox_imap import FETCH_CAP, ReadResult, _parse

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PAGE = 100

Get = Callable[[str, dict, dict], tuple[int, dict]]
Post = Callable[[str, dict], tuple[int, dict]]


class GmailAuthError(Exception):
    """Token exchange failed. Never carries the secret or the response body."""


def _http(
    method: str, url: str, params: dict | None, headers: dict | None, data: dict | None
) -> tuple[int, dict]:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed https hosts above
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, ValueError, OSError):
        return 0, {}


def real_get(url: str, params: dict, headers: dict) -> tuple[int, dict]:
    return _http("GET", url, params, headers, None)


def real_post(url: str, data: dict) -> tuple[int, dict]:
    return _http("POST", url, None, {"Content-Type": "application/x-www-form-urlencoded"}, data)


def access_token(post: Post, client_id: str, client_secret: str, refresh_token: str) -> str:
    status, body = post(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    tok = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not isinstance(tok, str) or not tok:
        raise GmailAuthError("token_exchange_failed")
    granted = str(body.get("scope") or "")
    if granted and any(s != SCOPE for s in granted.split()):
        raise GmailAuthError("token_has_more_than_the_read_only_scope")
    return tok


def read_messages_gmail(get: Get, token: str, since: date, limit: int = 500) -> ReadResult:
    """Read messages newer than ``since``, read-only, up to ``limit``."""
    hdr = {"Authorization": "Bearer " + token}
    out, skipped, truncated, ok = [], 0, False, True
    q = f"after:{int(datetime(since.year, since.month, since.day).timestamp())}"
    page: str | None = None
    try:
        while True:
            params = {"q": q, "maxResults": str(PAGE)}
            if page:
                params["pageToken"] = page
            status, listing = get(API, params, hdr)
            if status != 200:
                raise OSError("list_failed")
            for ref in listing.get("messages") or []:
                if len(out) >= limit:
                    truncated = True
                    break
                mid = str(ref.get("id") or "")
                if not mid.isalnum():
                    skipped += 1
                    continue
                st, meta = get(f"{API}/{mid}", {"format": "minimal"}, hdr)
                if st != 200:
                    raise OSError("get_failed")
                if int(meta.get("sizeEstimate") or 0) > FETCH_CAP:
                    skipped += 1
                    continue
                st, full = get(f"{API}/{mid}", {"format": "raw"}, hdr)
                if st != 200:
                    raise OSError("get_failed")
                try:
                    raw = base64.urlsafe_b64decode(
                        str(full.get("raw") or "") + "=" * (-len(str(full.get("raw") or "")) % 4)
                    )
                except (ValueError, TypeError):
                    skipped += 1
                    continue
                m = _parse(raw, "gmail", mid)
                if m is None:
                    skipped += 1
                elif m.sent.date() >= since:
                    out.append(m)
            page = listing.get("nextPageToken")
            if truncated or not page:
                break
    except OSError:
        ok = False
    return ReadResult(out, ("gmail",) if ok else (), () if ok else ("gmail",), skipped, truncated)
