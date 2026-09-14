"""One contract, every deployment of the decision slice.

The same tests run against three targets:

* ``fastapi`` - the app that runs on Fly, in process through TestClient.
* ``lambda``  - app/lambda_handler.handler, in process, fed API Gateway HTTP API
  payload 2.0 events, with a SQLite audit standing in for RDS.
* ``live``    - any deployed base URL over real HTTP. Collected only when
  ``PAYPILOT_CONTRACT_BASE_URL`` and ``PAYPILOT_CONTRACT_TOKEN`` are set::

      PAYPILOT_CONTRACT_BASE_URL=https://<api-id>.execute-api.<region>.amazonaws.com \\
      PAYPILOT_CONTRACT_TOKEN=... .venv/bin/python -m pytest tests/test_decision_contract.py -k live

A separate AWS test suite would prove the suite. Running this file against the
Fly URL and the AWS URL, unchanged, is what proves the deployments agree.

The live target writes real audit rows (invoice ids prefixed ``in_contract_``),
so it is never run against a database holding real invoices.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from app import api as api_module
from app import lambda_handler
from app.decision import MAX_BODY_BYTES
from app.decision_audit import SqliteDecisionAudit

# Read at import, before conftest's hermetic_env strips the environment.
_LIVE_BASE_URL = (os.environ.get("PAYPILOT_CONTRACT_BASE_URL") or "").rstrip("/")
_LIVE_TOKEN = os.environ.get("PAYPILOT_CONTRACT_TOKEN") or ""

TOKEN = "contract-test-token"


class _Target:
    """Minimal client: ``request(method, path, body, token) -> (status, json)``."""

    name = "abstract"
    in_process = True
    token = TOKEN

    def request(self, method, path, body=None, token=None, raw=None):
        raise NotImplementedError


class _FastApiTarget(_Target):
    name = "fastapi"

    def __init__(self):
        from fastapi.testclient import TestClient

        self.client = TestClient(api_module.app)

    def request(self, method, path, body=None, token=None, raw=None):
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        content = raw if raw is not None else (json.dumps(body) if body is not None else None)
        resp = self.client.request(method, path, content=content, headers=headers)
        return resp.status_code, resp.json()


class _LambdaTarget(_Target):
    name = "lambda"

    def request(self, method, path, body=None, token=None, raw=None):
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        event = {
            "version": "2.0",
            "routeKey": f"{method} {path}",
            "rawPath": path,
            "headers": headers,
            "requestContext": {"http": {"method": method, "path": path}},
            "body": raw if raw is not None else (json.dumps(body) if body is not None else None),
            "isBase64Encoded": False,
        }
        if path.startswith("/decisions/"):
            event["routeKey"] = f"{method} /decisions/{{invoice_id}}"
            event["pathParameters"] = {"invoice_id": path[len("/decisions/"):]}
        result = lambda_handler.handler(event, None)
        return result["statusCode"], json.loads(result["body"])


class _LiveTarget(_Target):
    name = "live"
    in_process = False

    def __init__(self):
        import httpx

        self.client = httpx.Client(base_url=_LIVE_BASE_URL, timeout=30)
        self.token = _LIVE_TOKEN

    def request(self, method, path, body=None, token=None, raw=None):
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        content = raw if raw is not None else (json.dumps(body) if body is not None else None)
        resp = self.client.request(method, path, content=content, headers=headers)
        return resp.status_code, resp.json()


_LIVE_READY = bool(_LIVE_BASE_URL and _LIVE_TOKEN)

# The live target is only COLLECTED when it is configured, rather than collected
# and skipped. A skip still counts toward the collected total, and that total is
# published in the README; 21 tests that never run offline must not inflate it.
_TARGETS = ["fastapi", "lambda", *(["live"] if _LIVE_READY else [])]


@pytest.fixture(params=_TARGETS)
def target(request, monkeypatch):
    if request.param == "live":
        return _LiveTarget()
    monkeypatch.setenv("DECISION_API_TOKEN", TOKEN)
    audit = SqliteDecisionAudit(":memory:")
    if request.param == "fastapi":
        monkeypatch.setattr(api_module, "_decision_audit", audit)
        return _FastApiTarget()
    monkeypatch.setattr(lambda_handler, "_get_audit", lambda: audit)
    return _LambdaTarget()


def _invoice_id() -> str:
    return f"in_contract_{uuid.uuid4().hex[:16]}"


# --- health ----------------------------------------------------------------


def test_health_needs_no_token(target):
    status, body = target.request("GET", "/health")
    assert status == 200
    assert body == {"status": "ok"}


# --- auth ------------------------------------------------------------------


def test_decide_without_token_is_401(target):
    status, body = target.request("POST", "/decide", {"invoice_id": _invoice_id(), "failure_code": "card_expired"})
    assert status == 401
    assert body["error"] == "unauthorised"


def test_decide_with_wrong_token_is_401(target):
    status, body = target.request(
        "POST", "/decide", {"invoice_id": _invoice_id(), "failure_code": "card_expired"}, token="wrong"
    )
    assert status == 401
    assert body["error"] == "unauthorised"


def test_lookup_without_token_is_401(target):
    status, _ = target.request("GET", f"/decisions/{_invoice_id()}")
    assert status == 401


def test_unconfigured_token_fails_closed(target, monkeypatch):
    if not target.in_process:
        pytest.skip("cannot unset a deployed environment variable from a test")
    monkeypatch.delenv("DECISION_API_TOKEN", raising=False)
    status, body = target.request(
        "POST", "/decide", {"invoice_id": _invoice_id(), "failure_code": "card_expired"}, token=TOKEN
    )
    assert status == 503
    assert body["error"] == "not_configured"


# --- decisions -------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure_code", "rule", "action", "days"),
    [
        ("card_expired", "card_expired", "request_card_update", 1),
        ("insufficient_funds", "insufficient_funds", "wait_and_retry", 3),
        ("generic_decline", "generic_decline", "retry_and_verify", 2),
        ("do_not_honor", "default", "retry_and_verify", 2),
    ],
)
def test_decision_names_the_rule_that_fired(target, failure_code, rule, action, days):
    invoice_id = _invoice_id()
    status, body = target.request(
        "POST", "/decide", {"invoice_id": invoice_id, "failure_code": failure_code}, token=target.token
    )
    assert status == 200, body
    assert body["invoice_id"] == invoice_id
    assert body["rule_fired"] == rule
    assert body["strategy"]["action"] == action
    assert body["strategy"]["retry_in_days"] == days
    assert body["strategy"]["escalated"] is False
    assert body["churn_risk"] == "low"
    assert body["input"] == {
        "invoice_id": invoice_id,
        "failure_code": failure_code,
        "attempt": 1,
        "prior_failures": 0,
    }
    assert len(body["audit_id"]) == 32


def test_high_risk_escalates_and_floors_at_one_day(target):
    status, body = target.request(
        "POST",
        "/decide",
        {"invoice_id": _invoice_id(), "failure_code": "card_expired", "attempt": 3},
        token=target.token,
    )
    assert status == 200, body
    assert body["churn_risk"] == "high"
    assert body["strategy"]["escalated"] is True
    assert body["strategy"]["retry_in_days"] == 1


@pytest.mark.parametrize(
    "bad_body",
    [
        {"failure_code": "card_expired"},
        {"invoice_id": "in_x", "failure_code": "card_expired", "email": "a@example.com"},
        {"invoice_id": "in_x", "failure_code": "card_expired", "attempt": True},
        {"invoice_id": "in_x", "failure_code": "card_expired", "attempt": 0},
        {"invoice_id": "in x", "failure_code": "card_expired"},
        {"invoice_id": "in_x", "failure_code": "Card-Expired"},
        ["in_x", "card_expired"],
    ],
)
def test_invalid_input_is_422_and_never_echoed(target, bad_body):
    status, body = target.request("POST", "/decide", bad_body, token=target.token)
    assert status == 422
    assert body["error"] == "invalid_input"
    assert "a@example.com" not in json.dumps(body)


def test_malformed_json_is_400(target):
    status, body = target.request("POST", "/decide", raw="{not json", token=target.token)
    assert status == 400
    assert body["error"] == "invalid_body"


def test_oversized_body_is_413(target):
    raw = json.dumps({"invoice_id": "in_x", "failure_code": "card_expired", "pad": "x" * MAX_BODY_BYTES})
    status, body = target.request("POST", "/decide", raw=raw, token=target.token)
    assert status == 413
    assert body["error"] == "body_too_large"


# --- audit trail -----------------------------------------------------------


def test_decision_is_readable_back_from_the_audit_trail(target):
    invoice_id = _invoice_id()
    status, decided = target.request(
        "POST", "/decide", {"invoice_id": invoice_id, "failure_code": "insufficient_funds"}, token=target.token
    )
    assert status == 200, decided

    status, looked_up = target.request("GET", f"/decisions/{invoice_id}", token=target.token)
    assert status == 200, looked_up
    assert looked_up["invoice_id"] == invoice_id
    assert len(looked_up["decisions"]) == 1
    row = looked_up["decisions"][0]
    assert row["audit_id"] == decided["audit_id"]
    assert row["rule_fired"] == "insufficient_funds"
    stored = {k: v for k, v in decided.items() if k != "audit_id"}
    assert row["decision"] == stored


def test_lookup_of_unknown_invoice_is_404(target):
    status, body = target.request("GET", f"/decisions/{_invoice_id()}", token=target.token)
    assert status == 404
    assert body["error"] == "not_found"
