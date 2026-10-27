"""M5: per-tenant webhooks, one failure body, tenant-keyed idempotency, suspension semantics."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import tenant_admin
from app.api import app
from app.store import get_database, get_platform, reset_database, store_for

client = TestClient(app, raise_server_exceptions=False)
SECRETS = {"alpha": "whsec_alpha_secret", "beta": "whsec_beta_secret"}


def _invoice_event(invoice_id="in_1", etype="invoice.payment_failed", event_id=None):
    return {
        "id": event_id or f"evt_{invoice_id}_{etype}",
        "type": etype,
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": "cus_XYZ",
            "subscription": "sub_XYZ", "customer_email": "owner@acme.test",
            "amount_due": 4900, "currency": "eur", "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


def _stripe_headers(payload: bytes, secret: str) -> dict:
    ts = int(time.time())
    v1 = hmac.new(secret.encode(), f"{ts}".encode() + b"." + payload, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={ts},v1={v1}", "content-type": "application/json"}


def _post_stripe(path, event, secret):
    raw = json.dumps(event).encode()
    headers = {} if secret is None else _stripe_headers(raw, secret)
    return client.post(path, content=raw, headers=headers)


@pytest.fixture
def multi(monkeypatch, isolated_store):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.setenv("PAYPILOT_TENANT_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    for name in ("STRIPE_WEBHOOK_SECRET", "RECHARGE_CLIENT_SECRET", "CHARGEBEE_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    reset_database()
    platform, db = get_platform(), get_database()
    for tid in ("alpha", "beta", "demo"):
        tenant_admin.create_tenant(platform, db, tid, tid.title())
    for tid, sec in SECRETS.items():
        tenant_admin.rotate_secrets(db, tid, {
            "stripe_webhook_secret": sec,
            "recharge_client_secret": sec + "_r",
            "chargebee_webhook_secret": sec + "_c",
        })
    yield {"platform": platform, "db": db}
    reset_database()


# -- one failure body --------------------------------------------------------


def _all_failures(multi):
    """Every way a delivery can fail to authenticate, as (label, response)."""
    ev = _invoice_event()
    tenant_admin.create_tenant(multi["platform"], multi["db"], "gamma", "Gamma")  # no secret stored
    tenant_admin.create_tenant(multi["platform"], multi["db"], "delta", "Delta")
    tenant_admin.offboard(multi["platform"], store_for("delta"), confirm=True)
    return {
        "unknown tenant": _post_stripe("/t/nosuch/webhooks/stripe", ev, "whsec_x"),
        "bad format": _post_stripe("/t/NO..SUCH/webhooks/stripe", ev, "whsec_x"),
        "offboarded": _post_stripe("/t/delta/webhooks/stripe", ev, "whsec_x"),
        "bad signature": _post_stripe("/t/alpha/webhooks/stripe", ev, "whsec_wrong"),
        "missing signature": _post_stripe("/t/alpha/webhooks/stripe", ev, None),
        "unconfigured secret": _post_stripe("/t/gamma/webhooks/stripe", ev, "whsec_x"),
        "other tenant's secret": _post_stripe("/t/alpha/webhooks/stripe", ev, SECRETS["beta"]),
    }


def test_every_authentication_failure_is_the_same_status_and_body(multi):
    results = _all_failures(multi)
    shapes = {label: (r.status_code, r.text) for label, r in results.items()}
    assert len(set(shapes.values())) == 1, shapes
    assert set(shapes.values()) == {(400, '{"detail":"Invalid webhook"}')}


def test_recharge_and_chargebee_failures_are_uniform_too(multi):
    body = json.dumps({"charge": {"id": 1, "error_type": "CARD_DECLINED"}}).encode()
    out = {
        client.post(u, content=body, headers=h).text
        for u, h in (
            ("/t/nosuch/webhooks/recharge", {}),
            ("/t/alpha/webhooks/recharge", {"x-recharge-hmac-sha256": "00"}),
            ("/t/nosuch/webhooks/chargebee", {}),
            ("/t/alpha/webhooks/chargebee?key=wrong", {}),
        )
    }
    assert out == {'{"detail":"Invalid webhook"}'}


# -- delivery lands in the right ledger -------------------------------------


def test_a_signed_delivery_lands_in_that_tenants_ledger_only(multi):
    r = _post_stripe("/t/alpha/webhooks/stripe", _invoice_event("in_a"), SECRETS["alpha"])
    assert r.status_code == 200, r.text
    assert store_for("alpha").get_failure("in_a") is not None
    assert store_for("beta").get_failure("in_a") is None


def test_a_tenant_secret_does_not_verify_at_another_tenants_url(multi):
    r = _post_stripe("/t/beta/webhooks/stripe", _invoice_event("in_x"), SECRETS["alpha"])
    assert r.status_code == 400
    assert store_for("beta").get_failure("in_x") is None
    assert store_for("alpha").get_failure("in_x") is None


# -- idempotency carries the tenant -----------------------------------------


def test_the_same_event_id_is_processed_once_per_tenant_not_once_overall(multi):
    ev = _invoice_event("in_same", event_id="evt_shared")
    a1 = _post_stripe("/t/alpha/webhooks/stripe", ev, SECRETS["alpha"])
    a2 = _post_stripe("/t/alpha/webhooks/stripe", ev, SECRETS["alpha"])
    b1 = _post_stripe("/t/beta/webhooks/stripe", ev, SECRETS["beta"])
    assert a1.status_code == a2.status_code == b1.status_code == 200
    assert a2.json().get("idempotent") is True
    assert not b1.json().get("idempotent"), "tenant B was handed tenant A's cached response"
    assert store_for("alpha").get_failure("in_same") is not None
    assert store_for("beta").get_failure("in_same") is not None


def test_idempotency_keys_for_every_provider_include_the_tenant():
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("_idem_get", "_idem_put"):
            key = node.args[0]
            if isinstance(key, ast.JoinedStr):
                text = ast.unparse(key)
                if any(p in text for p in ("stripe:", "recharge:", "chargebee:")):
                    assert "ctx.tag" in text, text


# -- unsigned only for demo, in code ----------------------------------------


def test_unsigned_is_accepted_for_the_demo_tenant_only(multi, monkeypatch):
    monkeypatch.setenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "1")
    ev = _invoice_event("in_demo")
    assert _post_stripe("/t/demo/webhooks/stripe", ev, None).status_code == 200
    # a non-demo tenant with NO stored secret is refused, flag or no flag
    tenant_admin.create_tenant(multi["platform"], multi["db"], "nosecret", "No Secret")
    assert _post_stripe("/t/nosecret/webhooks/stripe", ev, None).status_code == 400
    # and a tenant WITH a secret still refuses an unsigned delivery
    assert _post_stripe("/t/alpha/webhooks/stripe", ev, None).status_code == 400


# -- suspension --------------------------------------------------------------


def test_a_suspended_tenant_drops_dunning_events_but_applies_closing_ones(multi):
    # a failure recorded while active
    assert _post_stripe("/t/alpha/webhooks/stripe", _invoice_event("in_open"), SECRETS["alpha"]).status_code == 200
    assert store_for("alpha").get_failure("in_open")["state"] != "recovered"
    out = tenant_admin.suspend(multi["platform"], store_for("alpha"), "unpaid invoice")
    assert out["status"] == "suspended"

    # dunning event: acknowledged, dropped, audited
    dropped = _post_stripe("/t/alpha/webhooks/stripe", _invoice_event("in_new"), SECRETS["alpha"])
    assert dropped.status_code == 200
    assert dropped.json() == {"received": True, "handled": False, "reason": "tenant_suspended"}
    assert store_for("alpha").get_failure("in_new") is None

    # closing event: still applied, so the ledger stays true and nobody paid is dunned
    paid = _post_stripe(
        "/t/alpha/webhooks/stripe", _invoice_event("in_open", "invoice.paid", "evt_paid_1"), SECRETS["alpha"]
    )
    assert paid.status_code == 200
    assert store_for("alpha").get_failure("in_open")["state"] == "recovered"

    tenant_admin.resume(multi["platform"], "alpha")
    again = _post_stripe("/t/alpha/webhooks/stripe", _invoice_event("in_after"), SECRETS["alpha"])
    assert again.status_code == 200 and store_for("alpha").get_failure("in_after") is not None


def test_suspending_expires_pending_approvals_and_a_resume_does_not_revive_them(multi):
    store = store_for("alpha")
    aid = store.create_approval(
        invoice_id="in_p", channel="email", recipient="o@acme.test", subject="s", body="b",
        token_hash="h", expires_at="2999-01-01T00:00:00",
    )
    out = tenant_admin.suspend(multi["platform"], store, "test")
    assert out["approvals_expired"] == 1
    assert store.get_approval(aid)["status"] == "expired"
    tenant_admin.resume(multi["platform"], "alpha")
    assert store.get_approval(aid)["status"] == "expired"


def test_decide_and_the_mailer_refuse_a_suspended_tenant(multi, monkeypatch):
    from app import approval, mailer

    store = store_for("alpha")
    store.create_approval(
        invoice_id="in_q", channel="email", recipient="o@acme.test", subject="s", body="b",
        token_hash=approval.hash_token("tok"), expires_at="2999-01-01T00:00:00",
    )
    tenant_admin.suspend(multi["platform"], store, "test")
    # the approval was already expired by suspension; a fresh one made by hand is also refused
    aid2 = store.create_approval(
        invoice_id="in_r", channel="email", recipient="o@acme.test", subject="s", body="b",
        token_hash=approval.hash_token("tok"), expires_at="2999-01-01T00:00:00",
    )
    assert approval.decide(aid2, "tok", "approve", store)["outcome"] == "suspended"
    assert store.get_approval(aid2)["status"] == "expired"

    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    sent = []
    monkeypatch.setattr("httpx.post", lambda *a, **k: sent.append(1))
    res = mailer.send_dunning_email(invoice_id="in_s", to="o@acme.test", subject="s", body="b", store=store)
    assert res["status"] == mailer.STATUS_SUPPRESSED and res["error"] == "tenant_suspended"
    assert sent == []


# -- legacy URL and single mode ---------------------------------------------


def test_the_legacy_url_in_multi_mode_answers_uniformly_without_a_default_tenant(multi):
    r = _post_stripe("/webhooks/stripe", _invoice_event(), "whsec_x")
    assert (r.status_code, r.text) == (400, '{"detail":"Invalid webhook"}')


def test_the_legacy_url_resolves_to_the_default_tenant_with_its_environment_secret(multi, monkeypatch):
    tenant_admin.create_tenant(multi["platform"], multi["db"], "default", "Default")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_env")
    r = _post_stripe("/webhooks/stripe", _invoice_event("in_leg"), "whsec_env")
    assert r.status_code == 200, r.text
    assert store_for("default").get_failure("in_leg") is not None


def test_tenant_webhook_urls_do_not_exist_in_single_mode(isolated_store):
    r = client.post("/t/alpha/webhooks/stripe", content=b"{}")
    assert r.status_code == 404
