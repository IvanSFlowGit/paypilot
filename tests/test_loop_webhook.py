"""POST /webhooks/loop: authentication, transport, the hold rule and idempotency.

Loop's documentation describes no delivery signing, so the only authentication is a
URL secret. These tests pin what that route promises and, as importantly, what it
refuses: plain HTTP, a basic-auth password (Loop is not documented to send one), a
missing or wrong key. Every payload is synthetic and shaped from Loop's documented
example; no live delivery has been seen.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from tests.test_loop_subscriptions import DOC_FAILED_ORDER

SECRET = "loop-test-url-secret"
HTTPS = {"x-forwarded-proto": "https"}


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setenv("LOOP_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("PAYPILOT_ALLOW_HTTP_WEBHOOKS", raising=False)
    monkeypatch.delenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    api_module._idem_store.clear()
    return monkeypatch


def _event(*, status="FAILED", **attempt):
    body = json.loads(json.dumps(DOC_FAILED_ORDER))
    body["payload"]["status"] = status
    body["payload"]["paymentAttempt"].update(attempt)
    return body


def _post(client, body, *, key=SECRET, headers=HTTPS, raw=None):
    url = "/webhooks/loop" + (f"?key={key}" if key else "")
    content = raw if raw is not None else json.dumps(body)
    return client.post(url, content=content, headers=dict(headers or {}))


def test_the_url_key_over_https_authenticates(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event())
    assert r.status_code == 200, r.text
    assert r.json()["handled"] is True
    assert r.json()["error_code"] == "insufficient_funds"


def test_a_wrong_key_is_rejected(no_key):
    with TestClient(api_module.app) as client:
        assert _post(client, _event(), key="wrong").status_code == 400


def test_no_key_is_rejected(no_key):
    with TestClient(api_module.app) as client:
        assert _post(client, _event(), key=None).status_code == 400


def test_plain_http_is_refused_even_with_the_right_key(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(), headers={})
    assert r.status_code == 400
    assert "HTTPS" in r.json()["detail"]


def test_a_forwarded_http_scheme_is_refused(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(), headers={"x-forwarded-proto": "http"})
    assert r.status_code == 400


def test_the_local_test_override_allows_http(no_key):
    no_key.setenv("PAYPILOT_ALLOW_HTTP_WEBHOOKS", "1")
    with TestClient(api_module.app) as client:
        r = _post(client, _event(), headers={})
    assert r.status_code == 200, r.text


def test_a_basic_auth_password_is_not_a_carrier(no_key):
    """Loop is not documented to send basic auth, so the route reads none."""
    import base64

    token = base64.b64encode(f"loop:{SECRET}".encode()).decode()
    with TestClient(api_module.app) as client:
        r = _post(client, _event(), key=None, headers={**HTTPS, "Authorization": f"Basic {token}"})
    assert r.status_code == 400


def test_an_unset_secret_rejects_when_authentication_is_required(no_key):
    no_key.delenv("LOOP_WEBHOOK_SECRET", raising=False)
    with TestClient(api_module.app) as client:
        r = _post(client, _event(), key=None)
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_the_message_is_held_while_loop_is_still_retrying(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(retryLeftCount=2, idempotencyKey="k-held"))
    rec = r.json()["recovery"]
    assert rec["message"] is None
    assert rec["message_suppressed"] == "loop_recovery_in_progress"
    assert r.json()["loop_retries_left"] == 2


def test_an_unreadable_retry_count_is_treated_as_still_retrying(no_key):
    body = _event(idempotencyKey="k-unknown")
    del body["payload"]["paymentAttempt"]["retryLeftCount"]
    with TestClient(api_module.app) as client:
        r = _post(client, body)
    assert r.json()["loop_retries_left"] is None
    assert r.json()["recovery"]["message_suppressed"] == "loop_recovery_in_progress"


def test_the_message_is_written_once_loop_has_stopped_retrying(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(retryLeftCount=0, idempotencyKey="k-done"))
    rec = r.json()["recovery"]
    assert rec.get("message_suppressed") is None
    assert r.json()["loop_retries_left"] == 0


def test_a_store_side_failure_is_acknowledged_and_not_run(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(errorCode="INSUFFICIENT_INVENTORY"))
    assert r.status_code == 200
    assert r.json()["handled"] is False
    assert r.json()["reason"] == "not_customer"


def test_an_unknown_error_code_is_acknowledged_and_not_run(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(errorCode="SOMETHING_NEW"))
    assert r.json()["handled"] is False
    assert r.json()["reason"] == "unmapped"


def test_a_non_failed_order_is_not_run(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(status="PAID"))
    assert r.json()["handled"] is False


def test_the_same_idempotency_key_is_served_from_the_cache(no_key):
    ev = _event(idempotencyKey="k-dupe")
    with TestClient(api_module.app) as client:
        first = _post(client, ev).json()
        second = _post(client, ev).json()
    assert first.get("idempotent") is None
    assert second["idempotent"] is True


def test_a_non_dict_payload_is_a_400(no_key):
    with TestClient(api_module.app) as client:
        assert _post(client, None, raw="[]").status_code == 400


def test_invalid_json_is_a_400(no_key):
    with TestClient(api_module.app) as client:
        assert _post(client, None, raw="{not json").status_code == 400


def test_the_route_is_published_in_the_schema(no_key):
    assert "/webhooks/loop" in api_module.app.openapi()["paths"]
