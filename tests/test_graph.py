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

    assert set(output) == {"diagnosis", "risk", "strategy", "schedule", "message", "impact"}
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
    strategy = result["strategy"]
    # Base action/cadence/offer come from the default rule; no risk -> not escalated.
    assert strategy["action"] == nodes_module._DEFAULT_STRATEGY["action"]
    assert strategy["retry_in_days"] == nodes_module._DEFAULT_STRATEGY["retry_in_days"]
    assert strategy["offer"] == nodes_module._DEFAULT_STRATEGY["offer"]
    assert strategy["escalated"] is False


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
# assess_risk / escalation
# ---------------------------------------------------------------------------

def test_prior_failures_excludes_current_charge():
    """The current (last) failed charge is not counted as a prior failure."""
    customer = {
        "payment_history": [
            {"status": "succeeded"},
            {"status": "failed"},
            {"status": "succeeded"},
            {"status": "failed"},  # <- the current charge, excluded
        ]
    }
    assert nodes_module._prior_failures(customer) == 1


@pytest.mark.parametrize(
    "attempt, prior, expected",
    [
        (1, 0, "low"),
        (1, 1, "medium"),
        (2, 0, "medium"),
        (3, 0, "high"),
        (2, 2, "high"),
    ],
)
def test_score_churn_risk_buckets(attempt, prior, expected):
    assert nodes_module._score_churn_risk(attempt, prior) == expected


def test_assess_risk_flags_escalation_on_third_attempt():
    """A third dunning attempt is high churn risk and escalates."""
    state = {"event": {"attempt": 3}, "customer": {}}
    risk = nodes_module.assess_risk(state)["risk"]
    assert risk["churn_risk"] == "high"
    assert risk["escalate"] is True
    assert risk["attempt"] == 3


def test_choose_strategy_escalates_on_high_risk():
    """High churn risk tightens the retry cadence and marks the strategy escalated."""
    state = {
        "event": {"failure_code": "insufficient_funds"},  # base cadence 3 days
        "risk": {"escalate": True},
    }
    strategy = nodes_module.choose_strategy(state)["strategy"]
    assert strategy["escalated"] is True
    assert strategy["retry_in_days"] == 2  # pulled in by a day from 3
    assert "repeat failure" in strategy["offer"].lower()


def test_choose_strategy_cadence_floors_at_one_day():
    """Escalating a 1-day cadence must not drop below 1 day."""
    state = {
        "event": {"failure_code": "card_expired"},  # base cadence 1 day
        "risk": {"escalate": True},
    }
    strategy = nodes_module.choose_strategy(state)["strategy"]
    assert strategy["retry_in_days"] == 1


def test_impact_discounts_recovery_on_prior_failures():
    """Recent prior failures lower the recovery likelihood below the base rate."""
    event = {"amount": 100.0, "currency": "usd", "failure_code": "card_expired"}
    base = nodes_module._build_impact(event, {}, {"prior_failures": 0})
    penalised = nodes_module._build_impact(event, {}, {"prior_failures": 2})
    assert penalised["recovery_likelihood"] < base["recovery_likelihood"]
    assert penalised["expected_recovered"] < base["expected_recovered"]


def test_prior_failures_caps_at_window():
    """Only the most recent window of prior charges is counted."""
    # 10 failed entries; the last is the current charge (excluded), leaving 9
    # prior, but the default window caps the count at 6.
    customer = {"payment_history": [{"status": "failed"} for _ in range(10)]}
    assert nodes_module._prior_failures(customer) == 6


def test_build_impact_upcases_currency():
    event = {"amount": 50.0, "currency": "eur", "failure_code": "card_expired"}
    assert nodes_module._build_impact(event, {}, {})["currency"] == "EUR"


def test_build_impact_mrr_falls_back_to_amount():
    """With no customer MRR on file, annual value falls back to the failed amount."""
    event = {"amount": 80.0, "currency": "usd", "failure_code": "card_expired"}
    impact = nodes_module._build_impact(event, {}, {})
    assert impact["annual_value_at_risk"] == 80.0 * 12


def test_retrieve_context_builds_grounded_query(monkeypatch):
    """The RAG query is built from the failure code and the customer's plan."""
    captured = {}

    class _Spy:
        def invoke(self, query):
            captured["q"] = query
            return [_FakeDoc("snippet")]

    monkeypatch.setattr(nodes_module, "get_retriever", lambda: _Spy())
    event = {"customer_id": "cust_001", "failure_code": "card_expired"}
    nodes_module.retrieve_context({"event": event})
    assert "card_expired" in captured["q"]
    assert "Scale" in captured["q"]  # cust_001's plan, pulled from the record


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
    assert set(body) == {"diagnosis", "risk", "strategy", "schedule", "message", "impact"}
    assert body["strategy"]["action"] == "request_card_update"


def test_payment_failed_rejects_invalid_payload():
    """Missing required fields should fail pydantic validation with a 422."""
    client = TestClient(api_module.app)
    response = client.post("/payment-failed", json={"amount": 10.0})
    assert response.status_code == 422


def test_payment_failed_escalated_path_serializes(patched_nodes):
    """The escalated (high-risk) payload round-trips through the response model."""
    client = TestClient(api_module.app)
    r = client.post(
        "/payment-failed",
        json={
            "customer_id": "cust_002",
            "amount": 299.0,
            "currency": "usd",
            "failure_code": "insufficient_funds",
            "attempt": 3,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["risk"]["churn_risk"] == "high"
    assert body["strategy"]["escalated"] is True


def test_payment_failed_unknown_code_serializes(patched_nodes):
    """An unknown failure code takes the default strategy and still validates."""
    client = TestClient(api_module.app)
    r = client.post(
        "/payment-failed",
        json={
            "customer_id": "cust_001",
            "amount": 10.0,
            "currency": "usd",
            "failure_code": "mystery_code",
            "attempt": 1,
        },
    )
    assert r.status_code == 200
    assert r.json()["strategy"]["action"] == "retry_and_verify"  # default rule


# ---------------------------------------------------------------------------
# SEO / AEO / GEO discovery surface
# ---------------------------------------------------------------------------

def test_robots_txt_served():
    client = TestClient(api_module.app)
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap:" in r.text
    assert r.headers["content-type"].startswith("text/plain")


def test_sitemap_xml_served():
    client = TestClient(api_module.app)
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "paypilot.fly.dev" in r.text
    assert "xml" in r.headers["content-type"]


def test_llms_txt_served():
    """The AEO/GEO structured summary for LLM crawlers is reachable."""
    client = TestClient(api_module.app)
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert "PayPilot" in r.text


def test_landing_page_has_structured_data():
    """The landing page ships SoftwareApplication + FAQPage JSON-LD for AEO."""
    client = TestClient(api_module.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "application/ld+json" in r.text
    assert "SoftwareApplication" in r.text
    assert "FAQPage" in r.text


# ---------------------------------------------------------------------------
# Batch + portfolio aggregate
# ---------------------------------------------------------------------------

def test_run_recovery_batch_aggregates(patched_nodes):
    """A batch returns per-event outputs plus a summed portfolio aggregate."""
    events = [
        {"customer_id": "cust_001", "amount": 100.0, "currency": "usd", "failure_code": "card_expired", "attempt": 1},
        {"customer_id": "cust_003", "amount": 200.0, "currency": "usd", "failure_code": "card_expired", "attempt": 1},
    ]
    out = graph_module.run_recovery_batch(events)
    assert out["aggregate"]["count"] == 2
    assert len(out["results"]) == 2
    assert out["aggregate"]["currency"] == "USD"
    expected = round(sum(r["impact"]["expected_recovered"] for r in out["results"]), 2)
    assert out["aggregate"]["total_expected_recovered"] == expected


def test_run_recovery_batch_empty_is_safe():
    out = graph_module.run_recovery_batch([])
    assert out["results"] == []
    assert out["aggregate"]["count"] == 0
    assert out["aggregate"]["currency"] == "USD"


def test_batch_endpoint_returns_results_and_aggregate(patched_nodes):
    client = TestClient(api_module.app)
    r = client.post(
        "/payment-failed/batch",
        json={"events": [
            {"customer_id": "cust_001", "amount": 100.0, "currency": "usd", "failure_code": "card_expired", "attempt": 1}
        ]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["aggregate"]["count"] == 1
    assert len(body["results"]) == 1
    assert set(body["results"][0]) == {"diagnosis", "risk", "strategy", "schedule", "message", "impact"}


def test_batch_endpoint_rejects_oversize():
    client = TestClient(api_module.app)
    ev = {"customer_id": "cust_001", "amount": 1.0, "currency": "usd", "failure_code": "card_expired", "attempt": 1}
    r = client.post("/payment-failed/batch", json={"events": [ev] * 51})
    assert r.status_code == 422


def test_batch_endpoint_rejects_empty():
    client = TestClient(api_module.app)
    r = client.post("/payment-failed/batch", json={"events": []})
    assert r.status_code == 422


def test_portfolio_impact_covers_all_demo_customers(patched_nodes):
    """The homepage headline aggregates every demo customer."""
    client = TestClient(api_module.app)
    r = client.get("/portfolio-impact")
    assert r.status_code == 200
    agg = r.json()
    assert agg["count"] == 6  # six fixtures in data/customers.json
    assert agg["total_expected_recovered"] > 0
