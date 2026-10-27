"""Regression tests for the independent review of M4 to M6. One section per finding."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import sqlite3
import sys
import time
from collections import OrderedDict
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import api as api_module
from app import approval, mailer, principals, tenant_admin
from app.api import app
from app.store import get_database, get_platform, reset_database, store_for
from tests.test_chargebee import _event as _cb_event
from tests.test_recharge import _charge

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def world(monkeypatch, isolated_store):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.setenv("PAYPILOT_TENANT_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("ADMIN_TOKEN", "boot")
    for name in ("STRIPE_WEBHOOK_SECRET", "RECHARGE_CLIENT_SECRET", "CHARGEBEE_WEBHOOK_SECRET",
                 "PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "PAYPILOT_SEND_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())
    reset_database()
    platform, db = get_platform(), get_database()
    for tid in ("alpha", "beta"):
        tenant_admin.create_tenant(platform, db, tid, tid.title())
        tenant_admin.rotate_secrets(db, tid, {
            "stripe_webhook_secret": f"whsec_{tid}", "recharge_client_secret": f"rc_{tid}",
            "chargebee_webhook_secret": f"cb_{tid}",
        })
    yield {"platform": platform, "db": db}
    reset_database()


def _cli():
    spec = importlib.util.spec_from_file_location("tenants_cli", ROOT / "scripts" / "tenants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- 1: the platform-wide unsigned flag never reaches a multi-mode tenant ----------------------


def test_the_legacy_url_in_multi_mode_ignores_the_unsigned_flag(world, monkeypatch):
    tenant_admin.create_tenant(world["platform"], world["db"], "default", "Default")
    monkeypatch.setenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "1")
    event = {"id": "evt_u", "type": "invoice.payment_failed",
             "data": {"object": {"id": "in_u", "customer": "c", "amount_due": 100, "currency": "eur",
                                 "customer_email": "o@acme.test"}}}
    r = client.post("/webhooks/stripe", content=json.dumps(event).encode())
    assert r.status_code == 400
    assert store_for("default").get_failure("in_u") is None


# -- 2 and 3: offboarding keeps the export; one door for platform_admin -----------------------


def test_an_unwritable_export_path_stops_the_offboarding_before_anything_is_erased(world, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    store_for("alpha").record_failure(invoice_id="in_1", customer_id="c1", amount_minor=100,
                                      currency="eur", failure_code="expired_card")
    cli = _cli()
    code = cli.main(["offboard", "alpha", "--yes-erase-everything",
                     "--export-to", str(tmp_path / "no" / "such" / "dir" / "x.json")])
    assert code == 1 and "Nothing was erased" in capsys.readouterr().err
    assert store_for("alpha").get_failure("in_1") is not None
    assert world["platform"].tenant_row("alpha")["status"] == "active"


def test_an_existing_export_file_is_never_overwritten(world, tmp_path, capsys):
    target = tmp_path / "keep.json"
    target.write_text("precious")
    assert _cli().main(["offboard", "alpha", "--yes-erase-everything", "--export-to", str(target)]) == 1
    assert target.read_text() == "precious"
    assert world["platform"].tenant_row("alpha")["status"] == "active"


def test_the_http_offboard_never_returns_the_export_and_wants_it_taken_first(world):
    root = principals.bootstrap(world["platform"]).raw
    h = {"Authorization": f"Bearer {root}"}
    r = client.post("/admin/tenants/alpha/offboard", headers=h, json={"confirm": True, "export_taken": True})
    assert r.status_code == 200 and r.json()["export"] is None


def test_the_operator_door_cannot_mint_a_platform_admin(world, monkeypatch, capsys):
    cli = _cli()
    assert cli.main(["token", "create", "alpha", "platform_admin"]) == 1
    principals.bootstrap(world["platform"])
    assert cli.main(["token", "create", "alpha", "platform_admin"]) == 1


def test_the_cli_bootstrap_needs_the_bootstrap_value_like_the_http_one(world, monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert _cli().main(["bootstrap"]) == 1
    assert "bootstrap_not_configured" in capsys.readouterr().err


# -- 4: an unknown tenant does the work a known one costs --------------------------------------


def test_unknown_and_malformed_tenants_do_the_padding_work(world, monkeypatch):
    calls = []
    monkeypatch.setattr(api_module, "_dummy_tenant_lookup", lambda: calls.append(1))
    client.post("/t/nosuch/webhooks/stripe", content=b"{}")
    client.post("/t/NO..SUCH/webhooks/stripe", content=b"{}")
    assert len(calls) == 2


# -- 5: offboarding clears the stores keyed by client id ---------------------------------------


def test_offboarding_erases_decision_rows_for_that_client_only(world):
    audit = api_module._get_decision_audit()
    for client_id in ("alpha", "beta"):
        audit.record({"invoice_id": "in_d", "client_id": client_id, "rule_fired": "r",
                      "input": {"x": 1}, "decision": {"y": 2}, "decided_at": "2026-10-27T00:00:00+00:00"})
    out = tenant_admin.offboard(world["platform"], store_for("alpha"), confirm=True, decision_audit=audit)
    assert out["other_stores"]["decision_audit"] == 1
    assert audit.for_invoice("in_d", "alpha") == []
    assert audit.for_invoice("in_d", "beta")


# -- 6: writes need a live tenant -------------------------------------------------------------


def test_the_cli_refuses_secret_and_platform_for_unknown_or_offboarded_tenants(world, monkeypatch, capsys):
    cli = _cli()
    monkeypatch.setattr(sys, "stdin", io.StringIO("whsec_x"))
    assert cli.main(["secret", "ghost", "stripe_webhook_secret"]) == 1
    assert cli.main(["platform", "ghost", "--send", "on", "--recipient", "*"]) == 1
    from app.tenant_config import has_stored_config

    assert not has_stored_config(world["db"], "ghost")
    tenant_admin.offboard(world["platform"], store_for("beta"), confirm=True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("whsec_x"))
    assert cli.main(["secret", "beta", "stripe_webhook_secret"]) == 1
    assert not has_stored_config(world["db"], "beta")


def test_create_refuses_to_adopt_a_preseeded_config_row(world):
    from app import tenant_config

    tenant_config.set_platform_fields(world["db"], "ghost", send_enabled=True, allowed_recipients=["*"])
    with pytest.raises(tenant_admin.LifecycleError) as e:
        tenant_admin.create_tenant(world["platform"], world["db"], "ghost", "Ghost")
    assert e.value.code == "config_exists_without_tenant"


def test_a_webhook_whose_tenant_is_offboarded_mid_flight_writes_nothing(world, monkeypatch):
    raw = json.dumps({"id": "evt_m", "type": "invoice.payment_failed", "data": {"object": {
        "id": "in_m", "customer": "c", "amount_due": 100, "currency": "eur",
        "customer_email": "o@acme.test", "attempt_count": 1,
        "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}}}}}).encode()
    ts = int(time.time())
    sig = hmac.new(b"whsec_alpha", f"{ts}".encode() + b"." + raw, hashlib.sha256).hexdigest()
    real = api_module._tenant_is_live
    monkeypatch.setattr(api_module, "_tenant_is_live", lambda t: False)  # offboarded after ctx was built
    r = client.post("/t/alpha/webhooks/stripe", content=raw, headers={"stripe-signature": f"t={ts},v1={sig}"})
    monkeypatch.setattr(api_module, "_tenant_is_live", real)
    assert r.status_code == 400 and store_for("alpha").get_failure("in_m") is None


# -- 7: suspension checks fail closed ---------------------------------------------------------


def test_an_unreadable_tenant_status_blocks_rather_than_allows(world, monkeypatch):
    from app import store as store_module

    def boom(self, tenant_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store_module.Platform, "tenant_status", boom)
    store = store_for("alpha")
    assert approval._tenant_blocked(store) is True
    assert mailer._tenant_suspended(store) is True


def test_a_store_double_without_a_database_is_not_blocked():
    class Double:
        tenant_id = "x"

    assert approval._tenant_blocked(Double()) is False and mailer._tenant_suspended(Double()) is False


# -- 8: the multi-mode background sweep -------------------------------------------------------


def test_the_multi_sweep_visits_every_tenant_with_an_open_request_and_survives_a_failure(world, monkeypatch):
    for tid in ("alpha", "beta"):
        store_for(tid).create_approval(invoice_id="in_s", channel="email", recipient="o@x.test", subject="s",
                                       body="b", token_hash="h", expires_at="2000-01-01T00:00:00")
    seen = []

    def fake_sweep(store):
        seen.append(store.tenant_id)
        if store.tenant_id == "alpha":
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(approval, "sweep", fake_sweep)
    assert approval.sweep_all_once(True) == ["beta"]
    assert seen == ["alpha", "beta"]


def test_the_background_sweep_starts_in_multi_mode(world, monkeypatch):
    started = []
    monkeypatch.setattr(approval, "_reconciler_started", False)
    monkeypatch.setattr(approval.threading, "Thread", lambda **kw: type("T", (), {"start": lambda s: started.append(1)})())
    assert approval.start_background_sweep() is True and started == [1]
    monkeypatch.setattr(approval, "_reconciler_started", False)


# -- 9: every provider's idempotency key carries the tenant ------------------------------------


def test_recharge_same_charge_in_two_tenants_is_processed_in_each(world):
    body = json.dumps(_charge(charge_id=7001)).encode()

    def post(tenant):
        sig = hashlib.sha256(f"rc_{tenant}".encode() + body).hexdigest()
        return client.post(f"/t/{tenant}/webhooks/recharge", content=body,
                           headers={"x-recharge-hmac-sha256": sig})

    a1, a2, b1 = post("alpha"), post("alpha"), post("beta")
    assert a1.status_code == a2.status_code == b1.status_code == 200, (a1.text, b1.text)
    assert a2.json().get("idempotent") is True
    assert not b1.json().get("idempotent")


def test_chargebee_same_event_in_two_tenants_is_processed_in_each(world):
    body = json.dumps(_cb_event(event_id="ev_shared")).encode()

    def post(tenant):
        return client.post(f"/t/{tenant}/webhooks/chargebee?key=cb_{tenant}", content=body)

    a1, a2, b1 = post("alpha"), post("alpha"), post("beta")
    assert a1.status_code == a2.status_code == b1.status_code == 200, (a1.text, b1.text)
    assert a2.json().get("idempotent") is True
    assert not b1.json().get("idempotent")


# -- minor: last_used is written at most once a minute ----------------------------------------


def test_last_used_is_not_written_on_every_request(world, monkeypatch):
    token = principals.issue_for_operator(world["platform"], role="tenant_viewer", tenant_id="alpha").raw
    writes = []
    real = type(world["platform"]).touch_token
    monkeypatch.setattr(type(world["platform"]), "touch_token", lambda self, t: (writes.append(t), real(self, t))[1])
    for _ in range(5):
        assert principals.resolve(world["platform"], f"Bearer {token}")
    assert len(writes) == 1
