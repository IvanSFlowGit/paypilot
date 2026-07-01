"""Tests for the free demo (mock) mode and the API security controls.

Unlike test_graph.py - which mocks the LLM/retriever seams - these tests exercise
the *real* offline path: with no ``OPENAI_API_KEY`` set, ``get_llm`` returns the
deterministic mock model and ``get_retriever`` returns the lexical retriever, so
the whole flow runs with no key and no network. The security tests cover input
validation, the per-IP rate limit, and the response hardening headers.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import graph as graph_module
from app import nodes as nodes_module


@pytest.fixture
def no_key(monkeypatch):
    """Ensure the process looks key-less so the demo (mock) path is active."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # The retriever is a lazy singleton; reset it so it rebuilds as the lexical one.
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    return monkeypatch


# ---------------------------------------------------------------------------
# Mock / demo mode
# ---------------------------------------------------------------------------

def test_use_mock_true_without_key(no_key):
    assert nodes_module.use_mock() is True
    assert isinstance(nodes_module.get_llm(), nodes_module._MockLLM)


def test_mock_flow_is_grounded_and_offline(no_key):
    """The real offline flow produces a grounded, plan-aware payload with no key."""
    event = {
        "customer_id": "cust_001",  # Acme Robotics, Scale
        "amount": 1499.0,
        "currency": "usd",
        "failure_code": "card_expired",
        "attempt": 1,
    }

    output = graph_module.run_recovery(event)

    assert set(output) == {"diagnosis", "strategy", "message", "impact"}
    # Mock copy is grounded in the customer record and the failure reason.
    assert "Acme Robotics" in output["message"]
    assert "Scale" in output["message"]
    assert "expired" in output["diagnosis"].lower()
    # Strategy still comes from the deterministic rules table.
    assert output["strategy"]["action"] == "request_card_update"


def test_impact_quantifies_revenue(no_key):
    """The impact block turns a failed charge into money math."""
    event = {
        "customer_id": "cust_003",  # Nimbus Health, MRR 4200
        "amount": 4200.0,
        "currency": "usd",
        "failure_code": "card_expired",
        "attempt": 1,
    }
    impact = graph_module.run_recovery(event)["impact"]

    assert impact["amount_at_risk"] == 4200.0
    assert impact["currency"] == "USD"
    assert impact["recovery_likelihood"] == 0.70  # card_expired rate
    assert impact["expected_recovered"] == round(4200.0 * 0.70, 2)
    assert impact["annual_value_at_risk"] == 4200.0 * 12  # MRR annualised


def test_impact_rate_varies_by_code(no_key):
    base = {"customer_id": "cust_001", "amount": 100.0, "currency": "usd", "attempt": 1}
    expired = graph_module.run_recovery({**base, "failure_code": "card_expired"})["impact"]
    decline = graph_module.run_recovery({**base, "failure_code": "generic_decline"})["impact"]
    assert expired["recovery_likelihood"] > decline["recovery_likelihood"]


def test_mock_diagnosis_differs_by_failure_code(no_key):
    """Each failure code yields its own diagnosis template."""
    base = {"customer_id": "cust_001", "amount": 1.0, "currency": "usd", "attempt": 1}
    expired = graph_module.run_recovery({**base, "failure_code": "card_expired"})
    funds = graph_module.run_recovery({**base, "failure_code": "insufficient_funds"})
    assert expired["diagnosis"] != funds["diagnosis"]
    assert "funds" in funds["diagnosis"].lower()


# ---------------------------------------------------------------------------
# Security: validation, rate limiting, headers
# ---------------------------------------------------------------------------

def test_invalid_inputs_rejected():
    client = TestClient(api_module.app)
    # Bad customer_id (path-like), negative amount, malformed failure code.
    bad = client.post(
        "/payment-failed",
        json={
            "customer_id": "../etc/passwd",
            "amount": -5,
            "currency": "usd",
            "failure_code": "DROP TABLE",
            "attempt": 1,
        },
    )
    assert bad.status_code == 422


def test_rate_limit_returns_429(no_key, monkeypatch):
    """A single client IP over the window gets 429s once the cap is hit."""
    # Isolate this test's counter and use a small cap for speed.
    monkeypatch.setattr(api_module, "_RATE_MAX", 5)
    monkeypatch.setattr(api_module, "_rate_hits", {})

    client = TestClient(api_module.app)
    payload = {
        "customer_id": "cust_001",
        "amount": 1499.0,
        "currency": "usd",
        "failure_code": "card_expired",
        "attempt": 1,
    }
    headers = {"Fly-Client-IP": "203.0.113.7"}

    codes = [
        client.post("/payment-failed", json=payload, headers=headers).status_code
        for _ in range(7)
    ]
    assert codes.count(200) == 5
    assert codes.count(429) == 2


def test_distinct_ips_have_separate_budgets(no_key, monkeypatch):
    monkeypatch.setattr(api_module, "_RATE_MAX", 2)
    monkeypatch.setattr(api_module, "_rate_hits", {})
    client = TestClient(api_module.app)
    payload = {
        "customer_id": "cust_001",
        "amount": 1499.0,
        "currency": "usd",
        "failure_code": "card_expired",
        "attempt": 1,
    }
    a = client.post("/payment-failed", json=payload, headers={"Fly-Client-IP": "198.51.100.1"})
    b = client.post("/payment-failed", json=payload, headers={"Fly-Client-IP": "198.51.100.2"})
    assert a.status_code == 200 and b.status_code == 200


def test_security_headers_present():
    client = TestClient(api_module.app)
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in r.headers
    assert "strict-transport-security" in r.headers


def test_config_reports_mock_and_customers(no_key):
    client = TestClient(api_module.app)
    cfg = client.get("/config").json()
    assert cfg["mock"] is True
    assert any(c["id"] == "cust_001" for c in cfg["customers"])
