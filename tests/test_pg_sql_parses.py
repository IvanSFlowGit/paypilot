"""Every SQL statement the Postgres schema and the roles module can emit parses with Postgres's own
parser (pglast, which wraps libpg_query).

Why this exists: a function that does not exist, `pg_catalog.nullif`, was written into the schema, every
offline test passed, and the first real server refused it. A parser cannot catch a missing function, but it
catches malformed SQL, which is the larger class, before a person has to run a cluster to find out.
Skipped where pglast is not installed (it is installed in CI).
"""

from __future__ import annotations

import re

import pytest

from app import pg_backend, pg_roles

pglast = pytest.importorskip("pglast")

SCHEMA = pg_backend.SCHEMA_PATH.read_text(encoding="utf-8")


def _parses(sql: str) -> None:
    try:
        pglast.parse_sql(sql)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Postgres cannot parse this: {' '.join(sql.split())[:160]!r}: {exc}")


def test_every_schema_statement_parses():
    statements = pg_backend.split_statements(SCHEMA)
    assert len(statements) > 25
    for st in statements:
        _parses(st)


def test_the_tenant_function_body_parses_and_calls_only_real_pg_catalog_functions():
    body = re.search(r"\$f\$(.*)\$f\$", SCHEMA, re.S).group(1)
    _parses(body)
    called = set(re.findall(r"pg_catalog\.([a-z_]+)\(", body))
    # NULLIF and COALESCE are syntax, not functions: qualifying them is what failed on the first real run.
    assert called <= {"left", "substr", "current_setting"}, called


def test_the_statements_the_do_block_generates_parse():
    block = next(s for s in pg_backend.split_statements(SCHEMA) if s.startswith("DO $$"))
    templates = re.findall(r"'((?:ALTER TABLE|DROP POLICY|CREATE POLICY)[^']*(?:''[^']*)*)'", block)
    assert len(templates) >= 4, templates
    for t in templates:
        sql = t.replace("''", "'").replace("%I", "failures")
        _parses(sql)


def test_every_role_statement_parses():
    verifier = pg_roles.scram_verifier("x" * 40, b"0123456789abcdef")
    for exists in (False, True):
        for st in pg_roles.provision_sql("alpha", verifier, exists):
            _parses(st)
    for st in pg_roles.deprovision_sql("alpha"):
        _parses(st.replace(":n", "'pp_t_alpha'"))
    _parses(pg_roles._PUBLIC_CREATE_SQL)
    _parses("SELECT 1 WHERE has_schema_privilege('pp_t_alpha', 'public', 'CREATE')")
