# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""M9: one database login per tenant, so row-level security keys on WHO is connected.

STATUS: WRITTEN. The SQL is checked offline against a fake driver (tests/test_pg_roles.py);
``scripts/pg_rls_check.py`` proves it on a real Postgres. Off unless ``PAYPILOT_PG_TENANT_ROLES=1``.

WHY. With the shared ``paypilot_tenant`` role, the tenant is a transaction-local setting the
application makes, and a session running injected SQL can make the same setting. Here each tenant
logs in as ``pp_t_<tenant>``. ``pp_current_tenant()`` (pg_schema.sql) reads the tenant from the role
name, which a session cannot change: these roles are members of nothing, so ``SET ROLE`` to another
tenant's role or to the shared role is refused.

PASSWORDS ARE DERIVED, NOT STORED: HMAC-SHA256 over the tenant id under ``PAYPILOT_PG_ROLE_KEY``.
The application re-derives one at connect time; nothing is written to the database or a file. The
derivation cannot be reversed to the key, and one tenant's password reveals nothing about another's.
Rotating the key and re-running ``sync`` re-passwords every role.

CREATING ROLES IS NOT THE APPLICATION'S JOB. It takes CREATEROLE, which the app must never hold.
``scripts/pg_roles.py`` does it with an ADMIN connection (``PAYPILOT_PG_ADMIN_USER/PASSWORD``), run
by the operator after a tenant is created or offboarded. A tenant whose role does not exist cannot
connect: that is the fail-closed outcome, and it surfaces as a login error, not as another tenant's data.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets

PREFIX = "pp_t_"
#: Postgres truncates identifiers beyond 63 bytes without error; a truncated name could collide.
MAX_ROLE_LEN = 63
KEY_ENV = "PAYPILOT_PG_ROLE_KEY"
ENABLE_ENV = "PAYPILOT_PG_TENANT_ROLES"
MIN_KEY_LEN = 32

_TENANT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: Tables the per-tenant role may touch, and the only ones: platform tables are never granted.
TENANT_TABLES = ("failures", "events", "messages", "transitions", "approvals", "early_paid")


class RoleError(ValueError):
    """The tenant id or key cannot be turned into a role safely."""


def roles_enabled() -> bool:
    return os.getenv(ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def role_name(tenant_id: str) -> str:
    """``pp_t_<tenant>``. The tenant id charset is already [a-z0-9_], so the name needs no escaping,
    and it is quoted anyway everywhere it is used."""
    if not isinstance(tenant_id, str) or not _TENANT_RE.match(tenant_id):
        raise RoleError("invalid tenant id")
    name = PREFIX + tenant_id
    if len(name) > MAX_ROLE_LEN:
        raise RoleError(f"tenant id too long for a database role ({len(name)} > {MAX_ROLE_LEN})")
    return name


def tenant_from_role(name: str) -> str | None:
    return name[len(PREFIX):] if name.startswith(PREFIX) else None


def role_key(env=None) -> bytes:
    raw = (env if env is not None else os.environ).get(KEY_ENV, "")
    if len(raw) < MIN_KEY_LEN:
        raise RoleError(f"{KEY_ENV} must be set to at least {MIN_KEY_LEN} characters")
    return raw.encode("utf-8")


def derive_password(tenant_id: str, key: bytes | None = None) -> str:
    """40 lowercase hex characters, stable for a (key, tenant) pair, different for every tenant."""
    role_name(tenant_id)  # validates
    k = key if key is not None else role_key()
    return hmac.new(k, b"paypilot-pg-role:" + tenant_id.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def _q(ident: str) -> str:
    """Quote an identifier. The name is regex-validated first; the quote is defence in depth."""
    return '"' + ident.replace('"', '""') + '"'


_SCRAM_ITERATIONS = 4096
_VERIFIER_RE = re.compile(r"SCRAM-SHA-256\$\d{4,6}:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+")


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = _SCRAM_ITERATIONS) -> str:
    """The SCRAM-SHA-256 verifier Postgres stores for a password (RFC 5802 / 7677).

    Sent in place of the password, so the cleartext never appears in the statement text, which the
    server writes to its log when a statement fails or when ``log_statement`` covers DDL. Postgres
    stores a value in this form as-is. The salt is random per call; the login password stays the
    derived one, so the app's re-derivation still matches.
    """
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def provision_sql(tenant_id: str, verifier: str, exists: bool) -> list[str]:
    """The statements that make (or refresh) one tenant's login.

    ``verifier`` is a SCRAM-SHA-256 verifier, never a cleartext password. DDL cannot take bind
    parameters, so it goes in as a literal; the pattern admits only base64 and the fixed prefix.
    ``RESET ALL`` clears any role-level setting a previous session persisted.
    """
    if not _VERIFIER_RE.fullmatch(verifier):
        raise RoleError("password verifier is not the SCRAM-SHA-256 form")
    r = _q(role_name(tenant_id))
    attrs = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    verb = "ALTER ROLE" if exists else "CREATE ROLE"
    return [
        f"{verb} {r} WITH LOGIN PASSWORD '{verifier}' {attrs}",
        f"ALTER ROLE {r} RESET ALL",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {r}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TENANT_TABLES)} TO {r}",
        f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {r}",
    ]


def deprovision_sql(tenant_id: str) -> list[str]:
    r = _q(role_name(tenant_id))
    return [
        f"ALTER ROLE {r} NOLOGIN",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :n",
        f"DROP OWNED BY {r}",
        f"DROP ROLE IF EXISTS {r}",
    ]


#: Is CREATE on schema public held by PUBLIC (the default before PG15)? A tenant that can create
#: functions there can shadow what pp_current_tenant() calls. PG15+ revokes it by default.
_PUBLIC_CREATE_SQL = (
    "SELECT 1 FROM pg_namespace n, "
    "aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
    "WHERE n.nspname = 'public' AND a.grantee = 0 AND a.privilege_type = 'CREATE'"
)


def provision(admin, tenant_id: str, key: bytes | None = None) -> str:
    """Create or refresh the role for one tenant on an ADMIN pg8000.native connection.

    Refuses, before changing anything, when PUBLIC can create objects in ``public`` or when an
    existing role of this name is a member of any other role (a leftover or hand-made role could
    carry a membership that makes SET ROLE a way across). Re-passwording and ``RESET ALL`` run on
    every call, so ``sync`` after an incident repairs a role whose password was changed from inside.
    """
    name = role_name(tenant_id)
    if admin.run(_PUBLIC_CREATE_SQL):
        raise RoleError("PUBLIC can CREATE in schema public; run REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    exists = bool(admin.run("SELECT 1 FROM pg_roles WHERE rolname = :n", n=name))
    if exists:
        member_of = admin.run(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = :n", n=name)[0][0]
        if member_of:
            raise RoleError(f"{name} is a member of another role; drop it and re-provision")
    verifier = scram_verifier(derive_password(tenant_id, key))
    for stmt in provision_sql(tenant_id, verifier, exists):
        admin.run(stmt)
    if admin.run("SELECT 1 WHERE has_schema_privilege(:n, 'public', 'CREATE')", n=name):
        raise RoleError(f"{name} can CREATE in schema public after provisioning")
    return name


def deprovision(admin, tenant_id: str) -> str:
    """Log out, revoke and drop one tenant's role. Idempotent: a role already gone is success."""
    name = role_name(tenant_id)
    if not admin.run("SELECT 1 FROM pg_roles WHERE rolname = :n", n=name):
        return name
    for stmt in deprovision_sql(tenant_id):
        admin.run(stmt, n=name) if ":n" in stmt else admin.run(stmt)
    return name
