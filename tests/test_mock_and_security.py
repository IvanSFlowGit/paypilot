"""Tests for the free demo (mock) mode and the API security controls.

Unlike test_graph.py - which mocks the LLM/retriever seams - these tests exercise
the *real* offline path: with no ``OPENAI_API_KEY`` set, ``get_llm`` returns the
deterministic mock model and ``get_retriever`` returns the lexical retriever, so
the whole flow runs with no key and no network. The security tests cover input
validation, the per-IP rate limit, and the response hardening headers.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

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
    assert isinstance(nodes_module.get_llm(), nodes_module._TemplateEngine)


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

    assert set(output) == {
        "diagnosis", "risk", "strategy", "schedule", "message", "impact", "fallback_used"
    }
    # Grounded happy path: no LLM node fell back to a template.
    assert output["fallback_used"] is False
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


def test_high_risk_attempt_escalates_offline(no_key):
    """A third dunning attempt escalates the strategy and surfaces churn risk."""
    event = {
        "customer_id": "cust_002",  # Brightleaf, repeat insufficient_funds history
        "amount": 299.0,
        "currency": "usd",
        "failure_code": "insufficient_funds",
        "attempt": 3,
    }
    out = graph_module.run_recovery(event)

    assert out["risk"]["churn_risk"] == "high"
    assert out["risk"]["escalate"] is True
    assert out["strategy"]["escalated"] is True
    assert out["impact"]["churn_risk"] == "high"
    # The mock diagnosis reflects the elevated risk.
    assert "churn risk is elevated" in out["diagnosis"].lower()
    # The schedule follows the tightened cadence (3 -> 2 days on escalation).
    assert out["strategy"]["retry_in_days"] == 2
    assert out["schedule"]["retry_in_days"] == 2


def test_medium_risk_second_attempt_not_escalated(no_key):
    """A second attempt with a clean history is medium risk and does not escalate."""
    event = {
        "customer_id": "cust_001",  # Acme Robotics, no prior failures on file
        "amount": 1499.0,
        "currency": "usd",
        "failure_code": "card_expired",
        "attempt": 2,
    }
    out = graph_module.run_recovery(event)

    assert out["risk"]["churn_risk"] == "medium"
    assert out["risk"]["escalate"] is False
    assert out["strategy"]["escalated"] is False
    # No escalation suffix on the mock diagnosis at medium risk.
    assert "churn risk is elevated" not in out["diagnosis"].lower()


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


def _fresh_rate_state(monkeypatch, *, per_ip=5, global_max=600, max_ips=10000):
    """Reset the module-level limiter state so a test is isolated."""
    from collections import OrderedDict, deque

    monkeypatch.setattr(api_module, "_RATE_MAX", per_ip)
    monkeypatch.setattr(api_module, "_RATE_GLOBAL_MAX", global_max)
    monkeypatch.setattr(api_module, "_RATE_MAX_TRACKED_IPS", max_ips)
    monkeypatch.setattr(api_module, "_rate_hits", OrderedDict())
    monkeypatch.setattr(api_module, "_global_hits", deque())


_PAYLOAD = {
    "customer_id": "cust_001",
    "amount": 1499.0,
    "currency": "usd",
    "failure_code": "card_expired",
    "attempt": 1,
}


def test_rate_limit_returns_429(no_key, monkeypatch):
    """A single client IP over the window gets 429s once the cap is hit."""
    _fresh_rate_state(monkeypatch, per_ip=5)
    client = TestClient(api_module.app)
    headers = {"Fly-Client-IP": "203.0.113.7"}
    codes = [
        client.post("/payment-failed", json=_PAYLOAD, headers=headers).status_code
        for _ in range(7)
    ]
    assert codes.count(200) == 5
    assert codes.count(429) == 2


def test_distinct_ips_have_separate_budgets(no_key, monkeypatch):
    _fresh_rate_state(monkeypatch, per_ip=2)
    client = TestClient(api_module.app)
    a = client.post("/payment-failed", json=_PAYLOAD, headers={"Fly-Client-IP": "198.51.100.1"})
    b = client.post("/payment-failed", json=_PAYLOAD, headers={"Fly-Client-IP": "198.51.100.2"})
    assert a.status_code == 200 and b.status_code == 200


def test_global_cap_backstops_ip_rotation(no_key, monkeypatch):
    """Rotating the client-IP header per request still hits the global cap."""
    # Generous per-IP cap, tiny global cap: each request uses a fresh spoofed IP,
    # so per-IP never trips, but the global backstop must.
    _fresh_rate_state(monkeypatch, per_ip=1000, global_max=3)
    client = TestClient(api_module.app)
    codes = [
        client.post(
            "/payment-failed", json=_PAYLOAD, headers={"Fly-Client-IP": f"203.0.113.{i}"}
        ).status_code
        for i in range(5)
    ]
    assert codes.count(200) == 3
    assert codes.count(429) == 2


def test_rate_bucket_map_is_lru_capped(no_key, monkeypatch):
    """A flood of distinct client IPs can't grow the bucket map without bound."""
    _fresh_rate_state(monkeypatch, per_ip=5, max_ips=2)
    client = TestClient(api_module.app)
    for i in range(4):
        client.post("/payment-failed", json=_PAYLOAD, headers={"Fly-Client-IP": f"198.51.100.{i}"})
    # LRU ceiling holds regardless of how many unique IPs were seen.
    assert len(api_module._rate_hits) <= 2


def test_client_ip_falls_back_to_forwarded_for(no_key, monkeypatch):
    """Without Fly-Client-IP, the first X-Forwarded-For hop is used per-bucket."""
    _fresh_rate_state(monkeypatch, per_ip=1)
    client = TestClient(api_module.app)
    h = {"X-Forwarded-For": "192.0.2.5, 10.0.0.1"}
    first = client.post("/payment-failed", json=_PAYLOAD, headers=h)
    second = client.post("/payment-failed", json=_PAYLOAD, headers=h)
    assert first.status_code == 200
    assert second.status_code == 429  # same forwarded IP -> same bucket -> capped
    assert "192.0.2.5" in api_module._rate_hits


def test_security_headers_present():
    client = TestClient(api_module.app)
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in r.headers
    assert "strict-transport-security" in r.headers


def test_process_time_header_present():
    client = TestClient(api_module.app)
    r = client.get("/health")
    assert "x-process-time" in r.headers


def test_rate_limit_sets_retry_after(no_key, monkeypatch):
    _fresh_rate_state(monkeypatch, per_ip=1)
    client = TestClient(api_module.app)
    ip = {"Fly-Client-IP": "203.0.113.9"}
    client.post("/payment-failed", json=_PAYLOAD, headers=ip)
    r = client.post("/payment-failed", json=_PAYLOAD, headers=ip)
    assert r.status_code == 429
    assert r.headers.get("retry-after") is not None


def test_payment_failed_idempotency_key_replays(no_key, monkeypatch):
    """A repeated Idempotency-Key replays the first result, ignoring the new body."""
    from collections import OrderedDict

    _fresh_rate_state(monkeypatch)
    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())
    client = TestClient(api_module.app)
    headers = {"Idempotency-Key": "abc-123"}

    first = client.post("/payment-failed", json=_PAYLOAD, headers=headers)
    # Same key, different body -> must replay the cached first response.
    other = {**_PAYLOAD, "customer_id": "cust_003", "failure_code": "generic_decline"}
    second = client.post("/payment-failed", json=other, headers=headers)

    assert first.status_code == 200 and second.status_code == 200
    assert second.json() == first.json()


def test_docs_csp_allows_swagger_cdn():
    """/docs gets a CSP that permits the jsDelivr assets Swagger UI needs."""
    client = TestClient(api_module.app)
    r = client.get("/docs")
    assert r.status_code == 200
    assert "cdn.jsdelivr.net" in r.headers["content-security-policy"]


def test_non_docs_csp_stays_strict():
    """Non-docs paths keep the strict same-origin CSP (no CDN allowance)."""
    client = TestClient(api_module.app)
    r = client.get("/health")
    assert "cdn.jsdelivr.net" not in r.headers["content-security-policy"]


def test_config_reports_mock_and_customers(no_key):
    client = TestClient(api_module.app)
    cfg = client.get("/config").json()
    assert cfg["mock"] is True
    assert any(c["id"] == "cust_001" for c in cfg["customers"])


# ---------------------------------------------------------------------------
# Public-surface truth: what /config and the landing copy claim about the run
# ---------------------------------------------------------------------------

def test_config_model_is_mock_when_mock_is_true(no_key, monkeypatch):
    """/config must not advertise a model that no request is going to run.

    The handler used to re-derive the model name from ``OPENAI_MODEL``
    independently of :func:`app.nodes.use_mock`, so the public payload read
    ``{"mock": true, "model": "gpt-4o-mini"}`` - a capability claim the running
    code was not honouring. One derivation, in ``app.nodes``, for both.
    """
    monkeypatch.delenv("PAYPILOT_LLM_DRAFT", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    client = TestClient(api_module.app)
    cfg = client.get("/config").json()

    assert cfg["mock"] is True
    assert cfg["model"] == "mock"
    assert cfg["model"] == nodes_module.model_name()


def test_config_model_is_the_configured_model_when_live(monkeypatch):
    """With live drafting really on, /config names the model that will run."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("PAYPILOT_LLM_DRAFT", "1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    client = TestClient(api_module.app)
    cfg = client.get("/config").json()

    assert cfg["mock"] is False
    assert cfg["model"] == "gpt-4o-mini"
    assert cfg["model"] == nodes_module.model_name()


def test_landing_copy_has_no_orphaned_diagnosis_clause():
    """The "Retrieve + assess + diagnose" card shipped a stranded clause live."""
    client = TestClient(api_module.app)
    body = client.get("/").text

    assert "enabled of <i>why</i> the charge failed" not in body
    assert "<i>why</i> the charge failed" in body


def test_public_copy_never_claims_faiss_unqualified():
    """FAISS only runs on the keyed path; unqualified it overclaims the demo."""
    client = TestClient(api_module.app)
    for path in ("/", "/llms.txt"):
        text = client.get(path).text
        for line in text.splitlines():
            if "FAISS" not in line:
                continue
            assert "when a key is set" in line, f"{path}: unqualified FAISS: {line.strip()}"


def test_demo_script_never_prints_an_absolute_path():
    """The demo runs on camera; an absolute path shows the operator's home dir.

    An invariant, not a wording check: every path the script puts on screen goes
    through one of these two, and neither may ever return something absolute -
    including when it is handed an absolute path to begin with, which is exactly
    the case ``--db /somewhere/else.db`` produces.
    """
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "demo_loop_pathguard", root / "scripts" / "demo_loop.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert not Path(module.LINK_FILE_DISPLAY).is_absolute()
    for given in (
        "data/demo-loop.db",
        str(root / "data" / "demo-loop.db"),
        "/somewhere/else/ledger.db",
    ):
        shown = module._screen_safe_path(given)
        assert not Path(shown).is_absolute(), f"{given!r} printed as {shown!r}"


# ---------------------------------------------------------------------------
# Public test-count claims: seven surfaces, one number, no silent rot
# ---------------------------------------------------------------------------

#: Every public claim about how big the suite is, as a regex whose groups are the
#: numbers claimed. Seven sites, eight numbers (the stat chip claims "N/N").
#: The counterpart to the FAISS test above: the same class of defect, a public
#: number that drifts from the code and nobody notices until a reader checks.
_COUNT_CLAIM_PATTERNS = (
    r"tests-(\d+)%20passing",
    r"\*\*(\d+)-test\*\* suite",
    r"<b>(\d+)/(\d+)</b> tests",
    r"full (\d+)-test suite",
)
_EXPECTED_CLAIM_NUMBERS = 8


def _collected_test_total() -> int:
    """The number ``pytest -q`` reports, derived by collecting in a subprocess.

    A subprocess rather than the live session because the live session's item
    count depends on how pytest was invoked (``pytest tests/test_x.py`` collects
    a handful), which would make this guard fail for reasons that have nothing
    to do with the claim being wrong. Collection only imports, it never runs
    tests, so there is no recursion.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=180,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    assert match, f"could not read a collected count from:\n{proc.stdout[-2000:]}"
    return int(match.group(1))


def test_public_test_count_claims_match_the_suite():
    """The README badge, the landing page and llms.txt must claim the real total.

    ``pytest -q`` collects tests/ and evals/ together (see ``testpaths``), so the
    number these surfaces mean is that combined total, evals included.
    """
    total = _collected_test_total()
    client = TestClient(api_module.app)
    surfaces = {
        "README.md": Path(__file__).resolve().parents[1].joinpath("README.md").read_text(),
        "/": client.get("/").text,
        "/llms.txt": client.get("/llms.txt").text,
    }

    seen = 0
    for name, text in surfaces.items():
        for pattern in _COUNT_CLAIM_PATTERNS:
            for match in re.finditer(pattern, text):
                for claimed in match.groups():
                    seen += 1
                    assert int(claimed) == total, (
                        f"{name} claims {claimed} tests, the suite collects {total}"
                    )
    assert seen == _EXPECTED_CLAIM_NUMBERS, (
        f"expected {_EXPECTED_CLAIM_NUMBERS} public test-count claims, found {seen} - "
        "a surface was added or removed, so update _COUNT_CLAIM_PATTERNS"
    )
