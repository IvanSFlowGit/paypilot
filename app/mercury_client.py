# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""A read-only client for Mercury's receivables API.

Read-only is a property of the class, not a promise in a comment: the only HTTP
method this module can issue is GET, there is no method that writes, and a test
asserts both. Mercury issues Read Only, Read and Write, and Custom tokens
(claims row 41); a token's type cannot be read from the token, so the control is
that this client has no code path that would use write rights if it were handed
them, and the onboarding instruction is to create a Read Only token.

Nothing here calls Mercury on import, and the default transport is only built
when :func:`default_transport` is called. Every test uses an injected fake.

What the documentation does not state is not assumed (claims rows 43, 45, 48,
50): the response envelope of a list, pagination, and whether the AR paths sit
under the same base as the accounts example. A list therefore returns
``complete="unknown"`` and the caller must not report a total from it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

#: Taken from the accounts example on the getting-started page (claims row 50).
DEFAULT_BASE_URL = "https://api.mercury.com/api/v1"
TOKEN_ENV = "MERCURY_API_TOKEN"

MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 15
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

Transport = Callable[[str, str, dict[str, str], float], "tuple[int, bytes]"]


class MercuryError(Exception):
    """Base class. No subclass message ever contains the token."""


class MercuryAuthError(MercuryError):
    """401 or 403: the token is missing, wrong, revoked or lacks the permission."""


class MercuryNotFound(MercuryError):
    """404."""


class MercuryRateLimited(MercuryError):
    """429 still returned after the bounded retries."""


class MercuryServerError(MercuryError):
    """5xx, or a transport failure, still returned after the bounded retries."""


class MercuryResponseError(MercuryError):
    """The body was not the JSON shape the caller said to expect."""


@dataclass(frozen=True)
class ListResult:
    """Items from one list call. ``complete`` is never True: pagination is unstated."""

    items: list
    complete: str = "unknown"


def default_transport(method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """The one place a socket is opened. Not used by any test."""
    if method != "GET":
        raise MercuryError("only GET is permitted")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https base, fixed
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


class MercuryReadClient:
    """Fetch invoices. Holds a token privately and never prints it."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        value = token if token is not None else os.environ.get(TOKEN_ENV, "")
        if not value or not value.strip():
            raise MercuryAuthError("no token: set " + TOKEN_ENV)
        if not base_url.startswith("https://"):
            raise MercuryError("base_url: https only")
        self.__token = value.strip()
        self._base = base_url.rstrip("/")
        self._transport = transport or default_transport
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"MercuryReadClient(base={self._base!r}, token=<redacted>)"

    def list_invoices(self, *, items_key: str | None = None) -> ListResult:
        """GET /ar/invoices. ``items_key`` names the list inside an object envelope,
        or None when the body is a bare array. Anything else is a response error."""
        body = self._get("/ar/invoices")
        if items_key is None:
            items = body
        elif isinstance(body, dict):
            items = body.get(items_key)
        else:
            items = None
        if not isinstance(items, list):
            raise MercuryResponseError("list: not the expected array shape")
        return ListResult(items=items)

    def get_invoice(self, invoice_id: str) -> dict:
        """GET /ar/invoices/{id}."""
        if not isinstance(invoice_id, str) or not invoice_id or not all(
            c.isalnum() or c in "-_" for c in invoice_id
        ):
            raise MercuryError("invoice_id: expected letters, digits, hyphen, underscore")
        body = self._get(f"/ar/invoices/{invoice_id}")
        if not isinstance(body, dict):
            raise MercuryResponseError("invoice: not an object")
        return body

    def _get(self, path: str):
        url = self._base + path
        headers = {"Authorization": "Bearer " + self.__token, "Accept": "application/json"}
        last = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                status, raw = self._transport("GET", url, headers, TIMEOUT_SECONDS)
            except (TimeoutError, OSError):
                status, raw = 0, b""
            last = status
            if status == 200:
                try:
                    return json.loads(raw)
                except ValueError as exc:
                    raise MercuryResponseError("body: not JSON") from exc
            if status in (401, 403):
                raise MercuryAuthError(f"status {status}")
            if status == 404:
                raise MercuryNotFound("status 404")
            if status in RETRY_STATUSES or status == 0:
                if attempt < MAX_ATTEMPTS:
                    self._sleep(min(2 ** (attempt - 1), 8))
                    continue
                break
            raise MercuryError(f"status {status}")
        if last == 429:
            raise MercuryRateLimited("status 429 after retries")
        raise MercuryServerError(f"status {last} after retries")
