# ruff: noqa: F811  (pytest fixtures imported by name from test_approval)
"""Second round of reviewer findings on the M3 hardening pass. Each fails on the code before its fix.

The thread running through them: nothing that happens after a mail has left may make the system
forget it left, and a retry of the bookkeeping may not double the bookkeeping.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from app import loop, mailer
from app.gmail_drafts import GmailClient
from app.tenant_config import TenantConfig, config_for
from tests.test_approval import (  # noqa: F401
    CUSTOMER,
    _customer_mails,
    _event,
    _preflight,
    _ready_env,
    mail,
)
from tests.test_hardening_send import _fail_sent_writes

# -- 4: a retried ledger write must not record the send twice --------------------------------


class _CommitFailsOnce:
    """Connection proxy: the INSERT runs, the first COMMIT raises, later ones work."""

    def __init__(self, real):
        self._real = real
        self._failed = False

    def commit(self):
        if not self._failed:
            self._failed = True
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_commit_failure_then_a_retry_records_exactly_one_sent_row(isolated_store):
    isolated_store._conn = _CommitFailsOnce(isolated_store._conn)

    with pytest.raises(sqlite3.OperationalError):
        isolated_store.record_message(invoice_id="in_1", channel="email", status="sent")
    isolated_store.record_message(invoice_id="in_1", channel="email", status="sent")

    assert isolated_store.sent_message_count("in_1") == 1, "the failed attempt's row survived"


# -- 3: a failing session factory must not leak the in-flight count ---------------------------


def test_a_factory_failure_on_reopen_does_not_leak_the_in_flight_count():
    cfg = TenantConfig(
        tenant_id="default", gmail_client_id="c", gmail_client_secret="s", gmail_refresh_token="r"
    )

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3000})
        return httpx.Response(200, json={"id": "d1"})

    made: list = []
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 2:  # the reopen after close()
            raise ssl_error()
        c = httpx.Client(transport=httpx.MockTransport(handler))
        made.append(c)
        return c

    def ssl_error():
        return OSError("could not build an SSL context")

    x = GmailClient(config=cfg, http_factory=factory)
    x.close()

    with pytest.raises(OSError):
        x.create_draft("raw")
    assert x._inflight == 0, "the failed reopen left the call counted as in flight"

    assert x.create_draft("raw") == "d1"
    assert all(c.is_closed for c in made), "a session was left open on a superseded client"


# -- 5: exact-string settings keep a whitespace-only value, as they always have ---------------


@pytest.mark.parametrize(
    ("env", "field"), [("PAYPILOT_PII_SALT", "pii_salt"), ("PAYPILOT_IMAP_PASSWORD", "imap_password")]
)
def test_a_whitespace_only_exact_setting_is_kept_not_treated_as_unset(monkeypatch, env, field):
    monkeypatch.setenv(env, "  ")
    assert getattr(config_for("default"), field) == "  "


@pytest.mark.parametrize("env", ["PAYPILOT_PII_SALT", "PAYPILOT_IMAP_PASSWORD"])
def test_an_empty_exact_setting_is_still_unset(monkeypatch, env):
    monkeypatch.setenv(env, "")
    field = {"PAYPILOT_PII_SALT": "pii_salt", "PAYPILOT_IMAP_PASSWORD": "imap_password"}[env]
    assert getattr(config_for("default"), field) == ""


# -- 9: the preflight does not call a deployment READY that can send nothing ------------------


def test_the_preflight_flags_a_blank_from_address(monkeypatch, isolated_store):
    _ready_env(monkeypatch)
    monkeypatch.setenv("PAYPILOT_FROM_EMAIL", " ")

    problems, _notes = _preflight().check()

    assert any("PAYPILOT_FROM_EMAIL" in p for p in problems), problems


def test_the_preflight_is_still_clean_with_the_default_sender(monkeypatch, isolated_store):
    _ready_env(monkeypatch)
    monkeypatch.delenv("PAYPILOT_FROM_EMAIL", raising=False)
    assert _preflight().check()[0] == []


# -- 8: the write transaction rolls back --------------------------------------------------


def test_a_failure_after_the_insert_leaves_no_row_and_no_open_transaction(tmp_path):
    from app.store import Database
    from app.tenant_config import _write_transaction

    db = Database(tmp_path / "t.db", owner_tenant="default")

    with pytest.raises(RuntimeError), db.lock, _write_transaction(db):
        db.conn.execute(
            "INSERT INTO tenant_config (tenant_id, behaviour_json, secrets_enc, updated_at) "
            "VALUES ('default', '{}', NULL, 'now')"
        )
        raise RuntimeError("boom after the insert")

    assert db.conn.in_transaction is False
    assert db.conn.execute("SELECT COUNT(*) FROM tenant_config").fetchone()[0] == 0


# -- 1: an unrecorded send leaves a durable trace and is not repeated --------------------------


def test_an_unrecorded_send_emits_a_durable_audit_event(isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", CUSTOMER)

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_ok"}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    events: list = []
    monkeypatch.setattr(mailer, "audit_security_event", lambda **kw: events.append(kw))
    _fail_sent_writes(isolated_store, monkeypatch, 10_000)

    mailer.send_dunning_email(
        invoice_id="in_1", to=CUSTOMER, subject="s", body="b", store=isolated_store
    )

    assert [e["event"] for e in events] == ["sent_but_unrecorded"]
    assert "in_1" in events[0]["detail"]


def test_a_second_distinct_event_after_an_unrecorded_send_does_not_mail_again(
    mail, isolated_store, monkeypatch
):
    """Cooldown and the touch cap read the messages table, which has no row for the unrecorded
    send. The mailer remembers it for the day instead."""
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "off")
    _fail_sent_writes(isolated_store, monkeypatch, 10_000)

    loop.handle_payment_failed(_event("in_1"), isolated_store)
    assert len(_customer_mails(mail)) == 1

    second = _event("in_1")
    second["id"] = "evt_in_1_retry"
    second["data"]["object"]["attempt_count"] = 2
    loop.handle_payment_failed(second, isolated_store)

    assert len(_customer_mails(mail)) == 1, "an unrecorded send was mailed a second time"
