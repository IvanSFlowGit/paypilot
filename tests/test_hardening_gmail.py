# ruff: noqa: F811  (pytest fixtures imported by name from test_gmail_drafts)
"""A Gmail credential rotation cannot break a call that already holds the old client.

Reviewer finding 1 on M3. Request A takes client X from get_client. Request B, with a rotated
refresh token, closes X while its in-flight count is still 0. A's create_draft then hit a closed
httpx session and raised RuntimeError, which is not a transport error, so the webhook returned 500
and the approval row stayed PENDING with no draft. Reconcile later called it SKIPPED and the
invoice was refused forever as skipped by a person who never touched it.
"""

from __future__ import annotations

import httpx
import pytest

from app import gmail_drafts, loop
from app.gmail_drafts import GmailClient
from app.tenant_config import TenantConfig
from tests.test_gmail_drafts import FakeGmail, _event, gmail  # noqa: F401


def _cfg(refresh="r1") -> TenantConfig:
    return TenantConfig(
        tenant_id="default", gmail_client_id="cid", gmail_client_secret="csec",
        gmail_refresh_token=refresh,
    )


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3000})
    if request.method == "POST" and request.url.path.endswith("/drafts"):
        return httpx.Response(200, json={"id": "draft_1"})
    return httpx.Response(404)


def _session_factory(made: list):
    def make() -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(_handler), timeout=5.0)
        made.append(client)
        return client

    return make


def _client(made: list) -> GmailClient:
    factory = _session_factory(made)
    return GmailClient(http=factory(), config=_cfg(), http_factory=factory)


def test_a_client_closed_by_a_rotation_still_completes_the_call_it_was_handed():
    made: list = []
    x = _client(made)

    x.close()  # B rotated the credentials while X had no call in flight

    assert x.create_draft("raw-b64") == "draft_1"


def test_a_superseded_client_does_not_leak_a_session_after_the_call():
    made: list = []
    x = _client(made)
    x.close()

    x.create_draft("raw-b64")

    assert made, "the client never opened a session"
    assert all(c.is_closed for c in made), "a session was left open on a superseded client"


def test_a_client_that_was_not_superseded_keeps_one_session():
    made: list = []
    x = _client(made)

    x.create_draft("a")
    x.create_draft("b")

    assert len(made) == 1 and not made[0].is_closed


def test_the_cache_still_closes_a_superseded_client_when_idle():
    made: list = []
    old = _client(made)
    gmail_drafts._clients.clear()
    gmail_drafts._clients[("default", "stale-fingerprint")] = old
    try:
        gmail_drafts.get_client(_cfg("rotated"))
    finally:
        gmail_drafts._clients.clear()

    assert made[0].is_closed


def test_a_non_transport_error_creating_the_draft_leaves_no_pending_row(gmail, isolated_store):
    fake, _resend = gmail

    def _boom(raw):
        raise RuntimeError("anything that is not a transport error")

    fake.create_draft = _boom

    with pytest.raises(RuntimeError):
        loop.handle_payment_failed(_event(), isolated_store)

    rows = isolated_store.approvals_for("in_1")
    assert rows, "the approval row was never opened"
    assert all(r["status"] != "pending" for r in rows), "a PENDING row was left with no draft"
    assert rows[0]["status"] == "failed"


def test_a_close_that_lands_between_the_token_fetch_and_the_request_is_deferred():
    """The in-flight count must start BEFORE the token is fetched, not after."""
    made: list = []
    x = _client(made)
    real_token = x._access_token

    def _rotation_lands_now() -> str:
        x.close()  # another request rotated the credentials while this call is mid-flight
        return real_token()

    x._access_token = _rotation_lands_now

    assert x.create_draft("raw-b64") == "draft_1"
    assert all(c.is_closed for c in made), "the deferred close never happened"
