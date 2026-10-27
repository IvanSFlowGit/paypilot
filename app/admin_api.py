# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The ``/admin`` routes: tenant lifecycle, tokens, configuration, GDPR.

Multi mode only; in single mode every route here is a plain 404. Every route calls
``_authorize(request, <action>, tenant)`` with a LITERAL action name from :data:`app.authz.ACTIONS`,
so the coverage test can read the routes and prove every action is checked by some route.

The routes do almost nothing themselves: they authorise, then call :mod:`app.tenant_admin` or
:mod:`app.principals`, the same code ``scripts/tenants.py`` uses.
"""

from __future__ import annotations

import dataclasses
import hmac
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app import access, authz, principals, tenant_admin, tenant_config
from app.audit import audit_security_event
from app.store import get_database, get_platform, store_for, tenancy_mode

router = APIRouter(prefix="/admin", include_in_schema=False)

_ERRORS = {
    "name_required": 400, "invalid_slug": 400, "invalid_tenant_id": 400, "tenant_exists": 409,
    "slug_taken": 409, "reason_required": 400, "not_found": 404, "already_suspended": 409,
    "not_suspended": 409, "confirmation_required": 400, "invalid_config": 400,
    "invalid_secret_field": 400, "secret_store_unavailable": 503,
}


def _multi_only() -> None:
    if tenancy_mode() != "multi":
        raise HTTPException(status_code=404, detail="Not found")


def _authorize(request: Request, action: str, tenant_id: str | None = None) -> access.Allowed:
    result = access.check(
        get_platform(), request.headers.get("authorization"), action, tenant_id=tenant_id
    )
    if isinstance(result, access.Refused):
        detail = {
            "unauthorised": "Bearer token required", "not_found": "Not found",
            "tenant_suspended": "Tenant suspended", "tenant_required": "tenant_id required",
        }.get(result.code, "Forbidden")
        raise HTTPException(status_code=result.status, detail=detail)
    return result


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail="Body too large")
    try:
        import json

        body = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return body


def _fail(exc: tenant_admin.LifecycleError) -> HTTPException:
    return HTTPException(status_code=_ERRORS.get(exc.code, 400), detail=exc.code)


def _grant_fail(exc: principals.GrantRefused) -> HTTPException:
    status = 404 if exc.reason == "not_found" else 403 if exc.reason == "role_not_grantable" else 400
    return HTTPException(status_code=status, detail=exc.reason)


# -- bootstrap ---------------------------------------------------------------


@router.post("/bootstrap")
async def bootstrap(request: Request) -> JSONResponse:
    """Mint the first platform_admin token, once, using ADMIN_TOKEN as the bootstrap value.

    ADMIN_TOKEN is not a principal. After this succeeds (or after any platform admin token has
    existed) the route refuses, and ADMIN_TOKEN opens nothing. Unset ADMIN_TOKEN is a 503: multi
    mode never fails open.
    """
    _multi_only()
    expected = (os.getenv("ADMIN_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Bootstrap is not configured")
    presented = authz.bearer_token(request.headers.get("authorization")) or ""
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        audit_security_event(
            event="bootstrap_rejected", detail="bootstrap value missing or wrong", severity="error"
        )
        raise HTTPException(status_code=401, detail="Bearer token required")
    try:
        issued = principals.bootstrap(get_platform(), label="bootstrap")
    except principals.GrantRefused:
        audit_security_event(
            event="bootstrap_rejected", detail="already bootstrapped", severity="error"
        )
        raise HTTPException(status_code=403, detail="Forbidden") from None
    audit_security_event(
        event="bootstrap_completed", detail=f"platform_admin token {issued.token_id}", severity="warning"
    )
    return JSONResponse(
        status_code=201,
        content={"token": issued.raw, "token_id": issued.token_id, "role": issued.role},
        headers={"Cache-Control": "no-store"},
    )


# -- tenants -----------------------------------------------------------------


@router.get("/tenants")
def tenants_list(request: Request) -> dict:
    _multi_only()
    _authorize(request, "tenants.list")
    return {"tenants": get_platform().list_tenants()}


@router.post("/tenants")
async def tenants_create(request: Request) -> JSONResponse:
    _multi_only()
    _authorize(request, "tenants.create")
    body = await _json(request)
    try:
        result = tenant_admin.create_tenant(
            get_platform(), get_database(), str(body.get("tenant_id", "")), body.get("name", ""),
            slug=body.get("slug"), base_url=str(body.get("base_url", "")),
        )
    except tenant_admin.LifecycleError as exc:
        raise _fail(exc) from exc
    return JSONResponse(status_code=201, content=result, headers={"Cache-Control": "no-store"})


@router.post("/tenants/{tenant_id}/suspend")
async def tenants_suspend(tenant_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "tenants.suspend", tenant_id)
    body = await _json(request)
    try:
        return tenant_admin.suspend(get_platform(), store_for(tenant_id), str(body.get("reason", "")))
    except tenant_admin.LifecycleError as exc:
        raise _fail(exc) from exc


@router.post("/tenants/{tenant_id}/resume")
def tenants_resume(tenant_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "tenants.resume", tenant_id)
    try:
        return tenant_admin.resume(get_platform(), tenant_id)
    except tenant_admin.LifecycleError as exc:
        raise _fail(exc) from exc


@router.post("/tenants/{tenant_id}/offboard")
async def tenants_offboard(tenant_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "tenants.offboard", tenant_id)
    body = await _json(request)
    try:
        return tenant_admin.offboard(
            get_platform(), store_for(tenant_id), confirm=body.get("confirm") is True
        )
    except tenant_admin.LifecycleError as exc:
        raise _fail(exc) from exc


# -- configuration -----------------------------------------------------------


def _config_view(tenant_id: str) -> dict:
    cfg = tenant_config.config_for(tenant_id, get_database())
    raw = dataclasses.asdict(cfg)
    secrets = {name: bool(raw.pop(name, "")) for name in sorted(tenant_config.SECRET_FIELDS)}
    return {"config": raw, "secrets_set": secrets}


@router.get("/tenants/{tenant_id}/config")
def config_read(tenant_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "tenants.config.read", tenant_id)
    return _config_view(tenant_id)


@router.put("/tenants/{tenant_id}/config")
async def config_update(tenant_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "tenants.config.update", tenant_id)
    body = await _json(request)
    try:
        tenant_admin.update_config(get_database(), tenant_id, body)
    except tenant_admin.LifecycleError as exc:
        raise _fail(exc) from exc
    return _config_view(tenant_id)


@router.put("/tenants/{tenant_id}/platform-fields")
async def platform_fields(tenant_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "tenants.config.platform_fields", tenant_id)
    body = await _json(request)
    recipients = body.get("allowed_recipients", [])
    domains = body.get("allowed_from_domains")
    if not isinstance(recipients, list) or (domains is not None and not isinstance(domains, list)):
        raise HTTPException(status_code=400, detail="invalid_config")
    try:
        tenant_admin.set_platform_fields(
            get_database(), tenant_id, send_enabled=body.get("send_enabled") is True,
            allowed_recipients=[str(r) for r in recipients],
            allowed_from_domains=None if domains is None else [str(d) for d in domains],
        )
    except tenant_config.TenantConfigError as exc:
        raise HTTPException(status_code=400, detail="invalid_config") from exc
    return _config_view(tenant_id)


@router.put("/tenants/{tenant_id}/secrets")
async def secrets_rotate(tenant_id: str, request: Request) -> dict:
    """Write-only: the response names what was rotated and never returns a value."""
    _multi_only()
    _authorize(request, "secrets.rotate", tenant_id)
    body = await _json(request)
    secrets = {str(k): str(v) for k, v in body.items() if isinstance(v, str)}
    try:
        return {"rotated": tenant_admin.rotate_secrets(get_database(), tenant_id, secrets)}
    except tenant_admin.LifecycleError as exc:
        raise _fail(exc) from exc


# -- tokens ------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/tokens")
def tokens_list(tenant_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "tokens.list", tenant_id)
    return {"tokens": get_platform().list_tokens(tenant_id)}


@router.post("/tenants/{tenant_id}/tokens")
async def tokens_create(tenant_id: str, request: Request) -> JSONResponse:
    _multi_only()
    allowed = _authorize(request, "tokens.create", tenant_id)
    body = await _json(request)
    try:
        issued = principals.grant(
            get_platform(), allowed.principal, role=str(body.get("role", "")),
            tenant_id=tenant_id, label=body.get("label"),
        )
    except principals.GrantRefused as exc:
        raise _grant_fail(exc) from exc
    return JSONResponse(
        status_code=201,
        content={"token": issued.raw, "token_id": issued.token_id, "role": issued.role},
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/tenants/{tenant_id}/tokens/{token_id}")
def tokens_revoke(tenant_id: str, token_id: str, request: Request) -> dict:
    _multi_only()
    allowed = _authorize(request, "tokens.revoke", tenant_id)
    row = get_platform().token_row(token_id)
    if row is None or row["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Not found")
    if not principals.revoke(get_platform(), allowed.principal, token_id, "revoked via API"):
        raise HTTPException(status_code=404, detail="Not found")
    return {"revoked": token_id}


# -- GDPR --------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/export")
def gdpr_export(tenant_id: str, customer_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "gdpr.export", tenant_id)
    return store_for(tenant_id).export_customer(customer_id)


@router.post("/tenants/{tenant_id}/erase")
async def gdpr_erase(tenant_id: str, request: Request) -> dict:
    _multi_only()
    _authorize(request, "gdpr.erase", tenant_id)
    body = await _json(request)
    customer_id = str(body.get("customer_id", "")).strip()
    if not customer_id:
        raise HTTPException(status_code=400, detail="customer_id required")
    return {"erased": store_for(tenant_id).erase_customer(customer_id)}
