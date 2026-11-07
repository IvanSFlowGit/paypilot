"""The refusal ledger, step 1: a refused /decide body leaves a row.

What is pinned here, and why each is not obvious:

* the ledger holds a machine REASON CODE and never the rejected input or the message text,
  because an unknown field name is whatever the caller typed;
* a client reads only its own refusals;
* a refusal that cannot be recorded is still a 422, unlike a decision that cannot be
  recorded, which is a 503;
* every reason code the validator can raise is one the ledger accepts, so a new raise site
  cannot quietly produce a refusal the ledger cannot name.
"""

from __future__ import annotations

import pytest

from app import decision
from app.decision import (
    REFUSAL_REASONS,
    REFUSING_GATE,
    DecisionInputError,
    handle_decision_request,
    parse_input,
)
from app.decision_audit import SqliteDecisionAudit

OK = {"invoice_id": "INV-1", "failure_code": "card_expired"}


def _audit():
    return SqliteDecisionAudit(":memory:")


def test_a_refused_body_writes_one_row_and_returns_its_id():
    audit = _audit()
    status, body = handle_decision_request({**OK, "email": "a@b.c"}, audit, "client_a")
    assert status == 422
    rows = audit.refusals_for("client_a")
    assert len(rows) == 1
    assert body["refusal_id"] == rows[0]["id"]
    assert rows[0]["gate"] == REFUSING_GATE == "input_validated"
    assert rows[0]["reason"] == "unknown_field"
    assert rows[0]["invoice_id"] == "INV-1"


def test_the_ledger_never_holds_what_the_caller_typed():
    audit = _audit()
    secret = "4111111111111111"
    handle_decision_request({"invoice_id": secret + " bad id", "failure_code": "x"}, audit, "c")
    handle_decision_request({**OK, secret: 1}, audit, "c")  # a field NAME is caller-chosen
    stored = repr(audit.export_client("c")["decision_refusals"])
    assert secret not in stored
    rows = audit.refusals_for("c")
    assert {r["reason"] for r in rows} == {"invalid_invoice_id", "unknown_field"}
    # An invoice id that failed the validator is not stored, even though the body had one.
    assert all(r["invoice_id"] in ("", "INV-1") for r in rows)
    assert [r for r in rows if r["reason"] == "invalid_invoice_id"][0]["invoice_id"] == ""


def test_a_client_reads_only_its_own_refusals():
    audit = _audit()
    handle_decision_request({**OK, "x": 1}, audit, "client_a")
    handle_decision_request({**OK, "y": 1}, audit, "client_b")
    assert len(audit.refusals_for("client_a")) == 1
    assert len(audit.refusals_for("client_b")) == 1
    assert audit.refusals_for("client_c") == []


def test_a_good_body_writes_no_refusal():
    audit = _audit()
    status, _ = handle_decision_request(OK, audit, "client_a")
    assert status == 200
    assert audit.refusals_for("client_a") == []


class _RefusalWriteFails(SqliteDecisionAudit):
    def record_refusal(self, refusal):
        raise RuntimeError("store down")


def test_a_refusal_that_cannot_be_recorded_is_still_a_422_without_an_id():
    status, body = handle_decision_request({**OK, "x": 1}, _RefusalWriteFails(":memory:"), "c")
    assert status == 422
    assert "refusal_id" not in body
    assert body["error"] == "invalid_input"


def test_a_decision_that_cannot_be_recorded_is_still_a_503():
    class _DecisionWriteFails(SqliteDecisionAudit):
        def record(self, decision):
            raise RuntimeError("store down")

    status, body = handle_decision_request(OK, _DecisionWriteFails(":memory:"), "c")
    assert status == 503
    assert body["error"] == "audit_unavailable"


def test_the_ledger_has_no_update_or_delete_method():
    audit = _audit()
    names = {n for n in dir(audit) if "refusal" in n}
    assert names == {"record_refusal", "refusals_for"}, names


def test_client_export_and_erase_cover_the_ledger():
    audit = _audit()
    handle_decision_request({**OK, "x": 1}, audit, "client_a")
    handle_decision_request({**OK, "x": 1}, audit, "client_b")
    assert len(audit.export_client("client_a")["decision_refusals"]) == 1
    assert audit.erase_client("client_a")["decision_refusals"] == 1
    assert audit.refusals_for("client_a") == []
    assert len(audit.refusals_for("client_b")) == 1


@pytest.mark.parametrize(
    "body",
    [
        "not an object",
        {**OK, "extra": 1},
        {"invoice_id": "bad id!", "failure_code": "card_expired"},
        {"invoice_id": "INV-1", "failure_code": "Bad Code"},
        {**OK, "attempt": "two"},
        {**OK, "attempt": 0},
        {**OK, "prior_failures": True},
        {**OK, "prior_failures": 999},
    ],
)
def test_every_validator_refusal_carries_a_known_reason_code(body):
    with pytest.raises(DecisionInputError) as info:
        parse_input(body)
    assert info.value.code in REFUSAL_REASONS


def test_a_raise_site_with_an_unknown_code_is_refused_at_construction():
    with pytest.raises(ValueError, match="unknown refusal reason"):
        DecisionInputError("x", "made_up_reason")


def test_the_outcome_parser_codes_are_known_too():
    for bad in ("nope", {"outcome": "refunded"}, {"outcome": "paid", "report_id": "bad id"},
                {"outcome": "paid", "nope": 1}):
        with pytest.raises(DecisionInputError) as info:
            decision.parse_outcome_input(bad)
        assert info.value.code in REFUSAL_REASONS
