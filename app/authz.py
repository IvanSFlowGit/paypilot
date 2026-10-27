# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Who may do what: principals, roles, and the one authorisation function.

Pure logic. No database, no HTTP, no environment. The token table and the route
wiring come later; this is the part that must be right whatever they look like,
so it is the part with no way to depend on them.

The rules (docs/multi-tenancy.md, "Identity and roles"):

* A principal is a role plus, for every role except ``platform_admin``, exactly one
  tenant. There is no anonymous principal and no static one.
* ONE function, :func:`authorize`, answers every permission question and returns a
  verdict with a reason and the HTTP status a route should use. Routes do not carry
  their own role conditions.
* A caller reaching for another tenant's resource gets ``404 not_found``. Never 403,
  never an empty success: both say the resource exists, or doesn't, and a 404 says
  only that it is not theirs.
* A role may be granted only by a principal whose permissions contain it
  (:func:`can_grant`), so "not above your own role" is a computed fact and not a
  comment. Nothing mints ``platform_admin`` except ``platform_admin``.
* A role nobody checks is not a role: ``ACTIONS`` is the single table, and the tests
  assert every role appears in it and every action is checked by some route.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

PLATFORM_ADMIN = "platform_admin"
TENANT_ADMIN = "tenant_admin"
TENANT_APPROVER = "tenant_approver"
TENANT_VIEWER = "tenant_viewer"
TENANT_SERVICE = "tenant_service"

ROLES = (PLATFORM_ADMIN, TENANT_ADMIN, TENANT_APPROVER, TENANT_VIEWER, TENANT_SERVICE)
TENANT_ROLES = tuple(r for r in ROLES if r != PLATFORM_ADMIN)

_PA, _TA, _AP, _VW, _SV = ROLES

#: action -> (roles that may do it, whether it is about one specific tenant).
#: Platform-wide actions (creating tenants, the sweep across tenants) have no
#: tenant to belong to, so a tenant role is simply not permitted.
ACTIONS: dict[str, tuple[frozenset[str], bool]] = {
    "tenants.list":            (frozenset({_PA}), False),
    "tenants.create":          (frozenset({_PA}), False),
    "tenants.suspend":         (frozenset({_PA}), True),
    "tenants.resume":          (frozenset({_PA}), True),
    "tenants.offboard":        (frozenset({_PA}), True),
    "tenants.config.read":     (frozenset({_PA, _TA}), True),
    "tenants.config.update":   (frozenset({_PA, _TA}), True),
    # The send switch, the recipient allowlist (so a tenant cannot set "*"), the
    # verified from-domains and the tenant status. Resend is shared, so a tenant
    # setting these could send as another tenant.
    "tenants.config.platform_fields": (frozenset({_PA}), True),
    "secrets.rotate":          (frozenset({_PA, _TA}), True),
    "tokens.list":             (frozenset({_PA, _TA}), True),
    "tokens.create":           (frozenset({_PA, _TA}), True),
    "tokens.revoke":           (frozenset({_PA, _TA}), True),
    "approvals.list":          (frozenset({_PA, _TA, _AP, _VW}), True),
    "approvals.read_recipient": (frozenset({_PA, _TA, _AP}), True),
    "approvals.decide":        (frozenset({_PA, _TA, _AP}), True),
    "approvals.sweep_all":     (frozenset({_PA}), False),
    "approvals.sweep":         (frozenset({_PA, _TA}), True),
    "report.read":             (frozenset({_PA, _TA, _AP, _VW}), True),
    "decide.write":            (frozenset({_PA, _TA, _SV}), True),
    "decide.read":             (frozenset({_PA, _TA, _SV}), True),
    "gdpr.export":             (frozenset({_PA, _TA}), True),
    "gdpr.erase":              (frozenset({_PA, _TA}), True),
    "metrics.read":            (frozenset({_PA}), False),
}


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. Built from a token, never from a request field."""

    role: str
    tenant_id: str | None
    token_id: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")
        if self.role == PLATFORM_ADMIN and self.tenant_id is not None:
            raise ValueError("platform_admin belongs to no tenant")
        if self.role != PLATFORM_ADMIN and not self.tenant_id:
            raise ValueError(f"{self.role} must belong to exactly one tenant")
        if not self.token_id:
            raise ValueError("a principal has a token id; there is no anonymous or static principal")


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    status: int  # the HTTP status a route should answer with when not allowed

    def __bool__(self) -> bool:
        return self.allowed


_ALLOW = Verdict(True, "allowed", 200)


def permissions(role: str) -> frozenset[str]:
    """Every action a role may perform."""
    return frozenset(a for a, (roles, _) in ACTIONS.items() if role in roles)


def authorize(principal: Principal | None, action: str, tenant_id: str | None = None) -> Verdict:
    """The one permission function.

    ``tenant_id`` is the tenant the RESOURCE belongs to, resolved by the route from
    its own data, never taken from the caller.
    """
    if principal is None:
        return Verdict(False, "unauthenticated", 401)
    entry = ACTIONS.get(action)
    if entry is None:
        # A programming error, and it fails closed: an unknown action is refused,
        # not allowed because nothing said no.
        return Verdict(False, "unknown_action", 403)
    roles, tenant_scoped = entry

    if tenant_scoped:
        if tenant_id is None:
            return Verdict(False, "tenant_required", 403)
        if principal.role != PLATFORM_ADMIN and principal.tenant_id != tenant_id:
            return Verdict(False, "not_found", 404)
    elif principal.role != PLATFORM_ADMIN and tenant_id is not None and tenant_id != principal.tenant_id:
        return Verdict(False, "not_found", 404)

    if principal.role not in roles:
        return Verdict(False, "role_not_permitted", 403)
    return _ALLOW


def can_grant(granter_role: str, target_role: str) -> bool:
    """May a principal holding ``granter_role`` mint a token for ``target_role``?

    True when the granter's permissions contain the target's, and never for
    ``platform_admin`` unless the granter is one. Approver and viewer are not a
    subset of each other in the abstract, which is why this is computed from the
    table rather than from an ordering somebody has to remember.
    """
    if granter_role not in ROLES or target_role not in ROLES:
        return False
    if target_role == PLATFORM_ADMIN:
        return granter_role == PLATFORM_ADMIN
    return permissions(target_role) <= permissions(granter_role)


def unchecked_roles() -> list[str]:
    """Roles that appear in no action. A role nobody checks is not a role."""
    return [r for r in ROLES if not permissions(r)]


# -- tokens ----------------------------------------------------------------

_TOKEN_RE = re.compile(r"pp_([0-9a-f]{8})_([0-9a-f]{48})")


def new_token() -> tuple[str, str, str]:
    """``(token_id, raw_token, secret_hash)``. Only the hash is ever stored.

    Hex only, so the token cannot trip the output guard's secret-shape patterns
    (``sk-``, ``eyJ``, ``xox`` all contain letters outside a-f).
    """
    token_id = secrets.token_hex(4)
    secret = secrets.token_hex(24)
    return token_id, f"pp_{token_id}_{secret}", hash_secret(secret)


def hash_secret(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def parse_token(raw: str | None) -> tuple[str, str] | None:
    """Split a presented token into ``(token_id, secret)``, or None if malformed.

    Strict: exact shape, exact lengths, no trimming. ``fullmatch`` rather than an
    anchored ``match`` because ``$`` also matches before a trailing newline, which
    would let a newline-terminated token through. Whitespace around a header value
    is :func:`bearer_token`'s job. A malformed token is rejected before any lookup,
    so junk input never reaches the database.
    """
    m = _TOKEN_RE.fullmatch(raw or "")
    return (m.group(1), m.group(2)) if m else None


def secret_matches(secret: str, stored_hash: str) -> bool:
    """Constant-time comparison of a presented secret against the stored hash."""
    return hmac.compare_digest(hash_secret(secret), stored_hash or "")


def bearer_token(authorization_header: str | None) -> str | None:
    """The token from ``Authorization: Bearer <token>``, else None."""
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    return authorization_header[len("Bearer "):].strip() or None


def mask_email(address: str) -> str:
    """``owner@acme.test`` -> ``o***@acme.test``. For roles that may not see recipients."""
    local, sep, domain = (address or "").partition("@")
    if not sep or not local:
        return "***"
    return f"{local[0]}***@{domain}"
