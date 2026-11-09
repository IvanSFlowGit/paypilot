"""The published API document declares the auth the handlers enforce, and cannot drift from them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import api as api_module

CLIENT_OPS = {
    ("post", "/decide"),
    ("get", "/decisions/{invoice_id}"),
    ("post", "/decisions/{invoice_id}/outcome"),
    ("get", "/decisions/outcomes/summary"),
    ("get", "/bank/consents/{consent_id}"),
}
ADMIN_OPS = {("get", "/metrics"), ("get", "/recovery-report"), ("get", "/refusals")}


def _operations(schema):
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            if method in ("get", "post", "put", "delete", "patch"):
                yield method, path, op


def test_both_schemes_are_published_as_http_bearer():
    schemes = api_module.app.openapi()["components"]["securitySchemes"]
    assert schemes["clientBearer"]["type"] == "http" and schemes["clientBearer"]["scheme"] == "bearer"
    assert schemes["adminBearer"]["type"] == "http" and schemes["adminBearer"]["scheme"] == "bearer"


def test_every_listed_operation_declares_exactly_its_scheme():
    declared = {(m, p): op.get("security") for m, p, op in _operations(api_module.app.openapi())}
    for key in CLIENT_OPS:
        assert declared[key] == [{"clientBearer": []}], key
    for key in ADMIN_OPS:
        assert declared[key] == [{"adminBearer": []}], key


def test_no_other_operation_claims_a_bearer_scheme():
    for method, path, op in _operations(api_module.app.openapi()):
        if (method, path) not in CLIENT_OPS | ADMIN_OPS:
            assert not op.get("security"), (method, path)


def test_the_schema_is_stable_across_calls():
    a = api_module.app.openapi()
    b = api_module.app.openapi()
    assert a is b or a == b


def test_a_renamed_operation_fails_loudly_instead_of_dropping_the_claim(monkeypatch):
    monkeypatch.setitem(api_module._SECURITY_BY_OPERATION, ("get", "/no-such-route"), "clientBearer")
    api_module.app.openapi_schema = None
    try:
        with pytest.raises(RuntimeError, match="not in the schema"):
            api_module.app.openapi()
    finally:
        monkeypatch.delitem(api_module._SECURITY_BY_OPERATION, ("get", "/no-such-route"))
        api_module.app.openapi_schema = None


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("DECISION_API_TOKEN", "t-client-not-a-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "t-admin-not-a-secret")
    return TestClient(api_module.app)


@pytest.mark.parametrize("method,path", sorted(CLIENT_OPS | ADMIN_OPS))
def test_every_declared_operation_really_refuses_a_missing_token(configured, method, path):
    url = path.replace("{invoice_id}", "INV-1").replace("{consent_id}", "c1")
    resp = configured.request(method.upper(), url, json={} if method == "post" else None)
    assert resp.status_code == 401, (method, path, resp.status_code)


@pytest.mark.parametrize("method,path", sorted(CLIENT_OPS | ADMIN_OPS))
def test_every_declared_operation_refuses_a_wrong_token(configured, method, path):
    url = path.replace("{invoice_id}", "INV-1").replace("{consent_id}", "c1")
    resp = configured.request(
        method.upper(), url, json={} if method == "post" else None,
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401, (method, path, resp.status_code)
