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

    def for_invoice(self, invoice_id, client_id):
        raise RuntimeError("database is down")


def test_storage_failure_never_returns_an_unrecorded_decision():
    status, body = decision.handle_decision_request(
        {"invoice_id": "in_1", "failure_code": "card_expired"}, _BrokenAudit(), "acme"
    )
    assert status == 503
    assert "strategy" not in body
    assert "rule_fired" not in body


def test_lookup_storage_failure_is_503():
    status, _ = decision.handle_audit_lookup("in_1", _BrokenAudit(), "acme")
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
    assert sqlite_cols == ["id", "invoice_id", "client_id", "rule_fired", "decided_at",
                           "input", "decision"]
    assert _columns(pg_sql) == sqlite_cols


def test_schema_split_yields_only_sql_statements():
    pg_sql = (_REPO_ROOT / "app" / "decision_schema.sql").read_text(encoding="utf-8")
    # Control: the real file's comments contain a semicolon, which is what made
    # the naive split send prose to Postgres. If this stops holding, the test
    # below no longer covers the case that broke.
    assert any(";" in line for line in pg_sql.splitlines() if line.lstrip().startswith("--"))
    statements = split_sql_statements(pg_sql)
    # The count is DERIVED from the file rather than typed, because a literal here
    # goes red on every legitimate schema addition and the author then has two
    # options, edit nothing or step around the test. What must hold is one
    # statement out per top-level statement keyword in, with no prose between.
    expected = sum(
        1 for line in pg_sql.splitlines() if line[:1].isupper() and not line.startswith("--")
    )
    assert expected > 1, "control: the file must hold several statements to split"
    assert len(statements) == expected
    assert all(s.upper().startswith(("CREATE ", "ALTER ")) for s in statements)
    # At least one ALTER: CREATE TABLE IF NOT EXISTS will not add a column to a
    # database that already exists, so a migration path has to be in the file.
    assert sum(s.upper().startswith("ALTER ") for s in statements) >= 1
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


def test_lambda_responses_carry_the_security_headers():
    from app import lambda_handler

    event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/health"}
    headers = lambda_handler.handler(event, None)["headers"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store"
    # A refusal carries them too, not only a success.
    refused = lambda_handler.handler(
        {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/decide", "headers": {}}, None
    )
    assert refused["statusCode"] in (401, 503)
    assert refused["headers"]["x-content-type-options"] == "nosniff"


# --- the outcome loop ------------------------------------------------------
#
# These exist because the decision API recorded what it DECIDED and never
# whether it worked, so the recovery rate was measurable on the demo loop and
# unmeasurable on the path clients integrate against.


def _seeded_audit(tmp_path, invoice="inv_1", client="c1"):
    from app.decision import handle_decision_request
    from app.decision_audit import SqliteDecisionAudit

    audit = SqliteDecisionAudit(str(tmp_path / "audit.db"))
    status, _ = handle_decision_request(
        {"invoice_id": invoice, "failure_code": "card_expired"}, audit, client
    )
    assert status == 200, "control: the decision must be recorded before an outcome can attach"
    return audit


def test_outcome_is_unknown_before_anybody_reports(tmp_path):
    audit = _seeded_audit(tmp_path)
    # Three-valued on purpose: nobody has told us is not the same fact as not
    # paid, and collapsing them is how a recovery rate becomes fiction.
    assert audit.outcome_for("inv_1", "c1") == {
        "outcome": "unknown",
        "reported_at": None,
        "source": None,
    }
    assert audit.outcome_counts("c1") == {
        "decided": 1,
        "paid": 0,
        "not_paid": 0,
        "unknown": 1,
    }


def test_outcome_report_is_idempotent_without_a_caller_key(tmp_path):
    from app.decision import handle_outcome_report

    audit = _seeded_audit(tmp_path)
    first = handle_outcome_report("inv_1", {"outcome": "paid"}, audit, "c1")
    repeat = handle_outcome_report("inv_1", {"outcome": "paid"}, audit, "c1")
    assert first[0] == repeat[0] == 200
    assert first[1]["stored"] is True
    # The default key is derived from the invoice and the outcome, so a client
    # retrying a POST whose response it never saw does not inflate the count.
    assert repeat[1]["stored"] is False
    assert repeat[1]["outcome_id"] == first[1]["outcome_id"]
    assert audit.outcome_counts("c1")["paid"] == 1


def test_a_caller_key_allows_a_deliberate_second_report_and_the_latest_wins(tmp_path):
    from app.decision import handle_outcome_report

    audit = _seeded_audit(tmp_path)
    handle_outcome_report("inv_1", {"outcome": "not_paid", "report_id": "r1"}, audit, "c1")
    second = handle_outcome_report("inv_1", {"outcome": "paid", "report_id": "r2"}, audit, "c1")
    assert second[1]["stored"] is True
    assert audit.outcome_for("inv_1", "c1")["outcome"] == "paid"
    # Counted once, as its latest outcome, never once per report.
    assert audit.outcome_counts("c1") == {
        "decided": 1,
        "paid": 1,
        "not_paid": 0,
        "unknown": 0,
    }


def test_outcome_is_scoped_to_the_client_that_holds_the_decision(tmp_path):
    from app.decision import handle_outcome_report

    audit = _seeded_audit(tmp_path)
    # 404 rather than 403: the client id is inside the predicate, so probing
    # another tenant's invoice ids returns what an id nobody has ever seen does.
    status, body = handle_outcome_report("inv_1", {"outcome": "paid"}, audit, "c2")
    assert status == 404
    assert body["error"] == "not_found"
    assert audit.outcome_for("inv_1", "c2")["outcome"] == "unknown"
    assert audit.outcome_counts("c2")["decided"] == 0


def test_outcome_report_refuses_bad_input_and_fails_closed_on_storage(tmp_path):
    from app.decision import handle_outcome_report

    audit = _seeded_audit(tmp_path)
    # unknown is a READ value and never a reportable one: it means nobody told
    # us, so a caller asserting it is asserting an absence.
    assert handle_outcome_report("inv_1", {"outcome": "unknown"}, audit, "c1")[0] == 422
    assert handle_outcome_report("inv_1", {"outcome": "paid", "nope": 1}, audit, "c1")[0] == 422
    assert handle_outcome_report("inv_1", {}, audit, "c1")[0] == 422
    assert handle_outcome_report("inv!1", {"outcome": "paid"}, audit, "c1")[0] == 422
    assert handle_outcome_report("inv_zzz", {"outcome": "paid"}, audit, "c1")[0] == 404

    class _Down:
        def has_decision(self, *a, **k):
            raise RuntimeError("audit store unreachable")

    status, body = handle_outcome_report("inv_1", {"outcome": "paid"}, _Down(), "c1")
    assert status == 503
    assert body["error"] == "audit_unavailable"


def test_outcome_store_has_no_update_or_delete():
    # The docstring on decision_audit promises an append-only trail, which is a
    # GUARANTEE rather than an observation, so the outcome sits beside the
    # decision instead of mutating it. Assert the absence that makes that true.
    from app.decision_audit import PostgresDecisionAudit, SqliteDecisionAudit

    for cls in (SqliteDecisionAudit, PostgresDecisionAudit):
        names = [n for n in dir(cls) if not n.startswith("__")]
        assert not any("update" in n or "delete" in n for n in names), names
    sql = (_REPO_ROOT / "app" / "decision_schema.sql").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert "UPDATE " not in body.upper()
    assert "DELETE " not in body.upper()


def test_outcome_route_is_in_the_published_schema():
    # A route hidden from the OpenAPI document does not exist commercially.
    from app.api import app

    paths = app.openapi()["paths"]
    assert "/decisions/{invoice_id}/outcome" in paths
    assert "post" in paths["/decisions/{invoice_id}/outcome"]


# --- the decision-API report arm -------------------------------------------


def _reported(tmp_path, paid, not_paid, undecided=0):
    """Seed N decisions and report outcomes on some of them."""
    from app.decision import handle_decision_request, handle_outcome_report
    from app.decision_audit import SqliteDecisionAudit

    audit = SqliteDecisionAudit(str(tmp_path / "r.db"))
    n = 0
    for outcome, how_many in (("paid", paid), ("not_paid", not_paid), (None, undecided)):
        for _ in range(how_many):
            n += 1
            inv = f"inv_{n}"
            handle_decision_request({"invoice_id": inv, "failure_code": "card_expired"}, audit, "c1")
            if outcome is not None:
                handle_outcome_report(inv, {"outcome": outcome}, audit, "c1")
    return audit


def test_decision_report_counts_unknown_as_its_own_number(tmp_path):
    from app.report import build_decision_report

    audit = _reported(tmp_path, paid=2, not_paid=1, undecided=7)
    r = build_decision_report("c1", audit)
    assert r["decided"] == 10
    assert (r["paid"], r["not_paid"], r["unknown"]) == (2, 1, 7)
    # The three reported buckets plus unknown must account for every decision,
    # or some invoice has been dropped or double counted.
    assert r["paid"] + r["not_paid"] + r["unknown"] == r["decided"]
    assert r["reported"] == 3


def test_decision_report_withholds_a_ratio_below_the_floor(tmp_path):
    from app.report import MIN_REPORTED_OUTCOMES, build_decision_report

    audit = _reported(tmp_path, paid=2, not_paid=1)
    r = build_decision_report("c1", audit)
    # A denominator of three exaggerates wildly while reading as precision.
    assert r["rates_withheld"] is True
    assert r["reported_rate"] is None and r["floor_rate"] is None
    assert str(MIN_REPORTED_OUTCOMES) in r["note"]
    assert "3 of 3" in r["note"]


def test_decision_report_floor_rate_is_lower_than_the_reported_rate(tmp_path):
    from app.report import build_decision_report

    # 15 paid, 5 not paid, 20 never reported: enough reported to print a ratio.
    audit = _reported(tmp_path, paid=15, not_paid=5, undecided=20)
    r = build_decision_report("c1", audit)
    assert r["rates_withheld"] is False
    assert r["reported_rate"] == 0.75  # 15 of 20 reported
    assert r["floor_rate"] == 0.375  # 15 of 40 decided
    # The floor treats silence as failure, so it can never exceed the measured
    # rate. If it ever does, one of the two denominators is wrong.
    assert r["floor_rate"] <= r["reported_rate"]
    assert "floor_rate counts every unreported decision as unrecovered" in r["note"]


def test_decision_report_is_scoped_to_the_calling_client(tmp_path):
    from app.report import build_decision_report

    audit = _reported(tmp_path, paid=3, not_paid=0)
    other = build_decision_report("c2", audit)
    assert (other["decided"], other["paid"], other["unknown"]) == (0, 0, 0)


def test_outcome_summary_route_is_published_and_not_shadowed():
    from app.api import app

    paths = app.openapi()["paths"]
    assert "get" in paths["/decisions/outcomes/summary"]
    # The literal path must not be swallowed by /decisions/{invoice_id}/outcome,
    # which has the same segment count. Different methods, so assert that.
    assert "get" not in paths["/decisions/{invoice_id}/outcome"]
    assert "post" not in paths["/decisions/outcomes/summary"]
