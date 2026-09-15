# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""One-shot database bootstrap for the decision slice on RDS.

Runs as its own Lambda function, invoked by Terraform on apply and reachable
from no HTTP route. It is the only code that holds the RDS master password.
It applies the schema, creates the ``paypilot_app`` role the public decision
function logs in as, grants that role SELECT and INSERT on the audit table and
nothing else, and then PROVES the grants by logging in as the role.

The proof is what makes "append-only" a database property rather than a claim
about the application code: INSERT and SELECT must succeed, and UPDATE, DELETE,
TRUNCATE and CREATE must each fail with Postgres error 42501
(insufficient_privilege). Any other outcome, including a different error, fails
the invocation, which fails the Terraform apply.

Nothing secret is returned or logged: the result carries check names and
booleans only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path

from app.decision_audit import open_connection, split_sql_statements

log = logging.getLogger("paypilot.bootstrap")
log.setLevel(logging.INFO)

APP_ROLE = "paypilot_app"
APP_PASSWORD_ENV = "APP_DB_PASSWORD"

_SCHEMA_PATH = Path(__file__).resolve().parent / "decision_schema.sql"

# The password is embedded in ALTER ROLE, which cannot take a bind parameter.
# Accepting hex only means no value can close the quote.
_PASSWORD_RE = re.compile(r"^[0-9a-f]{32,128}$")
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

INSUFFICIENT_PRIVILEGE = "42501"

#: Statements the app role must be allowed to run, each inside a rolled-back
#: transaction so the probe leaves no row behind.
_MUST_SUCCEED = {
    "insert": (
        "INSERT INTO decision_audit (id, invoice_id, rule_fired, decided_at, input, decision) "
        "VALUES (:id, 'in_bootstrap_probe', 'probe', '1970-01-01T00:00:00.000+00:00', '{}', '{}')"
    ),
    "select": "SELECT count(*) FROM decision_audit",
}

#: Statements the app role must be refused, with insufficient_privilege and no other error.
_MUST_BE_DENIED = {
    "update": "UPDATE decision_audit SET rule_fired = rule_fired WHERE false",
    "delete": "DELETE FROM decision_audit WHERE false",
    "truncate": "TRUNCATE decision_audit",
    "create": "CREATE TABLE bootstrap_probe_should_not_exist (x int)",
}


def validate_password(password: str | None) -> str:
    if not password or not _PASSWORD_RE.fullmatch(password):
        raise ValueError(f"{APP_PASSWORD_ENV} must be 32-128 lowercase hex characters")
    return password


def validate_identifier(name: str | None) -> str:
    if not name or not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError("database name must be a plain lowercase identifier")
    return name


def role_statements(database: str, app_password: str) -> list[str]:
    """The role and grant statements, after the schema. Inputs are validated first."""
    database = validate_identifier(database)
    app_password = validate_password(app_password)
    return [
        (
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
            f"CREATE ROLE {APP_ROLE} LOGIN; "
            "END IF; END $$"
        ),
        f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{app_password}'",
        f"REVOKE ALL ON decision_audit FROM {APP_ROLE}",
        f"REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}",
        f"GRANT CONNECT ON DATABASE {database} TO {APP_ROLE}",
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
        f"GRANT SELECT, INSERT ON decision_audit TO {APP_ROLE}",
    ]


def error_code(exc: BaseException) -> str | None:
    """The SQLSTATE of a pg8000 DatabaseError (its first arg is a field dict), else None."""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], dict):
        return args[0].get("C")
    return None


def _probe(conn, sql: str, **params) -> tuple[bool, str | None]:
    """Run ``sql`` in a transaction that is always rolled back. Returns (succeeded, sqlstate)."""
    conn.run("BEGIN")
    try:
        conn.run(sql, **params)
        return True, None
    except Exception as exc:  # noqa: BLE001 - classified by SQLSTATE below
        return False, error_code(exc)
    finally:
        conn.run("ROLLBACK")


def verify_grants(conn) -> dict:
    """Probe the app role's privileges. Returns check name -> passed."""
    checks = {}
    for name, sql in _MUST_SUCCEED.items():
        params = {"id": uuid.uuid4().hex} if ":id" in sql else {}
        ok, _ = _probe(conn, sql, **params)
        checks[f"{name}_allowed"] = ok
    for name, sql in _MUST_BE_DENIED.items():
        ok, code = _probe(conn, sql)
        checks[f"{name}_denied"] = (not ok) and code == INSUFFICIENT_PRIVILEGE
    return checks


def run(env=None) -> dict:
    env = env if env is not None else os.environ
    app_password = validate_password(env.get(APP_PASSWORD_ENV))
    database = validate_identifier(env.get("PGDATABASE"))

    master = open_connection(env)
    try:
        for statement in split_sql_statements(_SCHEMA_PATH.read_text(encoding="utf-8")):
            master.run(statement)
        for statement in role_statements(database, app_password):
            master.run(statement)
    finally:
        master.close()

    app_env = {**env, "PGUSER": APP_ROLE, "PGPASSWORD": app_password}
    app = open_connection(app_env)
    try:
        checks = verify_grants(app)
    finally:
        app.close()

    result = {"role": APP_ROLE, "checks": checks, "ok": all(checks.values())}
    log.info(json.dumps({"event": "bootstrap", **result}))
    if not result["ok"]:
        # Raising makes the invocation a FunctionError, which fails terraform apply.
        raise RuntimeError(f"app role grants not as required: {json.dumps(checks, sort_keys=True)}")
    return result


def handler(event, context):
    """Lambda entry point. The event is ignored; Terraform changes it only to force a re-run."""
    return run()
