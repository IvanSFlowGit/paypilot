# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Multi-mode request authorisation: header in, verdict out. No FastAPI in here.

``check`` is the only function a route calls. It authenticates the bearer token, applies
:func:`app.authz.authorize`, and returns either an :class:`Allowed` carrying the principal and the
tenant the action applies to, or a :class:`Refused` with a status and a stable code.

* Unknown token, revoked token, malformed header: 401 ``unauthorised``. One answer.
* A resource in another tenant, or an unknown tenant named by a platform admin: 404 ``not_found``.
* A role that may not do the action: 403 ``forbidden``.
* A suspended tenant's service and approver actions that could cause a send: 403 ``tenant_suspended``.

Every refusal raises one audit event naming the action and the code, never the token.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import authz, principals
from app.audit import audit_security_event
from app.store import Platform

#: Actions that can lead to a customer being mailed. A suspended tenant is refused these.
SEND_CAPABLE_ACTIONS = frozenset({"decide.write", "approvals.decide"})


@dataclass(frozen=True)
class Allowed:
    principal: authz.Principal
    tenant_id: str | None  # the tenant the action applies to; None for platform-wide actions


@dataclass(frozen=True)
class Refused:
    status: int
    code: str  # unauthorised | not_found | forbidden | tenant_suspended | tenant_required


_CODES = {
    "unauthenticated": ("unauthorised", 401),
    "not_found": ("not_found", 404),
    "role_not_permitted": ("forbidden", 403),
    "tenant_required": ("tenant_required", 400),
    "unknown_action": ("forbidden", 403),
}


def _refuse(action: str, status: int, code: str) -> Refused:
    audit_security_event(
        event="authz_denied",
        detail=f"{code} on {action}",
        severity="error" if status == 401 else "warning",
    )
    return Refused(status, code)


def check(
    platform: Platform,
    authorization_header: str | None,
    action: str,
    *,
    tenant_id: str | None = None,
) -> Allowed | Refused:
    """Authenticate and authorise one request.

    ``tenant_id`` is what the CALLER named (a path or query value), used only by a platform_admin.
    A tenant principal's tenant is its own, always: whatever it names is ignored when it matches
    and is a 404 when it does not.
    """
    principal = principals.resolve(platform, authorization_header)
    entry = authz.ACTIONS.get(action)
    if principal is None:
        return _refuse(action, 401, "unauthorised")

    if entry is not None and entry[1]:  # tenant-scoped action
        if principal.role == authz.PLATFORM_ADMIN:
            resource_tenant = tenant_id
        else:
            if tenant_id is not None and tenant_id != principal.tenant_id:
                return _refuse(action, 404, "not_found")
            resource_tenant = principal.tenant_id
    else:
        resource_tenant = None if principal.role == authz.PLATFORM_ADMIN else tenant_id

    verdict = authz.authorize(principal, action, resource_tenant)
    if not verdict:
        code, status = _CODES.get(verdict.reason, ("forbidden", 403))
        return _refuse(action, status, code)

    if resource_tenant is not None:
        row = platform.tenant_row(resource_tenant)
        if row is None or row["status"] == "offboarded":
            return _refuse(action, 404, "not_found")
        if row["status"] == "suspended" and action in SEND_CAPABLE_ACTIONS:
            return _refuse(action, 403, "tenant_suspended")
    return Allowed(principal=principal, tenant_id=resource_tenant)
