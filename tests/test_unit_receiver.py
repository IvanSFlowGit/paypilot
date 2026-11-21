"""The Unit receiver: signature, decisions recorded before the answer, every refusal.

SYNTHETIC BODIES ONLY, shaped like Unit's published pages. These tests prove the plumbing and the
refusals. They do not prove a real Unit event carries a reason: no live payload has been seen.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app.decision_audit import SqliteDecisionAudit
from app.unit_receiver import handle_unit_webhook

SECRET = "test-secret-not-a-real-one"


def ev(eid, pid, reason=None, etype="payment.returned"):
    attrs = {"createdAt": "2026-11-01T10:00:00.000Z", "amount": 2500, "direction": "Debit"}
    if reason:
        attrs["reason"] = reason
    return {
        "id": eid,
        "type": etype,
        "attributes": attrs,
        "relationships": {"payment": {"data": {"type": "payment", "id": pid}}},
    }


def body(*events):
    return json.dumps({"data": list(events)}).encode()


def sign(raw, secret=SECRET):
    return base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha1).digest()).decode()


class Audit:
    def __init__(self, fail=False, fail_lookup=False):
        self.rows, self.fail, self.fail_lookup = [], fail, fail_lookup

    def record(self, decision):
        if self.fail:
            raise RuntimeError("disk full")
        self.rows.append(decision)
        return f"a{len(self.rows)}"

    def has_decision(self, invoice_id, client_id):
        if self.fail or self.fail_lookup:
            raise RuntimeError("disk full")
        return any(r["invoice_id"] == invoice_id and r["client_id"] == client_id for r in self.rows)


def call(raw, sig="auto", secret=SECRET, required=True, audit=None, rejects=None):
    audit = audit if audit is not None else Audit()
    sig = sign(raw) if sig == "auto" else sig
    out = handle_unit_webhook(
        raw,
        sig,
        secret,
        required,
        audit,
        "acme",
        on_reject=lambda e, d: (rejects if rejects is not None else []).append(e),
    )
    return out, audit


def test_a_signed_return_is_decided_and_recorded_before_the_answer():
    (status, out), audit = call(body(ev("1", "501", "InsufficientFunds")))
    assert status == 200 and out["decided"] == 1 and out["acted"] is False
    row = audit.rows[0]
    assert (
        row["invoice_id"] == "501" and row["client_id"] == "acme" and row["input"]["code"] == "R01"
    )
    assert row["acted"] is False and out["audit_ids"] == ["a1"]


def test_a_return_with_no_reason_is_recorded_as_held_for_a_person():
    (status, out), audit = call(body(ev("2", "502")))
    assert (
        status == 200 and out["held_for_a_person"] == 1 and audit.rows[0]["person_required"] is True
    )
    assert audit.rows[0]["rule_fired"] == "unit_ach_held"
    assert audit.rows[0]["input"]["payload_shape"]



def test_an_unknown_reason_name_is_held_not_guessed():
    (_, out), audit = call(body(ev("3", "503", "SomethingNewUnitInvented")))
    assert out["held_for_a_person"] == 1 and audit.rows[0]["input"]["code"] is None


def test_a_non_return_event_is_counted_and_not_recorded():
    (status, out), audit = call(body(ev("4", "504", "InsufficientFunds", etype="payment.created")))
    assert status == 200 and out["not_returns"] == 1 and audit.rows == []


def test_one_delivery_with_three_events_records_each_return():
    (_, out), audit = call(
        body(
            ev("5", "1", "InsufficientFunds"), ev("6", "2"), ev("7", "3", "x", etype="payment.sent")
        )
    )
    assert (
        out["decided"] == 1
        and out["held_for_a_person"] == 1
        and out["not_returns"] == 1
        and len(audit.rows) == 2
    )


def test_a_retried_delivery_is_acknowledged_and_recorded_once():
    raw = body(ev("9", "900", "InsufficientFunds"))
    audit = Audit()
    for _ in range(7):  # Unit retried each event 7 times within seconds
        (status, out), _a = call(raw, audit=audit)
        assert status == 200
    assert len(audit.rows) == 1 and out["duplicates"] == 1 and out["decided"] == 0


def test_a_second_event_for_the_same_payment_in_one_delivery_is_not_recorded_twice():
    (_, out), audit = call(body(ev("11", "55", "InsufficientFunds"), ev("12", "55", "InsufficientFunds")))
    assert len(audit.rows) == 1 and out["decided"] == 1 and out["duplicates"] == 1


def test_a_different_client_does_not_hide_a_payment_from_dedupe():
    raw = body(ev("13", "66", "InsufficientFunds"))
    audit = Audit()
    handle_unit_webhook(raw, sign(raw), SECRET, True, audit, "acme")
    (status, out) = handle_unit_webhook(raw, sign(raw), SECRET, True, audit, "other-client")
    assert status == 200 and len(audit.rows) == 2 and out["duplicates"] == 0


def test_dedupe_lookup_failure_is_503_not_a_silent_second_row():
    # Only the lookup fails: record() would succeed, so a swallowed lookup error would write a row.
    (status, out), audit = call(body(ev("14", "77", "InsufficientFunds")), audit=Audit(fail_lookup=True))
    assert status == 503 and audit.rows == []


def test_a_wrong_signature_is_400_records_nothing_and_alerts():
    raw, rej = body(ev("8", "9", "InsufficientFunds")), []
    (status, out), audit = call(raw, sig=sign(raw, "another-secret"), rejects=rej)
    assert (
        status == 400
        and out["error"] == "bad_signature"
        and audit.rows == []
        and rej == ["unit_webhook_signature_rejected"]
    )


def test_a_missing_signature_is_400():
    (status, _), audit = call(body(ev("9", "9", "InsufficientFunds")), sig=None)
    assert status == 400 and audit.rows == []


def test_no_secret_and_signatures_required_fails_closed():
    rej = []
    (status, out), audit = call(
        body(ev("1", "1", "InsufficientFunds")), secret="", required=True, rejects=rej
    )
    assert (
        status == 400
        and out["error"] == "unsigned_not_accepted"
        and audit.rows == []
        and rej == ["unit_webhook_unsigned_refused"]
    )


def test_no_secret_is_accepted_only_when_unsigned_is_explicitly_allowed():
    (status, _), audit = call(body(ev("1", "1", "InsufficientFunds")), secret="", required=False)
    assert status == 200 and len(audit.rows) == 1


def test_a_failed_write_is_503_so_unit_retries_and_nothing_is_acked():
    (status, out), _ = call(body(ev("1", "1", "InsufficientFunds")), audit=Audit(fail=True))
    assert status == 503 and out["error"] == "audit_unavailable"


def test_bad_payloads_are_400_and_oversize_is_413():
    assert call(b"not json")[0][0] == 400
    assert call(b'{"data": []}')[0][0] == 400
    assert call(b"x" * 5_000_001)[0][0] == 413


def test_the_signature_is_checked_before_the_body_is_parsed():
    raw = b"not json"
    (status, out), _ = call(raw, sig=sign(raw, "wrong"))
    assert out["error"] == "bad_signature"


@pytest.fixture
def client(monkeypatch):
    audit = SqliteDecisionAudit(":memory:")
    monkeypatch.setattr(api_module, "_decision_audit", audit)
    monkeypatch.setenv("UNIT_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    return TestClient(api_module.app), audit


def test_over_http_a_signed_delivery_is_recorded_and_readable_by_payment_id(client):
    c, audit = client
    raw = body(ev("1", "777", "InsufficientFunds"))
    r = c.post("/webhooks/unit", content=raw, headers={"x-unit-signature": sign(raw)})
    assert r.status_code == 200 and r.json()["decided"] == 1
    assert audit.for_invoice("777", r.json() and audit_client(audit))


def audit_client(audit):
    return audit._conn.execute("select client_id from decision_audit limit 1").fetchone()[0]


def test_over_http_seven_retries_of_one_signed_return_write_one_row(client):
    c, audit = client
    raw = body(ev("21", "888", "InsufficientFunds"))
    for _ in range(7):
        r = c.post("/webhooks/unit", content=raw, headers={"x-unit-signature": sign(raw)})
        assert r.status_code == 200
    n = audit._conn.execute("select count(*) from decision_audit where invoice_id = '888'").fetchone()[0]
    assert n == 1


def test_over_http_an_unsigned_delivery_is_refused(client):
    c, audit = client
    r = c.post("/webhooks/unit", content=body(ev("1", "1", "InsufficientFunds")))
    assert r.status_code == 400
    assert audit._conn.execute("select count(*) from decision_audit").fetchone()[0] == 0


def test_over_http_the_demo_unsigned_flag_does_not_open_the_unit_route(client, monkeypatch):
    c, audit = client
    monkeypatch.setenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "1")
    monkeypatch.delenv("UNIT_WEBHOOK_SECRET", raising=False)
    r = c.post("/webhooks/unit", content=body(ev("1", "1", "InsufficientFunds")))
    assert r.status_code == 400 and r.json()["error"] == "unsigned_not_accepted"
    assert audit._conn.execute("select count(*) from decision_audit").fetchone()[0] == 0


def test_a_decided_return_records_the_customer_message_and_a_held_one_records_none():
    (_, _), audit = call(body(ev("1", "601", "InsufficientFunds")))
    assert audit.rows[0]["customer_message"].strip() != ""
    (_, _), held = call(body(ev("2", "602")))
    assert held.rows[0]["customer_message"] == ""


# --- approving a held return ------------------------------------------------

ADMIN = "approve-route-admin-token"


@pytest.fixture
def approver(client, monkeypatch):
    c, audit = client
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)

    def hold(pid="HELD-1", reason=None):
        raw = body(ev(f"e-{pid}", pid, reason))
        r = c.post("/webhooks/unit", content=raw, headers={"x-unit-signature": sign(raw)})
        assert r.status_code == 200
        return r.json()

    def approve(pid, code, token=ADMIN):
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return c.post(f"/returns/held/{pid}/approve", json={"code": code}, headers=headers)

    return c, audit, hold, approve


def _rows(audit, pid):
    return audit.for_invoice(pid, audit_client(audit))


def test_approving_a_held_return_appends_the_table_decision_and_keeps_the_hold(approver):
    _, audit, hold, approve = approver
    assert hold()["held_for_a_person"] == 1
    r = approve("HELD-1", "r03")
    assert r.status_code == 200 and r.json()["approved"] is True and r.json()["acted"] is False
    rows = _rows(audit, "HELD-1")
    assert [x["rule_fired"] for x in rows][-1] == "unit_ach_held"          # the hold is kept
    newest = rows[0]["decision"]
    assert newest["status"] == "decided" and newest["rule_fired"].startswith("unit_ach_R03_row")
    assert newest["input"]["source"] == "person_approval" and newest["acted"] is False
    assert newest["customer_message"] and newest["customer_message"] != ""


def test_a_second_approval_is_refused_and_writes_nothing(approver):
    _, audit, hold, approve = approver
    hold()
    assert approve("HELD-1", "R03").status_code == 200
    r = approve("HELD-1", "R01")
    assert r.status_code == 409 and r.json()["error"] == "already_decided"
    assert len(_rows(audit, "HELD-1")) == 2


def test_only_a_held_return_of_this_client_can_be_approved(approver):
    c, audit, hold, approve = approver
    raw = body(ev("e-ok", "DECIDED-1", "InsufficientFunds"))
    c.post("/webhooks/unit", content=raw, headers={"x-unit-signature": sign(raw)})
    assert approve("DECIDED-1", "R01").status_code == 404             # decided, not held
    assert approve("NEVER-SEEN", "R01").status_code == 404
    audit.record({"invoice_id": "FOREIGN-1", "client_id": "someone-else", "rule_fired": "unit_ach_held",
                  "decided_at": "2026-11-01T00:00:00+00:00", "input": {}, "status": "held"})
    assert approve("FOREIGN-1", "R01").status_code == 404             # another client's hold
    assert len(audit.for_invoice("FOREIGN-1", "someone-else")) == 1


@pytest.mark.parametrize("code,status", [("X1", 422), ("", 422), (3, 422), ("R99", 422), ("R03; drop", 422)])
def test_a_bad_or_unlisted_code_is_refused_and_writes_nothing(approver, code, status):
    _, audit, hold, approve = approver
    hold()
    assert approve("HELD-1", code).status_code == status
    assert len(_rows(audit, "HELD-1")) == 1


def test_the_route_fails_closed_without_a_token_and_refuses_a_wrong_one(approver, monkeypatch):
    c, audit, hold, approve = approver
    hold()
    assert approve("HELD-1", "R03", token="wrong").status_code in (401, 403)
    assert approve("HELD-1", "R03", token=None).status_code in (401, 403)
    monkeypatch.delenv("ADMIN_TOKEN")
    r = approve("HELD-1", "R03", token=None)
    assert r.status_code == 503 and r.json()["error"] == "admin_token_not_configured"
    assert len(_rows(audit, "HELD-1")) == 1


def test_the_approved_row_is_the_newest_so_a_lookup_returns_the_decision(approver):
    _, audit, hold, approve = approver
    hold()
    approve("HELD-1", "R02")
    assert _rows(audit, "HELD-1")[0]["rule_fired"].startswith("unit_ach_R02_row")
