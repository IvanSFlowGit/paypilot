"""Gmail drafts as the approval step. No network: a fake client for the logic,
``httpx.MockTransport`` for the wire format.

What is NOT proven here, and is recorded in docs/approval.md as a human-run
step: that a real mailbox keeps the marker header on send and how soon a sent
message becomes searchable. These tests pin the behaviour for both outcomes.
"""

from __future__ import annotations

import base64
import email
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import approval, gmail_drafts, loop, stripe_client
from app.gmail_drafts import GmailClient, GmailError
from app.store import STATE_FAILED, STATE_MESSAGED

PORTAL = "https://billing.stripe.com/p/session/test_abc123"
CUSTOMER = "me@mine.test"
APPROVER = "owner@merchant.test"


def _event(invoice_id="in_1"):
    return {
        "id": f"evt_{invoice_id}", "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": "cus_XYZ",
            "subscription": "sub_XYZ", "customer_email": CUSTOMER, "amount_due": 4900,
            "currency": "eur", "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


class FakeGmail:
    """Stands in for GmailClient. Records calls; behaviour is set per test."""

    def __init__(self):
        self.drafts: dict[str, str] = {}
        self.deleted: list[str] = []
        self.sent: list[dict] = []
        self.raws: list[str] = []
        self.fail_create = False

    def create_draft(self, raw):
        if self.fail_create:
            raise GmailError("create_draft_http_403")
        self.raws.append(raw)
        did = f"r{len(self.drafts) + 1}"
        self.drafts[did] = raw
        return did

    def draft_exists(self, draft_id):
        return draft_id in self.drafts

    def delete_draft(self, draft_id):
        self.drafts.pop(draft_id, None)
        self.deleted.append(draft_id)

    def sent_since(self, *, to, after_epoch):
        return list(self.sent)


@pytest.fixture
def gmail(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "gmail")
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", CUSTOMER)
    monkeypatch.setenv("PAYPILOT_APPROVER_EMAILS", APPROVER)
    monkeypatch.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "0")
    monkeypatch.setenv("PAYPILOT_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("PAYPILOT_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("PAYPILOT_GMAIL_REFRESH_TOKEN", "rtoken")
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid, *_a, **_k: PORTAL)
    resend: list[dict] = []

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_ok"}

    monkeypatch.setattr("httpx.post", lambda url, json=None, **k: (resend.append(json), _Resp())[1])
    fake = FakeGmail()
    gmail_drafts.set_client(fake)
    yield fake, resend
    gmail_drafts.set_client(None)


def _customer_resend(resend):
    return [p for p in resend if CUSTOMER in p["to"]]


# -- creating the draft ------------------------------------------------------


def test_a_failure_creates_a_draft_and_sends_nothing(gmail, isolated_store):
    fake, resend = gmail
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["status"] == "pending_approval"
    assert len(fake.drafts) == 1
    assert _customer_resend(resend) == [], "PayPilot never sends in gmail mode"
    assert isolated_store.get_failure("in_1")["state"] == STATE_FAILED
    row = isolated_store.approvals_for("in_1")[0]
    assert row["channel"] == "gmail" and row["external_id"] == "r1"


def test_the_draft_is_the_exact_text_with_a_marker_header(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(fake.raws[0]))

    assert msg["To"] == CUSTOMER
    assert msg["Subject"] == row["subject"]
    assert msg["X-PayPilot-Approval"] == f"{row['id']}:{row['body_sha256'][:16]}"
    assert msg.get_payload(decode=True).decode().strip() == row["body"].strip()


def test_the_approver_is_told_a_draft_is_waiting_without_a_link(gmail, isolated_store):
    _, resend = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    notes = [p for p in resend if APPROVER in p["to"]]

    assert len(notes) == 1
    assert "http" not in notes[0]["text"]


def test_not_configured_fails_closed(gmail, isolated_store, monkeypatch):
    fake, resend = gmail
    monkeypatch.delenv("PAYPILOT_GMAIL_REFRESH_TOKEN")
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["error"] == "gmail_not_configured"
    assert fake.drafts == {} and _customer_resend(resend) == []


def test_a_draft_that_cannot_be_created_fails_closed(gmail, isolated_store):
    fake, resend = gmail
    fake.fail_create = True
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["error"] == "gmail_draft_create_failed"
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_FAILED
    assert _customer_resend(resend) == []


def test_a_retry_does_not_make_a_second_draft(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    again = _event()
    again["id"] = "evt_2"
    loop.handle_payment_failed(again, isolated_store)

    assert len(fake.drafts) == 1


# -- reconciling -------------------------------------------------------------


def _marker(isolated_store):
    row = isolated_store.approvals_for("in_1")[0]
    return f"{row['id']}:{row['body_sha256'][:16]}"


def test_a_draft_still_there_is_still_waiting(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    out = gmail_drafts.reconcile(isolated_store, fake)

    assert out["waiting"] == 1
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_a_sent_message_with_the_marker_settles_the_invoice(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    fake.drafts.clear()  # Gmail deletes the draft on send
    fake.sent = [{"id": "m_new", "subject": "x", "to": "y", "marker": _marker(isolated_store)}]

    out = gmail_drafts.reconcile(isolated_store, fake)

    assert out["sent"] == 1
    row = isolated_store.approvals_for("in_1")[0]
    assert row["status"] == approval.A_SENT and row["resolved_by"] == "gmail:header"
    assert isolated_store.get_failure("in_1")["state"] == STATE_MESSAGED
    assert isolated_store.sent_message_count("in_1") == 1
    assert isolated_store.messages_for("in_1")[-1]["provider_message_id"] == "m_new"


def test_if_gmail_drops_the_header_subject_and_recipient_still_match(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    fake.drafts.clear()
    fake.sent = [{"id": "m2", "subject": row["subject"], "to": f"Me <{CUSTOMER}>", "marker": ""}]

    gmail_drafts.reconcile(isolated_store, fake)

    assert isolated_store.approvals_for("in_1")[0]["resolved_by"] == "gmail:subject"
    assert isolated_store.get_failure("in_1")["state"] == STATE_MESSAGED


def test_a_vanished_draft_is_not_called_skipped_inside_the_grace_period(gmail, isolated_store):
    """A just-sent message may not be searchable yet. Calling the approval
    skipped now would suppress every later request for the invoice."""
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    fake.drafts.clear()

    out = gmail_drafts.reconcile(isolated_store, fake)

    assert out["waiting"] == 1
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_a_deleted_draft_becomes_skipped_after_the_grace_period(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    fake.drafts.clear()
    gmail_drafts.reconcile(isolated_store, fake)  # first sighting of the gap
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    isolated_store._conn.execute("UPDATE approvals SET gone_since = ?", (old,))
    isolated_store._conn.commit()

    out = gmail_drafts.reconcile(isolated_store, fake)

    assert out["skipped"] == 1
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_SKIPPED
    assert isolated_store.sent_message_count("in_1") == 0
    # And the person's answer sticks: no new draft on the next failure.
    again = _event()
    again["id"] = "evt_3"
    result = loop.handle_payment_failed(again, isolated_store)
    assert result["delivery"]["error"] == "approver_skipped"


def test_a_late_sent_message_still_wins_over_the_skip(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    fake.drafts.clear()
    gmail_drafts.reconcile(isolated_store, fake)
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    isolated_store._conn.execute("UPDATE approvals SET gone_since = ?", (old,))
    isolated_store._conn.commit()
    fake.sent = [{"id": "m3", "subject": "", "to": "", "marker": _marker(isolated_store)}]

    gmail_drafts.reconcile(isolated_store, fake)

    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_SENT


def test_a_paid_invoice_is_not_re_messaged_when_a_late_send_is_found(gmail, isolated_store):
    from app.store import STATE_RECOVERED

    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    isolated_store.transition("in_1", STATE_RECOVERED, reason="invoice.paid")
    fake.drafts.clear()
    fake.sent = [{"id": "m4", "subject": "", "to": "", "marker": _marker(isolated_store)}]

    gmail_drafts.reconcile(isolated_store, fake)

    assert isolated_store.get_failure("in_1")["state"] == STATE_RECOVERED


def test_an_expired_request_deletes_its_draft_so_it_cannot_be_sent_late(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    isolated_store._conn.execute("UPDATE approvals SET expires_at = ?", (past,))
    isolated_store._conn.commit()

    gmail_drafts.reconcile(isolated_store, fake)

    assert fake.deleted == ["r1"] and fake.drafts == {}
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_EXPIRED


def test_a_gmail_error_is_counted_and_never_marks_anything_sent(gmail, isolated_store):
    fake, _ = gmail
    loop.handle_payment_failed(_event(), isolated_store)

    def boom(draft_id):
        raise GmailError("get_draft_http_500")

    fake.draft_exists = boom
    out = gmail_drafts.reconcile(isolated_store, fake)

    assert out["errors"] == 1
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING
    assert isolated_store.sent_message_count("in_1") == 0


def test_sweep_runs_the_reconcile_only_in_gmail_mode(gmail, isolated_store, monkeypatch):
    loop.handle_payment_failed(_event(), isolated_store)
    assert approval.sweep(isolated_store)["gmail"]["checked"] == 1
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "email")
    assert approval.sweep(isolated_store)["gmail"] == {}


# -- the wire format ---------------------------------------------------------


def _client(handler):
    return GmailClient(httpx.Client(transport=httpx.MockTransport(handler)))


def test_wire_format_of_every_call(gmail):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, dict(req.url.params), req.headers.get("authorization")))
        if req.url.host == "oauth2.googleapis.com":
            form = dict(p.split("=", 1) for p in req.content.decode().split("&"))
            assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rtoken"
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if req.method == "POST" and req.url.path.endswith("/drafts"):
            return httpx.Response(200, json={"id": "d9"})
        if req.method == "GET" and req.url.path.endswith("/drafts/d9"):
            return httpx.Response(200, json={"id": "d9"})
        if req.method == "GET" and req.url.path.endswith("/drafts/gone"):
            return httpx.Response(404, json={})
        if req.method == "DELETE":
            return httpx.Response(204)
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        if req.url.path.endswith("/messages/m1"):
            return httpx.Response(200, json={"payload": {"headers": [
                {"name": "Subject", "value": "S"}, {"name": "To", "value": "a@b.test"},
                {"name": "X-PayPilot-Approval", "value": "7:abc"}]}})
        raise AssertionError(req.url)

    c = _client(handler)
    assert c.create_draft("RAW") == "d9"
    assert c.draft_exists("d9") is True
    assert c.draft_exists("gone") is False
    c.delete_draft("d9")
    got = c.sent_since(to="a@b.test", after_epoch=100)

    assert got == [{"id": "m1", "subject": "S", "to": "a@b.test", "marker": "7:abc"}]
    api_calls = [s_ for s_ in seen if s_[1].startswith("/gmail")]
    assert api_calls and all(s_[3] == "Bearer at" for s_ in api_calls)
    sent_q = next(q for m, p, q, a in seen if p.endswith("/messages"))
    assert sent_q["q"] == "in:sent to:a@b.test after:100"
    token_calls = [s for s in seen if s[1] == "/token"]
    assert len(token_calls) == 1, "the access token is cached across calls"


def test_a_failed_token_refresh_never_echoes_the_response(gmail):
    def handler(req):
        return httpx.Response(401, json={"error": "invalid_grant", "client": "cid"})

    with pytest.raises(GmailError) as exc:
        _client(handler).create_draft("RAW")
    assert str(exc.value) == "token_refresh_http_401"


def test_draft_creation_errors_carry_only_a_status(gmail):
    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(403, json={"error": "insufficientPermissions", "to": CUSTOMER})

    with pytest.raises(GmailError) as exc:
        _client(handler).create_draft("RAW")
    assert str(exc.value) == "create_draft_http_403"
    assert CUSTOMER not in str(exc.value)
