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
# Public copy carries NO test count. Absence, not agreement.
# ---------------------------------------------------------------------------
#
# WHY THIS ASSERTS ABSENCE RATHER THAN A NUMBER. The previous guard pinned a
# regex per surface and compared each claimed number to the collected total. It
# was green while README, the landing page and llms.txt all published 731,
# which the retired-numbers register retires by name, and while
# docs/compliance/controls-inventory.md published 720, a third number the guard
# did not cover at all. A guard that compares a claim to the suite cannot tell a
# retired figure from a current one: it only ever asked whether two things
# matched, never whether either was allowed.
#
# The register's rule is stronger and needs no maintenance: WRITTEN COPY CARRIES
# NO COUNT, approved wording "with a test and evaluation suite in CI". A number
# that is never printed cannot go stale, so this guard cannot go stale either.
#
# It fires on a number adjacent to suite-size language, whatever the number is.
# It must NOT fire on ordinary prose about an HTTP status ("the 422 test asserts
# ..."), which is what killed the temptation to match any digit near "test".

_COUNT_CLAIM = re.compile(
    r"badge/tests-\d+"                              # shields.io badge with a number
    r"|\d+\s*[-\u2011]?\s*test\b[^\n]{0,6}?\bsuite"  # "731-test suite", "**731-test** suite"
    r"|<b>\s*\d+\s*/\s*\d+\s*</b>"                   # "<b>731/731</b>" stat chip
    r"|\d+\s+(?:automated\s+)?(?:tests|checks|evals)\b"  # "749 automated checks"
    r"|\b(?:test\s+suite|checks|evals)\s*[:=]\s*\d+",   # "Test suite: 720"
    re.I,
)

#: A count that names ONE file is not a claim about the size of the suite, which
#: is what the register retires. "tests/test_ai_disclosure.py (6 tests)" is
#: precise, checkable and drifts with the file it names. Firing on it would be a
#: permanent false positive in a compliance document, and a guard people step
#: around has already been repealed.
_PER_FILE_COUNT = re.compile(r"\.py`?\s*\(\d+\s+tests?\)", re.I)


def test_public_copy_carries_no_test_count():
    """No public surface may publish a suite size. See the register, not a list.

    Four surfaces, including the controls inventory that the previous guard did
    not read. Adding a surface means adding it here; a surface nobody added is a
    surface reported as clean.
    """
    root = Path(__file__).resolve().parents[1]
    client = TestClient(api_module.app)
    surfaces = {
        "README.md": root.joinpath("README.md").read_text(),
        "/": client.get("/").text,
        "/llms.txt": client.get("/llms.txt").text,
        "docs/compliance/controls-inventory.md":
            root.joinpath("docs/compliance/controls-inventory.md").read_text(),
    }
    found = []
    for name, text in surfaces.items():
        for m in _COUNT_CLAIM.finditer(text):
            window = text[max(0, m.start() - 60):m.end() + 5]
            if _PER_FILE_COUNT.search(window):
                continue
            found.append(f"{name}: {m.group(0)!r}")
    assert not found, (
        "public copy must carry no test count, the approved wording is "
        '"with a test and evaluation suite in CI". Found: ' + "; ".join(found)
    )


def test_the_count_guard_can_actually_fail():
    """The guard above has only ever passed. Prove it can fire, both ways.

    Blocking cases are the wordings this repository actually published. Passing
    cases are the ones it must wave through, because a guard with a permanent
    false positive is one somebody switches off.
    """
    must_block = (
        "https://img.shields.io/badge/tests-731%20passing-brightgreen",
        "full **731-test** suite runs offline",
        "<b>731/731</b> tests, offline",
        "749 automated checks (725 tests plus 24 evals)",
        "Test suite: 720 tests, offline",
    )
    must_pass = (
        "the 422 test asserts the rejected value appears in neither",
        "the 500 test asserts the customer's name is absent",
        "runs its full test and evaluation suite offline",
        "with a test and evaluation suite in CI",
    )
    per_file = "`tests/test_ai_disclosure.py` (6 tests). Show it:"
    assert _PER_FILE_COUNT.search(per_file), (
        "a per-file count must be exempt, or the guard fires forever on the "
        "controls inventory and somebody switches it off"
    )
    for s in must_block:
        assert _COUNT_CLAIM.search(s), f"guard failed to catch a real published count: {s!r}"
    for s in must_pass:
        assert not _COUNT_CLAIM.search(s), f"guard fired on legitimate copy: {s!r}"
