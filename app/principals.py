# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Tokens to principals: issue, resolve, revoke, bootstrap.

The pure rules (roles, actions, containment, token shape) live in :mod:`app.authz`. This module
is the part that touches the ``api_tokens`` table. It has no HTTP in it.

* A token is ``pp_<id>_<secret>``. Only a SHA-256 of the secret is stored. The raw token is
  returned once, at creation, and cannot be recovered.
* :func:`resolve` turns a presented header into a :class:`~app.authz.Principal` or ``None``.
  ``None`` is one answer for every failure (malformed, unknown id, wrong secret, revoked, tenant
  offboarded or missing) so the caller cannot tell them apart, and a lookup that finds no row still
  does one hash comparison so timing does not separate "no such id" from "wrong secret".
* :func:`grant` is the only way a token is minted for a caller who is not the bootstrap path, and it
  enforces :func:`app.authz.can_grant` and the tenant scope of the granter.
* :func:`bootstrap` mints the first ``platform_admin`` token. It is one-shot: once any platform
  admin token has ever existed it refuses, whether or not that token is still live.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import authz
from app.store import Platform

#: Compared against when no row matches, so a miss costs the same one hash as a wrong secret.
_DUMMY_HASH = authz.hash_secret("no-such-token")


class GrantRefused(Exception):
    """A token was not minted. ``reason`` is a stable code, safe to show the caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class IssuedToken:
    token_id: str
    raw: str  # shown once; never stored
    role: str
    tenant_id: str | None


def _issue(
    platform: Platform, *, role: str, tenant_id: str | None, created_by: str, label: str | None
) -> IssuedToken:
    token_id, raw, secret_hash = authz.new_token()
    platform.insert_token(
        token_id=token_id, tenant_id=tenant_id, role=role, secret_hash=secret_hash,
        label=(label or None), created_by=created_by,
    )
    return IssuedToken(token_id=token_id, raw=raw, role=role, tenant_id=tenant_id)


def resolve(platform: Platform, authorization_header: str | None) -> authz.Principal | None:
    """The principal behind an ``Authorization: Bearer`` header, or None. Never raises on input."""
    parsed = authz.parse_token(authz.bearer_token(authorization_header))
    if parsed is None:
        return None
    token_id, secret = parsed
    row = platform.token_row(token_id)
    stored = row["secret_hash"] if row else _DUMMY_HASH
    matched = authz.secret_matches(secret, stored)
    if row is None or not matched or row["revoked_at"]:
        return None
    tenant_id = row["tenant_id"]
    if tenant_id is not None:
        tenant = platform.tenant_row(tenant_id)
        if tenant is None or tenant["status"] == "offboarded":
            return None
    try:
        principal = authz.Principal(role=row["role"], tenant_id=tenant_id, token_id=token_id)
    except ValueError:
        return None  # a malformed row is a refusal, not a 500
    platform.touch_token(token_id)
    return principal


def grant(
    platform: Platform,
    granter: authz.Principal,
    *,
    role: str,
    tenant_id: str | None,
    label: str | None = None,
) -> IssuedToken:
    """Mint a token on behalf of ``granter``. Raises :class:`GrantRefused` with a stable code.

    * the target role must be contained in the granter's permissions (:func:`authz.can_grant`);
    * a tenant role needs a tenant, and platform_admin must have none;
    * a tenant-scoped granter can mint only inside its own tenant;
    * the tenant must exist and not be offboarded.
    """
    if role not in authz.ROLES:
        raise GrantRefused("unknown_role")
    if not authz.can_grant(granter.role, role):
        raise GrantRefused("role_not_grantable")
    if role == authz.PLATFORM_ADMIN:
        if tenant_id is not None:
            raise GrantRefused("platform_admin_has_no_tenant")
    else:
        if not tenant_id:
            raise GrantRefused("tenant_required")
        if granter.role != authz.PLATFORM_ADMIN and granter.tenant_id != tenant_id:
            raise GrantRefused("not_found")
        tenant = platform.tenant_row(tenant_id)
        if tenant is None or tenant["status"] == "offboarded":
            raise GrantRefused("not_found")
    return _issue(
        platform, role=role, tenant_id=tenant_id, created_by=granter.token_id, label=label
    )


def revoke(
    platform: Platform, granter: authz.Principal, token_id: str, reason: str
) -> bool:
    """Revoke a token the granter may see. A tenant admin reaches only its own tenant's tokens.

    Returns False for an unknown, foreign or already-revoked token (one answer for all three).
    """
    row = platform.token_row(token_id)
    if row is None:
        return False
    if granter.role != authz.PLATFORM_ADMIN and row["tenant_id"] != granter.tenant_id:
        return False
    if row["role"] == authz.PLATFORM_ADMIN and granter.role != authz.PLATFORM_ADMIN:
        return False
    return platform.revoke_token(token_id, reason or "revoked")


def bootstrap(platform: Platform, *, label: str | None = None) -> IssuedToken:
    """Mint the first platform_admin token. Refuses once any platform admin token has existed."""
    if platform.has_platform_admin_token():
        raise GrantRefused("already_bootstrapped")
    return _issue(
        platform, role=authz.PLATFORM_ADMIN, tenant_id=None, created_by="bootstrap", label=label
    )


def issue_for_operator(
    platform: Platform, *, role: str, tenant_id: str | None, label: str | None = None
) -> IssuedToken:
    """Mint a token from the local CLI. The operator has the database, which is more than any token.

    Same shape rules as :func:`grant`, with the platform_admin granter implied. Used by
    ``scripts/tenants.py``; never reachable from HTTP.
    """
    operator = authz.Principal(role=authz.PLATFORM_ADMIN, tenant_id=None, token_id="operator")
    return grant(platform, operator, role=role, tenant_id=tenant_id, label=label)
