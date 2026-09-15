"""Unit tests for the decision slice that the contract suite cannot reach.

The contract suite (tests/test_decision_contract.py) proves the HTTP behaviour.
These prove the properties behind it: the graph and the endpoint share one
rules table, a storage failure never returns an unrecorded decision, the two
schemas agree, and the Lambda entry point does not import the LangGraph stack.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from app import decision, nodes
from app.decision_audit import SQLITE_SCHEMA, PostgresDecisionAudit, split_sql_statements

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_graph_and_endpoint_share_one_rules_table():
    # Identity, not equality: two equal copies would still drift on next edit.
    assert nodes._STRATEGY_RULES is decision.STRATEGY_RULES
    assert nodes._DEFAULT_STRATEGY is decision.DEFAULT_STRATEGY
    assert nodes._score_churn_risk is decision.score_churn_risk


def test_graph_node_and_standalone_decision_agree():
    for code in [*decision.STRATEGY_RULES, "unknown_code"]:
        for escalate in (False, True):
            via_node = nodes.choose_strategy(
                {"event": {"failure_code": code}, "risk": {"escalate": escalate}}
            )["strategy"]
            assert via_node == decision.strategy_for(code, escalate=escalate)


class _BrokenAudit:
    def record(self, decision_payload):
        raise RuntimeError("database is down")

    def for_invoice(self, invoice_id):
        raise RuntimeError("database is down")


def test_storage_failure_never_returns_an_unrecorded_decision():
    status, body = decision.handle_decision_request(
        {"invoice_id": "in_1", "failure_code": "card_expired"}, _BrokenAudit()
    )
    assert status == 503
    assert "strategy" not in body
    assert "rule_fired" not in body


def test_lookup_storage_failure_is_503():
    status, _ = decision.handle_audit_lookup("in_1", _BrokenAudit())
    assert status == 503


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_bearer_fails_closed_when_unconfigured(configured):
    status, _ = decision.check_bearer("Bearer anything", configured)
    assert status == 503


def test_bearer_non_ascii_header_is_401_not_500():
    status, _ = decision.check_bearer("Bearer \xe9\xe9", "right-token")
    assert status == 401


def _columns(schema_sql: str) -> list[str]:
    body = re.search(r"CREATE TABLE IF NOT EXISTS decision_audit \((.*?)\);", schema_sql, re.S)
    assert body, "decision_audit table not found in schema"
    return [line.split()[0] for line in body.group(1).strip().splitlines() if line.strip()]


def test_sqlite_and_postgres_schemas_agree():
    pg_sql = (_REPO_ROOT / "app" / "decision_schema.sql").read_text(encoding="utf-8")
    sqlite_cols = _columns(SQLITE_SCHEMA)
    assert sqlite_cols == ["id", "invoice_id", "rule_fired", "decided_at", "input", "decision"]
    assert _columns(pg_sql) == sqlite_cols


def test_schema_split_yields_only_sql_statements():
    pg_sql = (_REPO_ROOT / "app" / "decision_schema.sql").read_text(encoding="utf-8")
    # Control: the real file's comments contain a semicolon, which is what made
    # the naive split send prose to Postgres. If this stops holding, the test
    # below no longer covers the case that broke.
    assert any(";" in line for line in pg_sql.splitlines() if line.lstrip().startswith("--"))
    statements = split_sql_statements(pg_sql)
    assert len(statements) == 2
    assert all(s.upper().startswith("CREATE ") for s in statements)
    assert not any("--" in s for s in statements)


def test_postgres_refuses_unverified_tls():
    audit = PostgresDecisionAudit(
        env={"PGHOST": "h", "PGUSER": "u", "PGPASSWORD": "p", "PGDATABASE": "d"}
    )
    with pytest.raises(RuntimeError, match="PGSSLROOTCERT"):
        audit._connect()


def test_lambda_handler_does_not_import_the_langgraph_stack():
    code = (
        "import sys; import app.lambda_handler; "
        "bad = sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'langchain', 'langchain_openai', 'langgraph', 'openai', 'stripe', 'fastapi'} "
        "or m in {'app.nodes', 'app.graph', 'app.api'}); "
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    )
    # Control: the same probe must see the stack when it IS imported, or an
    # empty result proves nothing about the handler.
    control = subprocess.run(
        [sys.executable, "-c", code.replace("import app.lambda_handler", "import app.graph")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert control.stdout.strip(), "probe cannot detect the LangGraph stack"
    assert out.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Bootstrap: the app role is append-only by database grant, and that is proved
# ---------------------------------------------------------------------------

from app import decision_bootstrap as boot  # noqa: E402

_HEX = "ab" * 16


class _DbError(Exception):
    """Shaped like pg8000's DatabaseError: the first arg is the field dict."""

    def __init__(self, code):
        super().__init__({"C": code, "M": "refused"})


class _FakeConn:
    """Answers each probe from a table of statement prefix -> SQLSTATE (None = succeeds)."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.log = []

    def run(self, sql, **params):
        self.log.append(sql)
        if sql in ("BEGIN", "ROLLBACK"):
            return []
        for prefix, code in self.outcomes.items():
            if sql.startswith(prefix):
                if code is not None:
                    raise _DbError(code)
                return [[0]]
        raise AssertionError(f"unexpected statement: {sql}")

    def close(self):
        pass


_APPEND_ONLY = {
    "INSERT": None,
    "SELECT": None,
    "UPDATE": "42501",
    "DELETE": "42501",
    "TRUNCATE": "42501",
    "CREATE": "42501",
}


@pytest.mark.parametrize(
    "bad", [None, "", "short", "AB" * 16, "ab" * 16 + "'; DROP ROLE x; --", "zz" * 16]
)
def test_bootstrap_refuses_a_password_that_could_leave_the_quote(bad):
    with pytest.raises(ValueError):
        boot.validate_password(bad)
    assert boot.validate_password(_HEX) == _HEX


@pytest.mark.parametrize("bad", [None, "", "paypilot; DROP", "Pay", "a b"])
def test_bootstrap_refuses_a_database_name_that_is_not_an_identifier(bad):
    with pytest.raises(ValueError):
        boot.validate_identifier(bad)


def test_bootstrap_grants_select_and_insert_and_nothing_else():
    grants = [s for s in boot.role_statements("paypilot", _HEX) if s.startswith("GRANT")]
    table_grants = [s for s in grants if " ON decision_audit " in s]
    assert table_grants == [f"GRANT SELECT, INSERT ON decision_audit TO {boot.APP_ROLE}"]
    assert not any(word in " ".join(grants) for word in ("UPDATE", "DELETE", "TRUNCATE", "ALL"))


def test_verify_grants_passes_only_the_append_only_shape():
    conn = _FakeConn(_APPEND_ONLY)
    checks = boot.verify_grants(conn)
    assert checks and all(checks.values()), checks
    # Every probe ran inside its own rolled-back transaction.
    assert conn.log.count("BEGIN") == conn.log.count("ROLLBACK") == len(checks)


@pytest.mark.parametrize("statement", ["UPDATE", "DELETE", "TRUNCATE", "CREATE"])
def test_verify_grants_fails_when_a_forbidden_statement_is_allowed(statement):
    checks = boot.verify_grants(_FakeConn({**_APPEND_ONLY, statement: None}))
    assert checks[f"{statement.lower()}_denied"] is False


def test_verify_grants_does_not_count_a_different_error_as_denied():
    # A missing table (42P01) also makes DELETE fail. That is not a privilege
    # refusal, and counting it as one would pass a role that was never tested.
    checks = boot.verify_grants(_FakeConn({**_APPEND_ONLY, "DELETE": "42P01"}))
    assert checks["delete_denied"] is False


def test_verify_grants_fails_when_insert_is_refused():
    checks = boot.verify_grants(_FakeConn({**_APPEND_ONLY, "INSERT": "42501"}))
    assert checks["insert_allowed"] is False


def test_bootstrap_run_raises_when_grants_are_wrong(monkeypatch):
    master = _FakeConn({"CREATE": None, "DO ": None, "ALTER": None, "REVOKE": None, "GRANT": None})
    app = _FakeConn({**_APPEND_ONLY, "DELETE": None})
    conns = iter([master, app])
    monkeypatch.setattr(boot, "open_connection", lambda env: next(conns))
    env = {"PGDATABASE": "paypilot", boot.APP_PASSWORD_ENV: _HEX, "PGUSER": "master"}
    with pytest.raises(RuntimeError, match="delete_denied"):
        boot.run(env)


def test_bootstrap_run_returns_no_secret(monkeypatch):
    master = _FakeConn({"CREATE": None, "DO ": None, "ALTER": None, "REVOKE": None, "GRANT": None})
    conns = iter([master, _FakeConn(_APPEND_ONLY)])
    monkeypatch.setattr(boot, "open_connection", lambda env: next(conns))
    env = {"PGDATABASE": "paypilot", boot.APP_PASSWORD_ENV: _HEX, "PGUSER": "master"}
    result = boot.run(env)
    assert result["ok"] is True
    assert _HEX not in repr(result)
