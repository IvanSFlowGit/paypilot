"""M4: tokens, principals, authorisation on every route, the admin API.

Multi mode is exercised through the HTTP surface wherever a route exists, and through
``access.check`` for the full role-by-action matrix.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import access, authz, principals, tenant_admin
from app.api import app
from app.store import get_database, get_platform, reset_database, store_for

ROOT = Path(__file__).resolve().parents[1]
BOOT = "bootstrap-value-123"


@pytest.fixture
def multi(monkeypatch, isolated_store):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.setenv("PAYPILOT_TENANT_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("ADMIN_TOKEN", BOOT)
    monkeypatch.delenv("DECISION_API_TOKEN", raising=False)
    reset_database()
    platform, db = get_platform(), get_database()
    out = {"platform": platform, "db": db}
    for tid in ("alpha", "beta"):
        made = tenant_admin.create_tenant(platform, db, tid, tid.title())
        out[tid] = {"admin": made["admin_token"]}
        for role in ("tenant_approver", "tenant_viewer", "tenant_service"):
            out[tid][role.split("_")[1]] = principals.issue_for_operator(
                platform, role=role, tenant_id=tid
            ).raw
    out["root"] = principals.bootstrap(platform).raw
    yield out
    reset_database()


def _h(token: str | None) -> dict:
    return {} if token is None else {"Authorization": f"Bearer {token}"}


client = TestClient(app, raise_server_exceptions=False)


# -- principals --------------------------------------------------------------


def test_a_token_resolves_to_its_role_and_tenant(multi):
    p = principals.resolve(multi["platform"], f"Bearer {multi['alpha']['admin']}")
    assert (p.role, p.tenant_id) == ("tenant_admin", "alpha")


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer x", "Basic abc", "Bearer pp_zz_zz"])
def test_malformed_headers_resolve_to_nothing(multi, header):
    assert principals.resolve(multi["platform"], header) is None


def test_a_wrong_secret_and_an_unknown_id_are_the_same_answer(multi):
    token = multi["alpha"]["admin"]
    tid, secret = authz.parse_token(token)
    wrong = f"pp_{tid}_{'0' * 48}"
    unknown = f"pp_{'0' * 8}_{secret}"
    assert principals.resolve(multi["platform"], f"Bearer {wrong}") is None
    assert principals.resolve(multi["platform"], f"Bearer {unknown}") is None


def test_the_secret_is_stored_hashed_only(multi):
    token = multi["alpha"]["admin"]
    tid, secret = authz.parse_token(token)
    row = multi["platform"].token_row(tid)
    assert secret not in json.dumps(row) and row["secret_hash"] == authz.hash_secret(secret)
    assert all("secret_hash" not in t for t in multi["platform"].list_tokens("alpha"))


def test_a_revoked_token_stops_working_at_once(multi):
    token = multi["alpha"]["viewer"]
    tid, _ = authz.parse_token(token)
    assert principals.resolve(multi["platform"], f"Bearer {token}")
    assert multi["platform"].revoke_token(tid, "test")
    assert principals.resolve(multi["platform"], f"Bearer {token}") is None


def test_an_offboarded_tenants_tokens_resolve_to_nothing(multi):
    tenant_admin.offboard(multi["platform"], store_for("beta"), confirm=True)
    assert principals.resolve(multi["platform"], f"Bearer {multi['beta']['admin']}") is None


def test_last_used_is_recorded(multi):
    token = multi["alpha"]["admin"]
    tid, _ = authz.parse_token(token)
    assert multi["platform"].token_row(tid)["last_used_at"] is None
    principals.resolve(multi["platform"], f"Bearer {token}")
    assert multi["platform"].token_row(tid)["last_used_at"]


# -- grant and containment ---------------------------------------------------


def _p(role, tenant):
    return authz.Principal(role=role, tenant_id=tenant, token_id="t")


def test_a_tenant_admin_mints_contained_roles_inside_its_own_tenant(multi):
    admin = _p("tenant_admin", "alpha")
    for role in ("tenant_approver", "tenant_viewer", "tenant_service", "tenant_admin"):
        assert principals.grant(multi["platform"], admin, role=role, tenant_id="alpha").raw


def test_nothing_mints_platform_admin_except_platform_admin(multi):
    admin = _p("tenant_admin", "alpha")
    with pytest.raises(principals.GrantRefused) as e:
        principals.grant(multi["platform"], admin, role="platform_admin", tenant_id=None)
    assert e.value.reason == "role_not_grantable"
    root = _p("platform_admin", None)
    assert principals.grant(multi["platform"], root, role="platform_admin", tenant_id=None).raw


@pytest.mark.parametrize("granter", ["tenant_approver", "tenant_viewer", "tenant_service"])
def test_a_lesser_role_cannot_mint_a_greater_one(multi, granter):
    with pytest.raises(principals.GrantRefused):
        principals.grant(multi["platform"], _p(granter, "alpha"), role="tenant_admin", tenant_id="alpha")


def test_a_tenant_admin_cannot_mint_in_another_tenant(multi):
    with pytest.raises(principals.GrantRefused) as e:
        principals.grant(multi["platform"], _p("tenant_admin", "alpha"), role="tenant_viewer", tenant_id="beta")
    assert e.value.reason == "not_found"


def test_bootstrap_is_one_shot_even_after_revocation(multi):
    # the fixture already bootstrapped; revoke it and try again
    tid, _ = authz.parse_token(multi["root"])
    multi["platform"].revoke_token(tid, "test")
    with pytest.raises(principals.GrantRefused) as e:
        principals.bootstrap(multi["platform"])
    assert e.value.reason == "already_bootstrapped"


# -- the full matrix through access.check -----------------------------------


def _token_for(multi, role, tenant="alpha"):
    if role == "platform_admin":
        return multi["root"]
    return multi[tenant]["admin" if role == "tenant_admin" else role.split("_")[1]]


@pytest.mark.parametrize("action", sorted(authz.ACTIONS))
@pytest.mark.parametrize("role", authz.ROLES)
def test_check_agrees_with_authorize_for_every_role_and_action(multi, role, action):
    roles, scoped = authz.ACTIONS[action]
    tenant = "alpha" if scoped else None
    result = access.check(multi["platform"], f"Bearer {_token_for(multi, role)}", action, tenant_id=tenant)
    if role in roles and (scoped or role == "platform_admin"):
        assert isinstance(result, access.Allowed), (role, action, result)
        if scoped:
            assert result.tenant_id == "alpha"
    else:
        assert isinstance(result, access.Refused), (role, action)
        assert result.status in (403, 404)


@pytest.mark.parametrize("role", authz.TENANT_ROLES)
def test_a_tenant_principal_naming_another_tenant_gets_404(multi, role):
    r = access.check(multi["platform"], f"Bearer {_token_for(multi, role)}", "report.read", tenant_id="beta")
    assert isinstance(r, access.Refused) and (r.status, r.code) == (404, "not_found")


def test_no_token_is_401_and_a_platform_admin_naming_no_tenant_is_refused(multi):
    assert access.check(multi["platform"], None, "report.read", tenant_id="alpha").status == 401
    r = access.check(multi["platform"], f"Bearer {multi['root']}", "report.read")
    assert isinstance(r, access.Refused)


def test_a_platform_admin_naming_an_unknown_tenant_gets_404(multi):
    r = access.check(multi["platform"], f"Bearer {multi['root']}", "report.read", tenant_id="nosuch")
    assert (r.status, r.code) == (404, "not_found")


def test_a_suspended_tenant_is_refused_send_capable_actions_only(multi):
    tenant_admin.suspend(multi["platform"], store_for("alpha"), "test")
    svc = f"Bearer {multi['alpha']['service']}"
    assert access.check(multi["platform"], svc, "decide.write").code == "tenant_suspended"
    assert isinstance(access.check(multi["platform"], svc, "decide.read"), access.Allowed)


# -- coverage: every role granted, every action checked by a route ----------


def _literal_actions_checked_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name in {"_authorize", "_admin_store", "_decision_client", "check", "authorize"}:
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    if isinstance(arg, ast.Constant) and arg.value in authz.ACTIONS:
                        found.add(arg.value)
        if isinstance(node, ast.FunctionDef):  # default-argument actions, e.g. _decision_client
            for d in node.args.defaults:
                if isinstance(d, ast.Constant) and d.value in authz.ACTIONS:
                    found.add(d.value)
    return found


def test_every_action_is_checked_by_some_route_and_every_role_can_do_something():
    seen = set()
    for f in ("app/api.py", "app/admin_api.py"):
        seen |= _literal_actions_checked_in(ROOT / f)
    assert set(authz.ACTIONS) - seen == set(), sorted(set(authz.ACTIONS) - seen)
    assert authz.unchecked_roles() == []


# -- HTTP: bootstrap ---------------------------------------------------------


def test_bootstrap_flow_over_http(monkeypatch, isolated_store):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.setenv("ADMIN_TOKEN", BOOT)
    reset_database()
    try:
        assert client.post("/admin/bootstrap").status_code == 401
        assert client.post("/admin/bootstrap", headers=_h("wrong")).status_code == 401
        ok = client.post("/admin/bootstrap", headers=_h(BOOT))
        assert ok.status_code == 201 and ok.json()["role"] == "platform_admin"
        assert ok.headers["cache-control"] == "no-store"
        # one shot: the bootstrap value now opens nothing
        assert client.post("/admin/bootstrap", headers=_h(BOOT)).status_code == 403
        assert client.get("/admin/tenants", headers=_h(BOOT)).status_code == 401
        assert client.get("/admin/tenants", headers=_h(ok.json()["token"])).status_code == 200
    finally:
        reset_database()


def test_bootstrap_is_503_when_no_value_is_set_and_404_in_single_mode(monkeypatch, isolated_store):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    reset_database()
    try:
        assert client.post("/admin/bootstrap", headers=_h("anything")).status_code == 503
        monkeypatch.setenv("PAYPILOT_TENANCY", "single")
        assert client.post("/admin/bootstrap", headers=_h("anything")).status_code == 404
        assert client.get("/admin/tenants").status_code == 404
    finally:
        reset_database()


# -- HTTP: the admin API -----------------------------------------------------


def test_admin_tenant_lifecycle_over_http(multi):
    root = _h(multi["root"])
    made = client.post("/admin/tenants", headers=root, json={"tenant_id": "gamma", "name": "Gamma"})
    assert made.status_code == 201
    body = made.json()
    assert body["admin_token"].startswith("pp_") and "/t/gamma/webhooks/stripe" in body["webhooks"]["stripe"]
    assert client.post("/admin/tenants", headers=root, json={"tenant_id": "gamma", "name": "G"}).status_code == 409
    assert client.post("/admin/tenants/gamma/suspend", headers=root, json={}).status_code == 400
    assert client.post("/admin/tenants/gamma/suspend", headers=root, json={"reason": "unpaid"}).status_code == 200
    assert client.post("/admin/tenants/gamma/resume", headers=root).json() == {"status": "active"}
    assert client.post("/admin/tenants/gamma/offboard", headers=root, json={}).status_code == 400
    assert client.post("/admin/tenants/gamma/offboard", headers=root, json={"confirm": True}).status_code == 400
    # the erase refuses until the tenant has been taken out of service
    erase = {"confirm": True, "export_taken": True}
    assert client.post("/admin/tenants/gamma/offboard", headers=root, json=erase).status_code == 409
    assert client.post("/admin/tenants/gamma/offboard/start", headers=root).status_code == 200
    kept = client.get("/admin/tenants/gamma/export-all", headers=root)
    assert kept.status_code == 200 and kept.headers["cache-control"] == "no-store"
    off = client.post("/admin/tenants/gamma/offboard", headers=root, json=erase)
    assert off.status_code == 200 and off.json()["status"] == "offboarded"
    assert client.get("/admin/tenants/gamma/config", headers=root).status_code == 404


def test_a_tenant_admin_cannot_use_platform_routes_or_other_tenants(multi):
    a = _h(multi["alpha"]["admin"])
    assert client.get("/admin/tenants", headers=a).status_code == 403
    assert client.post("/admin/tenants", headers=a, json={"tenant_id": "x", "name": "X"}).status_code == 403
    assert client.get("/admin/tenants/beta/config", headers=a).status_code == 404
    assert client.get("/admin/tenants/alpha/config", headers=a).status_code == 200
    assert client.put("/admin/tenants/alpha/platform-fields", headers=a, json={"send_enabled": True}).status_code == 403


def test_secret_rotation_is_write_only_and_never_echoes(multi):
    a = _h(multi["alpha"]["admin"])
    r = client.put("/admin/tenants/alpha/secrets", headers=a, json={"stripe_webhook_secret": "whsec_TESTVALUE1"})
    assert r.status_code == 200 and r.json() == {"rotated": ["stripe_webhook_secret"]}
    assert "TESTVALUE1" not in r.text
    view = client.get("/admin/tenants/alpha/config", headers=a)
    assert "TESTVALUE1" not in view.text
    assert view.json()["secrets_set"]["stripe_webhook_secret"] is True
    assert client.put("/admin/tenants/alpha/secrets", headers=a, json={"nope": "x"}).status_code == 400


def test_a_tenant_cannot_set_platform_only_fields_through_the_config_route(multi):
    a = _h(multi["alpha"]["admin"])
    r = client.put("/admin/tenants/alpha/config", headers=a, json={"send_enabled": True})
    assert r.status_code == 400
    root = _h(multi["root"])
    ok = client.put("/admin/tenants/alpha/platform-fields", headers=root,
                    json={"send_enabled": True, "allowed_recipients": ["ap@acme.test"]})
    assert ok.status_code == 200 and ok.json()["config"]["send_enabled"] is True


def test_token_routes_enforce_containment_and_scope(multi):
    a = _h(multi["alpha"]["admin"])
    made = client.post("/admin/tenants/alpha/tokens", headers=a, json={"role": "tenant_viewer", "label": "x"})
    assert made.status_code == 201
    assert client.post("/admin/tenants/alpha/tokens", headers=a, json={"role": "platform_admin"}).status_code == 403
    viewer = _h(multi["alpha"]["viewer"])
    assert client.post("/admin/tenants/alpha/tokens", headers=viewer, json={"role": "tenant_viewer"}).status_code == 403
    assert client.post("/admin/tenants/beta/tokens", headers=a, json={"role": "tenant_viewer"}).status_code == 404
    tid = made.json()["token_id"]
    assert client.delete(f"/admin/tenants/beta/tokens/{tid}", headers=_h(multi["beta"]["admin"])).status_code == 404
    assert client.delete(f"/admin/tenants/alpha/tokens/{tid}", headers=a).status_code == 200
    assert client.get("/admin/tenants/alpha/tokens", headers=a).status_code == 200
    assert client.get("/approvals", headers=_h(made.json()["token"])).status_code == 401


def test_gdpr_export_and_erase_are_tenant_scoped(multi):
    for s in (store_for("alpha"), store_for("beta")):
        s.record_failure(invoice_id="in_1", customer_id="c1", amount_minor=1000, currency="eur",
                         failure_code="insufficient_funds")
    a = _h(multi["alpha"]["admin"])
    assert client.get("/admin/tenants/alpha/export?customer_id=c1", headers=a).status_code == 200
    assert client.get("/admin/tenants/beta/export?customer_id=c1", headers=a).status_code == 404
    assert client.post("/admin/tenants/alpha/erase", headers=a, json={}).status_code == 400
    assert client.post("/admin/tenants/alpha/erase", headers=a, json={"customer_id": "c1"}).status_code == 200


# -- HTTP: existing routes under multi mode ---------------------------------


def test_admin_token_opens_nothing_in_multi_mode(multi):
    for path in ("/metrics", "/approvals", "/report", "/recovery-report"):
        assert client.get(path, headers=_h(BOOT)).status_code == 401, path


def test_metrics_is_platform_only_and_reports_are_tenant_scoped(multi):
    assert client.get("/metrics", headers=_h(multi["root"])).status_code == 200
    assert client.get("/metrics", headers=_h(multi["alpha"]["admin"])).status_code == 403
    assert client.get("/recovery-report", headers=_h(multi["alpha"]["viewer"])).status_code == 200
    assert client.get("/recovery-report", headers=_h(multi["alpha"]["service"])).status_code == 403
    assert client.get("/recovery-report?tenant_id=beta", headers=_h(multi["alpha"]["admin"])).status_code == 404
    assert client.get("/recovery-report", headers=_h(multi["root"])).status_code == 400
    assert client.get("/recovery-report?tenant_id=beta", headers=_h(multi["root"])).status_code == 200


def test_approvals_list_is_scoped_and_masks_recipients_for_viewers(multi):
    assert store_for("alpha").create_approval(
        invoice_id="in_a", channel="email", recipient="owner@acme.test", subject="s", body="b",
        token_hash="t", expires_at="2999-01-01T00:00:00",
    )
    admin = client.get("/approvals", headers=_h(multi["alpha"]["admin"])).json()["approvals"]
    viewer = client.get("/approvals", headers=_h(multi["alpha"]["viewer"])).json()["approvals"]
    other = client.get("/approvals", headers=_h(multi["beta"]["admin"])).json()["approvals"]
    assert admin and admin[0]["recipient"] == "owner@acme.test"
    assert viewer[0]["recipient"] == "o***@acme.test"
    assert other == []
    assert client.get("/approvals", headers=_h(multi["alpha"]["service"])).status_code == 403


def test_sweep_all_is_platform_only(multi):
    assert client.post("/approvals/sweep", headers=_h(multi["alpha"]["admin"])).status_code == 403
    assert client.post("/approvals/sweep", headers=_h(multi["root"])).status_code == 200
    assert client.post("/approvals/sweep?tenant_id=alpha", headers=_h(multi["alpha"]["admin"])).status_code == 200
    assert client.post("/approvals/sweep?tenant_id=beta", headers=_h(multi["alpha"]["admin"])).status_code == 404


def test_decision_routes_use_service_tokens_and_the_tenant_is_the_client(multi, monkeypatch):
    monkeypatch.setenv("DECISION_API_TOKEN", "legacy-token-value")  # retired in multi mode
    assert client.post("/decide", headers=_h("legacy-token-value"), json={}).status_code == 401
    svc = _h(multi["alpha"]["service"])
    r = client.post("/decide", headers=svc, json={"invoice_id": "in_1", "attempt": 1,
                                                  "prior_failures": 0, "failure_code": "insufficient_funds"})
    assert r.status_code == 200, r.text
    mine = client.get("/decisions/in_1", headers=svc)
    theirs = client.get("/decisions/in_1", headers=_h(multi["beta"]["service"]))
    assert mine.status_code == 200 and json.dumps(mine.json()).count("in_1") >= 1
    assert "in_1" not in json.dumps(theirs.json().get("decisions", theirs.json()))
    assert client.post("/decide", headers=_h(multi["alpha"]["viewer"]), json={}).status_code == 403
    assert client.post("/decide", headers=_h(multi["root"]), json={}).status_code == 403


def test_demo_only_routes_are_not_available_in_multi_mode(multi):
    assert client.get("/config").status_code in (404, 500)
