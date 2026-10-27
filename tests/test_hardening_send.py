# ruff: noqa: F811  (pytest fixtures imported by name from test_approval)
"""A delivered email is never turned into a FAILED approval by a ledger write.

Resend answered 2xx, so the mail has left. If the ledger write that records it then raises (SQLite
"database is locked"), the approval used to be resolved FAILED with no messages row. Cooldown and
touch cap read the messages table, so nothing remembered the send, and a Stripe retry could mail
the customer a second time. The write is retried a bounded number of times and, if it still
fails, the send is reported as sent: nothing after a 2xx may read as a failure.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import approval, loop, mailer
from app.store import STATE_MESSAGED
from tests.test_approval import (  # noqa: F401  (fixtures are used by name)
    _approval_link,
    _customer_mails,
    _event,
    client,
    mail,
)

_FORM = {"content-type": "application/x-www-form-urlencoded"}


def _fail_sent_writes(store, monkeypatch, times: int):
    """Make record_message raise for SENT rows the first `times` calls, then behave."""
    real = store.record_message
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        if kwargs.get("status") == mailer.STATUS_SENT:
            calls["n"] += 1
            if calls["n"] <= times:
                raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "record_message", flaky)
    monkeypatch.setattr(mailer, "_LEDGER_RETRY_SLEEP", 0.0)
    return calls


@pytest.mark.parametrize("failures", [1, 2])
def test_a_transient_ledger_failure_after_a_2xx_is_retried_and_recorded(
    mail, isolated_store, client, monkeypatch, failures
):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    _fail_sent_writes(isolated_store, monkeypatch, failures)

    r = client.post(f"/approve/{aid}", content=f"t={token}&action=approve", headers=_FORM)

    assert r.status_code == 200 and "Sent" in r.text
    assert len(_customer_mails(mail)) == 1
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_SENT
    assert isolated_store.sent_message_count("in_1") == 1
    assert isolated_store.get_failure("in_1")["state"] == STATE_MESSAGED


def test_a_persistent_ledger_failure_after_a_2xx_never_reads_as_failed(
    mail, isolated_store, client, monkeypatch
):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    _fail_sent_writes(isolated_store, monkeypatch, 10_000)

    r = client.post(f"/approve/{aid}", content=f"t={token}&action=approve", headers=_FORM)

    assert r.status_code == 200 and "Sent" in r.text
    assert len(_customer_mails(mail)) == 1, "the mail left exactly once"
    row = isolated_store.approvals_for("in_1")[0]
    assert row["status"] == approval.A_SENT, "a delivered mail must not read as FAILED"
    assert isolated_store.get_failure("in_1")["state"] == STATE_MESSAGED
    # A second click, or a Stripe redelivery, cannot send again.
    again = client.post(f"/approve/{aid}", content=f"t={token}&action=approve", headers=_FORM)
    assert len(_customer_mails(mail)) == 1
    assert "Sent" in again.text or again.status_code in (200, 404, 409)


def test_the_send_function_reports_sent_when_only_the_ledger_write_fails(
    isolated_store, monkeypatch
):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "me@mine.test")

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_ok"}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    _fail_sent_writes(isolated_store, monkeypatch, 10_000)

    result = mailer.send_dunning_email(
        invoice_id="in_1", to="me@mine.test", subject="s", body="b", store=isolated_store
    )

    assert result["status"] == mailer.STATUS_SENT
    assert result["provider_message_id"] == "rs_ok"
    assert result["message_id"] is None
    assert result.get("ledger_write_failed") is True


def test_a_failed_send_is_still_recorded_as_failed(isolated_store, monkeypatch):
    """The fix must not turn a real failure into a success."""
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "me@mine.test")

    class _Bad:
        status_code = 500
        content = b"{}"

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Bad())
    monkeypatch.setattr(mailer, "_BACKOFF_SECONDS", 0.0)

    result = mailer.send_dunning_email(
        invoice_id="in_1", to="me@mine.test", subject="s", body="b", store=isolated_store
    )

    assert result["status"] == mailer.STATUS_FAILED
