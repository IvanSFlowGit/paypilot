"""Tests for the Stripe webhook path: signature verification + event mapping.

All offline: no OpenAI key (mock mode) and no network. Signatures are computed
locally with the same HMAC scheme the verifier checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import stripe_map


@pytest.fixture
def no_key(monkeypatch):
    """Force the keyless demo path so run_recovery works offline."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    return monkeypatch


def _stripe_event(
    code="expired_card", customer="cust_001", amount_due=149900, attempt=1,
    etype="invoice.payment_failed",
):
    return {
        "id": "evt_test_123",
        "type": etype,
        "data": {
            "object": {
                "object": "invoice",
                "customer": "cus_XYZ",
                "amount_due": amount_due,
                "currency": "usd",
                "attempt_count": attempt,
                "payment_intent": {"last_payment_error": {"decline_code": code}},
                "metadata": {"paypilot_customer_id": customer},
            }
        },
    }


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    signed = f"{ts}".encode() + b"." + payload
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def test_signature_valid():
    payload = b'{"hello":"world"}'
    header = _sign(payload, "whsec_test")
    assert stripe_map.verify_stripe_signature(payload, header, "whsec_test") is True


def test_signature_rejects_tampered_payload():
    header = _sign(b'{"hello":"world"}', "whsec_test")
    assert stripe_map.verify_stripe_signature(b'{"hello":"evil"}', header, "whsec_test") is False


def test_signature_rejects_wrong_secret():
    payload = b'{"a":1}'
    header = _sign(payload, "whsec_test")
    assert stripe_map.verify_stripe_signature(payload, header, "whsec_other") is False


def test_signature_rejects_missing_header_or_secret():
    assert stripe_map.verify_stripe_signature(b"x", "", "whsec_test") is False
    assert stripe_map.verify_stripe_signature(b"x", "t=1,v1=abc", "") is False


def test_signature_rejects_stale_timestamp():
    payload = b'{"a":1}'
    header = _sign(payload, "whsec_test", ts=1)  # far in the past
    assert stripe_map.verify_stripe_signature(payload, header, "whsec_test", tolerance=300) is False


# ---------------------------------------------------------------------------
# Event mapping
# ---------------------------------------------------------------------------

def test_maps_expired_card_and_cents():
    internal = stripe_map.stripe_event_to_internal(_stripe_event(code="expired_card", amount_due=149900))
    assert internal["failure_code"] == "card_expired"
    assert internal["amount"] == 1499.0  # cents -> decimal
    assert internal["currency"] == "usd"


def test_maps_customer_via_metadata_hint():
    internal = stripe_map.stripe_event_to_internal(_stripe_event(customer="cust_003"))
    assert internal["customer_id"] == "cust_003"  # metadata wins over the cus_ id


def test_maps_attempt_count():
    internal = stripe_map.stripe_event_to_internal(_stripe_event(attempt=3))
    assert internal["attempt"] == 3


def test_unknown_decline_code_defaults_to_generic():
    internal = stripe_map.stripe_event_to_internal(_stripe_event(code="some_new_code"))
    assert internal["failure_code"] == "generic_decline"


def test_mapping_is_empty_safe():
    internal = stripe_map.stripe_event_to_internal({})
    assert internal["failure_code"] == "generic_decline"
    assert internal["amount"] == 0.0
    assert internal["attempt"] == 1


def test_extracts_code_from_finalization_error():
    event = {
        "type": "invoice.payment_failed",
        "data": {"object": {"last_finalization_error": {"code": "insufficient_funds"}, "amount_due": 5000}},
    }
    internal = stripe_map.stripe_event_to_internal(event)
    assert internal["failure_code"] == "insufficient_funds"
    assert internal["amount"] == 50.0


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

def test_webhook_runs_recovery_without_secret(no_key, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    client = TestClient(api_module.app)
    r = client.post("/webhooks/stripe", json=_stripe_event(customer="cust_001", attempt=3))
    assert r.status_code == 200
    body = r.json()
    assert body["received"] is True and body["handled"] is True
    assert set(body["recovery"]) == {"diagnosis", "risk", "strategy", "schedule", "message", "impact"}
    assert body["recovery"]["risk"]["churn_risk"] == "high"  # attempt 3 escalates


def test_webhook_is_idempotent_on_event_id(no_key, monkeypatch):
    """A retried Stripe event (same id) replays the stored result, flagged idempotent."""
    from collections import OrderedDict

    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())
    client = TestClient(api_module.app)
    event = _stripe_event(customer="cust_001")

    first = client.post("/webhooks/stripe", json=event)
    second = client.post("/webhooks/stripe", json=event)
    assert first.status_code == 200 and second.status_code == 200
    assert "idempotent" not in first.json()
    assert second.json().get("idempotent") is True
    assert second.json()["recovery"] == first.json()["recovery"]


def test_webhook_acknowledges_other_event_types(no_key, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    client = TestClient(api_module.app)
    r = client.post("/webhooks/stripe", json=_stripe_event(etype="customer.created"))
    assert r.status_code == 200
    assert r.json() == {"received": True, "handled": False}


def test_webhook_rejects_invalid_json(no_key, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    client = TestClient(api_module.app)
    r = client.post("/webhooks/stripe", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_webhook_verifies_signature_when_secret_set(no_key, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    client = TestClient(api_module.app)
    payload = json.dumps(_stripe_event()).encode()

    bad = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert bad.status_code == 400

    good = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": _sign(payload, "whsec_test"), "content-type": "application/json"},
    )
    assert good.status_code == 200
    assert good.json()["handled"] is True
