"""The unified refusal view, on synthetic data, and the gate map checked against the code.

The map is the control, so the first test does not read the map to decide what the code can
write: it walks the source of the two modules that write refusals and collects the reason
codes from there. A map checked against a list somebody typed beside it can only agree with
itself.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app import mailer, refusals
from app.decision import handle_decision_request
from app.decision_audit import SqliteDecisionAudit

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _reasons_the_code_can_write() -> set[str]:
    """Reason codes written with status suppressed, read from the source of both modules."""
    found: set[str] = set()
    for name in ("mailer.py", "approval.py"):
        tree = ast.parse((APP / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.startswith("ERROR_") for t in node.targets
            ):
                if (v := _literal(node.value)) is not None:
                    found.add(v)
            if not isinstance(node, ast.Call):
                continue
            fn = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if fn in {"record_message", "_record"}:
                for kw in node.keywords:
                    if kw.arg == "error" and (v := _literal(kw.value)) is not None:
                        found.add(v)
            if fn == "_record_refusal" and len(node.args) >= 3:
                if (v := _literal(node.args[2])) is not None:
                    found.add(v)
        if name == "approval.py":
            for fn_def in ast.walk(tree):
                if isinstance(fn_def, ast.FunctionDef) and fn_def.name == "_recheck_before_sending":
                    for ret in ast.walk(fn_def):
                        if isinstance(ret, ast.Return) and ret.value is not None:
                            if (v := _literal(ret.value)) is not None:
                                found.add(v)
                            elif isinstance(ret.value, ast.JoinedStr):
                                head = _literal(ret.value.values[0])
                                if head:
                                    found.add(head + "x")  # a representative of the prefix
    return found


def test_the_control_finds_reasons_in_the_code_at_all():
    # A walker that returns nothing would make the completeness test pass on an empty set.
    found = _reasons_the_code_can_write()
    assert {"recipient_not_allowlisted", "approval_expired", "failed_output_guard"} <= found
    assert any(r.startswith("invoice_already_") for r in found)
    assert len(found) >= 12, sorted(found)


def test_every_reason_the_code_can_write_has_a_gate():
    missing = sorted(r for r in _reasons_the_code_can_write() if refusals.gate_for(r) is None)
    assert not missing, f"refusal reasons with no gate in app/refusals.py: {missing}"


def test_the_map_names_no_reason_the_code_cannot_write():
    # The other direction: a stale entry would claim a gate that no longer exists.
    written = _reasons_the_code_can_write()
    stale = sorted(r for r in refusals.REASON_GATES if r not in written)
    assert not stale, f"gate map entries the code never writes: {stale}"


def _enable_sending(monkeypatch, allowed="someone.else@test.example"):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", allowed)


def test_synthetic_run_lists_refusals_from_every_gate_in_both_stores(isolated_store, monkeypatch):
    audit = SqliteDecisionAudit(":memory:")
    tenant = isolated_store.tenant_id

    # Gate 1, the decision API: a body with a field it does not accept.
    handle_decision_request({"invoice_id": "SYN-1", "failure_code": "card_expired", "x": 1}, audit, tenant)

    # Gate 2, the recipient allowlist: sending is on, the address is not on the list.
    _enable_sending(monkeypatch)
    out = mailer.send_dunning_email(
        invoice_id="SYN-2", to="stranger@test.example", subject="s", body="b", store=isolated_store
    )
    assert out["status"] == mailer.STATUS_SUPPRESSED

    # Gate 3, the output guard: the provider call is never made, the guard refuses first.
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "ok@test.example")
    monkeypatch.setattr(
        mailer, "_post_to_resend", lambda *a, **k: (False, None, mailer.ERROR_FAILED_OUTPUT_GUARD)
    )
    mailer.send_dunning_email(
        invoice_id="SYN-3", to="ok@test.example", subject="s", body="b", store=isolated_store
    )

    view = refusals.unified(isolated_store, audit)
    by_invoice = {e["invoice_id"]: e for e in view["refusals"]}
    assert by_invoice["SYN-1"]["gate"] == "input_validated"
    assert by_invoice["SYN-1"]["source"] == "decision_ledger"
    assert by_invoice["SYN-2"]["gate"] == "recipient_allowlist"
    assert by_invoice["SYN-3"]["gate"] == "output_guard"
    assert by_invoice["SYN-2"]["source"] == by_invoice["SYN-3"]["source"] == "message_store"
    assert view["unclassified"] == 0
    assert view["decision_client_id"] == tenant


def test_a_dry_run_and_a_provider_failure_are_not_refusals(isolated_store, monkeypatch):
    audit = SqliteDecisionAudit(":memory:")
    # Sending off: a dry run, which is not a refusal.
    mailer.send_dunning_email(
        invoice_id="SYN-4", to="a@test.example", subject="s", body="b", store=isolated_store
    )
    # Sending on and allowed, but the provider fails: a failure, which is not a refusal.
    _enable_sending(monkeypatch, allowed="a@test.example")
    monkeypatch.setattr(mailer, "_post_to_resend", lambda *a, **k: (False, None, "http_500"))
    out = mailer.send_dunning_email(
        invoice_id="SYN-5", to="a@test.example", subject="s", body="b", store=isolated_store
    )
    assert out["status"] == mailer.STATUS_FAILED
    assert refusals.unified(isolated_store, audit)["refusals"] == []


def test_a_reason_the_map_does_not_know_is_listed_as_unclassified_not_dropped(isolated_store):
    isolated_store.record_message(invoice_id="SYN-6", status="suppressed", error="a_brand_new_reason")
    view = refusals.unified(isolated_store, SqliteDecisionAudit(":memory:"))
    assert view["unclassified"] == 1
    entry = view["refusals"][0]
    assert entry["gate"] == refusals.UNCLASSIFIED and entry["classified"] is False
    assert entry["reason"] == "a_brand_new_reason"


def test_the_route_serves_the_view(isolated_store):
    from fastapi.testclient import TestClient

    from app.api import app

    isolated_store.record_message(invoice_id="SYN-7", status="suppressed", error="approval_expired")
    body = TestClient(app).get("/refusals").json()
    assert body["refusals"][0]["gate"] == "approval_gate"
    assert set(body) == {"decision_client_id", "refusals", "unclassified"}


@pytest.mark.parametrize("reason", ["invoice_already_recovered", "invoice_already_messaged"])
def test_the_prefixed_recheck_reason_maps_to_its_gate(reason):
    assert refusals.gate_for(reason) == "approval_recheck"
