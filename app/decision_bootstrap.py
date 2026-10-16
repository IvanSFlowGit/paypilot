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

from app.decision_audit import open_connection, set_client, split_sql_statements

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

#: Every probe declares a tenant, because row level security refuses a write whose
#: client_id does not match paypilot.client_id and the grant probes would otherwise
#: start measuring RLS instead of the grant they were written for. The INSERT probe
#: below previously omitted client_id and took the column DEFAULT '', which nullif
#: maps to NULL, so it would have been refused with 42501 the moment the policies
#: landed and failed the apply for the wrong reason.
PROBE_CLIENT = "cl_bootstrap_probe"

#: The two clients the isolation probe uses. A row written as A must be invisible
#: to B with NO client_id predicate in the query at all, which is the only thing
#: that distinguishes row level security from the application-side filter.
RLS_CLIENT_A = "cl_rls_probe_a"
RLS_CLIENT_B = "cl_rls_probe_b"

#: Statements the app role must be allowed to run, each inside a rolled-back
#: transaction so the probe leaves no row behind.
_MUST_SUCCEED = {
    "insert": (
        "INSERT INTO decision_audit "
        "(id, invoice_id, client_id, rule_fired, decided_at, input, decision) "
        "VALUES (:id, 'in_bootstrap_probe', :client, 'probe', "
        "'1970-01-01T00:00:00.000+00:00', '{}', '{}')"
    ),
    "select": "SELECT count(*) FROM decision_audit",
    # One probe per GRANTED TABLE, never one per table somebody remembered. A probe
    # list shorter than the grant list cannot report a missing grant.
    "insert_outcome": (
        "INSERT INTO decision_outcomes "
        "(id, invoice_id, client_id, outcome, reported_at, report_key, source) "
        "VALUES (:id, 'in_bootstrap_probe', :client, 'paid', "
        "'1970-01-01T00:00:00.000+00:00', :id, 'probe')"
    ),
    "select_outcome": "SELECT count(*) FROM decision_outcomes",
}

#: Statements the app role must be refused, with insufficient_privilege and no other error.
_MUST_BE_DENIED = {
    "update": "UPDATE decision_audit SET rule_fired = rule_fired WHERE false",
    "delete": "DELETE FROM decision_audit WHERE false",
    "truncate": "TRUNCATE decision_audit",
    "create": "CREATE TABLE bootstrap_probe_should_not_exist (x int)",
    "update_outcome": "UPDATE decision_outcomes SET outcome = outcome WHERE false",
    "delete_outcome": "DELETE FROM decision_outcomes WHERE false",
    "truncate_outcome": "TRUNCATE decision_outcomes",
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
        # decision_outcomes was added with the outcome route and NEVER GRANTED, so
        # POST /decisions/{invoice_id}/outcome raised insufficient_privilege on every
        # call on the Lambda path while the offline SQLite suite passed. The grant list
        # and the probe list below enumerated the same one table, so the gap was
        # invisible from both sides: nothing asked whether the list was complete.
        f"REVOKE ALL ON decision_outcomes FROM {APP_ROLE}",
        f"GRANT SELECT, INSERT ON decision_outcomes TO {APP_ROLE}",
    ]


def error_code(exc: BaseException) -> str | None:
    """The SQLSTATE of a pg8000 DatabaseError (its first arg is a field dict), else None."""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], dict):
        return args[0].get("C")
    return None


def _probe(conn, sql: str, **params) -> tuple[bool, str | None]:
    """Run ``sql`` in a transaction that is always rolled back. Returns (succeeded, sqlstate).

    The tenant is declared first so these probes keep measuring the GRANT rather
    than the policy. The deny probes all carry WHERE false, so no row is ever in
    scope for them and a refusal can only come from the privilege check.
    """
    conn.run("BEGIN")
    try:
        set_client(conn, PROBE_CLIENT)
        conn.run(sql, **params)
        return True, None
    except Exception as exc:  # noqa: BLE001 - classified by SQLSTATE below
        return False, error_code(exc)
    finally:
        try:
            conn.run("ROLLBACK")
        except Exception:  # noqa: BLE001 - a dead connection must not mask the verdict
            pass


def verify_grants(conn) -> dict:
    """Probe the app role's privileges. Returns check name -> passed."""
    checks = {}
    for name, sql in _MUST_SUCCEED.items():
        params = {}
        if ":id" in sql:
            params["id"] = uuid.uuid4().hex
        if ":client" in sql:
            params["client"] = PROBE_CLIENT
        ok, _ = _probe(conn, sql, **params)
        checks[f"{name}_allowed"] = ok
    for name, sql in _MUST_BE_DENIED.items():
        ok, code = _probe(conn, sql)
        checks[f"{name}_denied"] = (not ok) and code == INSUFFICIENT_PRIVILEGE
    return checks


#: One insert per tenant table for the isolation probe, client_id explicit and
#: bound. Keys are the tables row level security must cover; they are literals
#: here and are checked against the identifier pattern before interpolation,
#: because the read-back query names its table and a table name cannot be bound.
_RLS_INSERT = {
    "decision_audit": (
        "INSERT INTO decision_audit "
        "(id, invoice_id, client_id, rule_fired, decided_at, input, decision) "
        "VALUES (:id, :invoice_id, :client_id, 'probe', "
        "'1970-01-01T00:00:00.000+00:00', '{}', '{}')"
    ),
    "decision_outcomes": (
        "INSERT INTO decision_outcomes "
        "(id, invoice_id, client_id, outcome, reported_at, report_key, source) "
        "VALUES (:id, :invoice_id, :client_id, 'paid', "
        "'1970-01-01T00:00:00.000+00:00', :id, 'probe')"
    ),
}


def _count_without_a_client_predicate(conn, table: str, invoice_id: str) -> int:
    """Count rows for one invoice with NO client_id filter.

    The missing predicate is the entire point. Both stores already filter by
    client_id on every read, so a query that carries that filter cannot tell
    row level security from the application code. This one can.
    """
    if not _IDENTIFIER_RE.fullmatch(table):
        raise ValueError(f"not a plain identifier: {table!r}")
    rows = conn.run(
        f"SELECT count(*) FROM {table} WHERE invoice_id = :invoice_id",  # noqa: S608
        invoice_id=invoice_id,
    )
    return int(rows[0][0])


def _rls_probe(conn, table: str) -> dict:
    """Write a row as one client and try to read it as another, then roll back.

    THE POSITIVE CONTROL IS WHAT MAKES THE ZERO MEAN ANYTHING. A count of zero as
    the other client is indistinguishable from an insert that never landed, a
    probe id that never matched, or a table that was empty for some unrelated
    reason, so the same query runs again as the OWNING client and must return
    exactly one. Without that, a broken probe and working isolation print the
    same result and the apply goes green on a measurement of nothing.

    Any exception leaves all three checks False with the SQLSTATE logged, which
    fails the apply: a probe that could not run is not a probe that passed.
    """
    probe_invoice = "in_rls_probe_" + uuid.uuid4().hex
    own = other = unset = None
    conn.run("BEGIN")
    try:
        set_client(conn, RLS_CLIENT_A)
        conn.run(
            _RLS_INSERT[table],
            id=uuid.uuid4().hex,
            invoice_id=probe_invoice,
            client_id=RLS_CLIENT_A,
        )
        own = _count_without_a_client_predicate(conn, table, probe_invoice)
        set_client(conn, RLS_CLIENT_B)
        other = _count_without_a_client_predicate(conn, table, probe_invoice)
        # The empty string is the legacy client_id DEFAULT. nullif maps it to NULL,
        # so it must read nothing rather than reading everything that was never
        # attributed to a tenant.
        set_client(conn, "")
        unset = _count_without_a_client_predicate(conn, table, probe_invoice)
    except Exception as exc:  # noqa: BLE001 - every outcome is a failed check below
        log.info(
            json.dumps(
                {"event": "rls_probe_failed", "table": table, "sqlstate": error_code(exc)}
            )
        )
    finally:
        try:
            conn.run("ROLLBACK")
        except Exception:  # noqa: BLE001 - a dead connection must not mask the verdict
            pass
    return {
        f"{table}_rls_own_row_visible": own == 1,
        f"{table}_rls_other_client_blind": other == 0,
        f"{table}_rls_unset_client_blind": unset == 0,
    }


def verify_rls(conn) -> dict:
    """Probe tenant isolation as the app role. Returns check name -> passed."""
    checks = {}
    for table in _RLS_INSERT:
        checks.update(_rls_probe(conn, table))
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
        checks.update(verify_rls(app))
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
