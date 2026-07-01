"""Tests for the PayPilot recovery flow.

These tests run with **no OpenAI key and no network**. The two external seams -
the chat model (``app.nodes.get_llm``) and the RAG retriever
(``app.nodes.get_retriever``) - are monkeypatched with in-memory fakes. The node
functions look these names up in the ``app.nodes`` module namespace at call
time, so patching them affects the already-compiled module-level ``graph`` too.

Coverage:

* ``run_recovery`` end-to-end for an expired card (shape + grounded fields).
* The deterministic strategy table for every documented failure code, plus the
  fallback for an unknown code.
* The FastAPI surface: ``/health`` and ``/payment-failed`` via ``TestClient``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import graph as graph_module
from app import nodes as nodes_module


# ---------------------------------------------------------------------------
# Fakes for the LLM + retriever seams (no network, no API key)
# ---------------------------------------------------------------------------

class _FakeDoc:
    """Mimics a LangChain Document: exposes ``page_content``."""

    def __init__(self, content: str) -> None:
        self.page_content = content


class _FakeRetriever:
    """Returns canned playbook snippets regardless of the query."""

    def invoke(self, query: str):  # noqa: D401 - simple stub
        return [
            _FakeDoc("card_expired: ask the customer to update their card on file."),
            _FakeDoc("Keep dunning emails short, warm, and one clear call to action."),
        ]


class _FakeResponse:
    """Mimics a chat model response object with a ``.content`` attribute."""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """Returns a fixed string for any prompt, standing in for ChatOpenAI."""

    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, prompt: str):  # noqa: D401 - simple stub
        return _FakeResponse(self._content)


@pytest.fixture
def patched_nodes(monkeypatch):
    """Patch both external seams in ``app.nodes`` with deterministic fakes."""
    monkeypatch.setattr(nodes_module, "get_retriever", lambda: _FakeRetriever())
    monkeypatch.setattr(
        nodes_module,
        "get_llm",
        lambda: _FakeLLM("We noticed your card expired - please update it to stay subscribed."),
    )
    return monkeypatch


# ---------------------------------------------------------------------------
# run_recovery end-to-end
# ---------------------------------------------------------------------------

def test_run_recovery_returns_full_payload(patched_nodes):
    """A card_expired event yields a diagnosis, strategy, and message."""
    event = {
        "customer_id": "cust_001",  # Acme Robotics, card_expired in fixtures
        "amount": 1499.0,
        "currency": "usd",
        "failure_code": "card_expired",
        "attempt": 1,
    }

    output = graph_module.run_recovery(event)

    assert set(output) == {"diagnosis", "strategy", "schedule", "message", "impact"}
    assert output["diagnosis"]  # non-empty, came from the fake LLM
    assert output["message"]
    # Strategy is deterministic for card_expired (not LLM-decided).
    assert output["strategy"]["action"] == "request_card_update"
    assert output["strategy"]["retry_in_days"] == 1


def test_run_recovery_loads_known_customer(patched_nodes):
    """retrieve_context should hydrate the matching customer record."""
    event = {
        "customer_id": "cust_003",  # Nimbus Health, Enterprise
        "amount": 4200.0,
        "currency": "usd",
        "failure_code": "generic_decline",
        "attempt": 1,
    }

    # Run only the first node to inspect the hydrated state.
    state = nodes_module.retrieve_context({"event": event})

    assert state["customer"]["name"] == "Nimbus Health"
    assert state["customer"]["plan"] == "Enterprise"
    assert state["context"]  # RAG snippets joined into a string


def test_run_recovery_unknown_customer_degrades_gracefully(patched_nodes):
    """An unknown customer_id should not crash the flow."""
    event = {
        "customer_id": "does_not_exist",
        "amount": 10.0,
        "currency": "usd",
        "failure_code": "insufficient_funds",
        "attempt": 2,
    }

    output = graph_module.run_recovery(event)

    assert output["strategy"]["action"] == "wait_and_retry"
    assert output["message"]


# ---------------------------------------------------------------------------
# Deterministic strategy table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "failure_code, expected_action, expected_days",
    [
        ("card_expired", "request_card_update", 1),
        ("insufficient_funds", "wait_and_retry", 3),
        ("generic_decline", "retry_and_verify", 2),
    ],
)
def test_choose_strategy_known_codes(failure_code, expected_action, expected_days):
    """Each documented failure code maps to its fixed action and cadence."""
    state = {"event": {"failure_code": failure_code}}
    result = nodes_module.choose_strategy(state)
    assert result["strategy"]["action"] == expected_action
    assert result["strategy"]["retry_in_days"] == expected_days


def test_choose_strategy_unknown_code_uses_default():
    """An unexpected failure code falls back to the safe default strategy."""
    state = {"event": {"failure_code": "mystery_code"}}
    result = nodes_module.choose_strategy(state)
    assert result["strategy"] == nodes_module._DEFAULT_STRATEGY


def test_choose_strategy_returns_a_copy():
    """The returned strategy must not alias the shared rules table."""
    state = {"event": {"failure_code": "card_expired"}}
    result = nodes_module.choose_strategy(state)
    result["strategy"]["action"] = "mutated"
    # The canonical table is untouched.
    assert nodes_module._STRATEGY_RULES["card_expired"]["action"] == "request_card_update"


# ---------------------------------------------------------------------------
# schedule_retry
# ---------------------------------------------------------------------------

def test_schedule_retry_pins_a_future_utc_time():
    """schedule_retry turns retry_in_days into a concrete UTC retry time."""
    from datetime import datetime, timedelta, timezone

    state = {"strategy": {"retry_in_days": 3}}
    schedule = nodes_module.schedule_retry(state)["schedule"]

    assert schedule["retry_in_days"] == 3
    assert schedule["timezone"] == "UTC"
    # retry_on is the calendar date three days out; next_retry_at parses as ISO.
    expected = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
    assert schedule["retry_on"] == expected
    parsed = datetime.fromisoformat(schedule["next_retry_at"])
    assert parsed.tzinfo is not None  # timezone-aware


def test_schedule_retry_handles_missing_strategy():
    """A missing/zero cadence degrades to 'retry now' instead of raising."""
    schedule = nodes_module.schedule_retry({})["schedule"]
    assert schedule["retry_in_days"] == 0


# ---------------------------------------------------------------------------
# FastAPI surface
# ---------------------------------------------------------------------------

def test_health_endpoint():
    """/health is a static liveness probe and needs no patching."""
    client = TestClient(api_module.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_payment_failed_endpoint(patched_nodes):
    """POST /payment-failed validates the event and returns the recovery payload."""
    client = TestClient(api_module.app)
    response = client.post(
        "/payment-failed",
        json={
            "customer_id": "cust_001",
            "amount": 1499.0,
            "currency": "usd",
            "failure_code": "card_expired",
            "attempt": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"diagnosis", "strategy", "schedule", "message", "impact"}
    assert body["strategy"]["action"] == "request_card_update"


def test_payment_failed_rejects_invalid_payload():
    """Missing required fields should fail pydantic validation with a 422."""
    client = TestClient(api_module.app)
    response = client.post("/payment-failed", json={"amount": 10.0})
    assert response.status_code == 422
