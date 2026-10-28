"""The Postgres adapter, against a fake pg8000.native connection.

This proves the TRANSLATION and the TRANSACTION DISCIPLINE (setting first, rollback on error, error
mapping, no idle-in-transaction on a read). It does not prove row-level security: that needs a real
server, and scripts/pg_rls_check.py is where it is proved.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import pg_backend
from app.pg_backend import PgConnection, PgDatabase, split_statements, translate


class FakeNative:
    def __init__(self, results=None, raise_on=None):
        self.calls: list[tuple[str, dict]] = []
        self.results = results or {}
        self.raise_on = raise_on
        self.columns: list[dict] = []
        self.row_count = -1
        self.closed = False

    def run(self, sql, **kw):
        self.calls.append((sql, kw))
        if self.raise_on and self.raise_on in sql:
            raise Exception({"C": self.error_state, "M": "boom"})  # noqa: TRY002
        for needle, (cols, rows, count) in self.results.items():
            if needle in sql:
                self.columns = [{"name": c} for c in cols]
                self.row_count = count
                return rows
        self.columns = []
        self.row_count = 0
        return None

    error_state = "23505"

    def close(self):
        self.closed = True

    def sql(self):
        return [c[0] for c in self.calls]


# -- translate ----------------------------------------------------------------------------------


def test_question_marks_become_named_parameters_in_order():
    sql, vals = translate("SELECT * FROM failures WHERE tenant_id = ? AND invoice_id = ?", ("t", "i"))
    assert sql == "SELECT * FROM failures WHERE tenant_id = :p0 AND invoice_id = :p1"
    assert vals == {"p0": "t", "p1": "i"}


def test_a_question_mark_inside_a_literal_is_data_not_a_placeholder():
    sql, vals = translate("SELECT 'a?b' WHERE x = ?", ("v",))
    assert sql == "SELECT 'a?b' WHERE x = :p0" and vals == {"p0": "v"}


def test_an_escaped_quote_does_not_end_the_literal():
    sql, _ = translate("SELECT 'it''s ?' WHERE x = ?", ("v",))
    assert sql == "SELECT 'it''s ?' WHERE x = :p0"


@pytest.mark.parametrize("params", [(), ("a", "b")])
def test_a_placeholder_count_mismatch_is_refused(params):
    with pytest.raises(ValueError):
        translate("SELECT ?", params)


def test_insert_or_ignore_becomes_on_conflict_do_nothing():
    sql, _ = translate("INSERT OR IGNORE INTO early_paid (tenant_id, invoice_id) VALUES (?, ?)", ("t", "i"))
    assert sql.startswith("INSERT INTO early_paid")
    assert sql.endswith("ON CONFLICT DO NOTHING")
    assert "OR IGNORE" not in sql


@pytest.mark.parametrize("table", ["messages", "transitions", "approvals"])
def test_inserts_whose_id_is_read_gain_returning_id(table):
    sql, _ = translate(f"INSERT INTO {table} (tenant_id) VALUES (?)", ("t",))
    assert sql.endswith("RETURNING id")


def test_inserts_into_other_tables_do_not_gain_returning():
    sql, _ = translate("INSERT INTO events (tenant_id) VALUES (?)", ("t",))
    assert "RETURNING" not in sql


def test_a_trailing_semicolon_is_dropped_before_suffixes_are_added():
    sql, _ = translate("INSERT INTO messages (tenant_id) VALUES (?);", ("t",))
    assert sql.endswith("RETURNING id") and ";" not in sql


# -- the schema splitter ------------------------------------------------------------------------


def test_the_schema_splits_with_the_do_block_and_functions_whole():
    parts = split_statements(pg_backend.SCHEMA_PATH.read_text(encoding="utf-8"))
    do_blocks = [p for p in parts if p.startswith("DO $$")]
    assert len(do_blocks) == 1 and do_blocks[0].rstrip().endswith("END $$")
    assert "FORCE ROW LEVEL SECURITY" in do_blocks[0]
    funcs = [p for p in parts if p.startswith("CREATE OR REPLACE FUNCTION")]
    assert len(funcs) == 3 and all("SECURITY DEFINER" in f for f in funcs)
    assert all(p and not p.startswith("--") for p in parts)


def test_a_semicolon_inside_a_literal_or_comment_does_not_split():
    assert split_statements("SELECT 'a;b'; -- c; d\nSELECT 2;") == ["SELECT 'a;b'", "SELECT 2"]


# -- transactions and the tenant setting --------------------------------------------------------


def test_a_tenant_connection_sets_the_tenant_first_in_every_transaction():
    native = FakeNative()
    conn = PgConnection(native, tenant_id="acme")
    conn.execute("UPDATE failures SET state = ? WHERE tenant_id = ?", ("x", "acme"))
    conn.commit()
    conn.execute("UPDATE failures SET state = ? WHERE tenant_id = ?", ("y", "acme"))
    conn.commit()
    sqls = native.sql()
    assert sqls[:2] == ["START TRANSACTION", "SELECT set_config('paypilot.tenant_id', :t, true)"]
    assert native.calls[1][1] == {"t": "acme"}
    # the second transaction sets it again: transaction-local means it does not carry over
    assert sqls.count("SELECT set_config('paypilot.tenant_id', :t, true)") == 2
    assert sqls.count("START TRANSACTION") == 2 and sqls.count("COMMIT") == 2


def test_the_setting_is_always_transaction_local():
    native = FakeNative()
    PgConnection(native, tenant_id="acme").execute("DELETE FROM failures WHERE tenant_id = ?", ("acme",))
    set_calls = [c for c in native.calls if "set_config" in c[0]]
    assert set_calls and all(c[0].rstrip().endswith(", true)") for c in set_calls)


def test_the_platform_connection_sets_no_tenant():
    native = FakeNative()
    PgConnection(native, tenant_id=None).execute("SELECT 1 FROM tenants")
    assert not any("set_config" in s for s in native.sql())


def test_a_read_does_not_leave_a_transaction_open():
    native = FakeNative({"FROM failures": (["invoice_id"], [["in_1"]], 1)})
    conn = PgConnection(native, tenant_id="acme")
    row = conn.execute("SELECT invoice_id FROM failures WHERE tenant_id = ?", ("acme",)).fetchone()
    assert row["invoice_id"] == "in_1" and row[0] == "in_1"
    assert native.sql()[-1] == "COMMIT" and not conn.in_transaction


def test_a_write_stays_open_until_commit_and_a_read_after_it_does_not_commit_early():
    native = FakeNative({"FROM failures": (["invoice_id"], [["in_1"]], 1)})
    conn = PgConnection(native, tenant_id="acme")
    conn.execute("UPDATE failures SET state = ? WHERE tenant_id = ?", ("x", "acme"))
    conn.execute("SELECT invoice_id FROM failures WHERE tenant_id = ?", ("acme",))
    assert conn.in_transaction and "COMMIT" not in native.sql()
    conn.commit()
    assert not conn.in_transaction


def test_lastrowid_comes_from_returning():
    native = FakeNative({"INSERT INTO messages": (["id"], [[42]], 1)})
    conn = PgConnection(native, tenant_id="acme")
    cur = conn.execute("INSERT INTO messages (tenant_id) VALUES (?)", ("acme",))
    assert cur.lastrowid == 42
    assert "RETURNING id" in native.calls[2][0]


def test_rowcount_is_passed_through_for_ignore_inserts():
    native = FakeNative({"INSERT INTO early_paid": ([], None, 0)})
    cur = PgConnection(native, tenant_id="acme").execute(
        "INSERT OR IGNORE INTO early_paid (tenant_id, invoice_id, seen_at) VALUES (?, ?, ?)",
        ("acme", "i", "now"),
    )
    assert cur.rowcount == 0


def test_a_unique_violation_is_an_integrity_error_and_rolls_back():
    native = FakeNative(raise_on="INSERT INTO approvals")
    conn = PgConnection(native, tenant_id="acme")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO approvals (tenant_id) VALUES (?)", ("acme",))
    assert "ROLLBACK" in native.sql() and not conn.in_transaction


@pytest.mark.parametrize("state", ["40001", "40P01", "55P03"])
def test_a_serialisation_or_lock_failure_is_the_transient_error_callers_retry(state):
    native = FakeNative(raise_on="UPDATE failures")
    native.error_state = state
    conn = PgConnection(native, tenant_id="acme")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE failures SET state = ?", ("x",))


def test_any_other_server_error_is_not_swallowed_or_renamed():
    native = FakeNative(raise_on="UPDATE failures")
    native.error_state = "42P01"
    with pytest.raises(Exception) as ei:  # noqa: PT011
        PgConnection(native, tenant_id="acme").execute("UPDATE failures SET state = ?", ("x",))
    assert not isinstance(ei.value, (sqlite3.IntegrityError, sqlite3.OperationalError))


def test_begin_immediate_takes_an_advisory_lock_not_a_noop():
    native = FakeNative()
    conn = PgConnection(native, tenant_id=None)
    conn.execute("BEGIN IMMEDIATE")
    assert any("pg_advisory_xact_lock" in s for s in native.sql())
    assert conn.in_transaction
    conn.rollback()
    assert not conn.in_transaction


# -- the database object ------------------------------------------------------------------------


def test_each_tenant_gets_its_own_connection_as_the_tenant_role():
    opened: list[tuple[str, FakeNative]] = []

    def connect(role):
        native = FakeNative()
        opened.append((role, native))
        return native

    db = PgDatabase(connect=connect)
    a1 = db.conn_for("a")
    assert db.conn_for("a") is a1
    b = db.conn_for("b")
    assert b is not a1 and db.lock_for("a") is not db.lock_for("b")
    assert [r for r, _ in opened] == ["platform", "tenant", "tenant"]
    assert db.conn.tenant_id is None and a1.tenant_id == "a" and b.tenant_id == "b"
    db.close()
    assert all(n.closed for _, n in opened)


def test_a_store_on_the_postgres_database_uses_its_tenant_connection(monkeypatch):
    from app.store import Store

    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    native_by_role: dict[str, list[FakeNative]] = {"platform": [], "tenant": []}

    def connect(role):
        n = FakeNative({"tenancy_meta": (["value"], [], 0)})
        native_by_role[role].append(n)
        return n

    db = PgDatabase(connect=connect)
    store = Store(db, tenant_id="acme")
    assert store._conn is db.conn_for("acme") and store._conn is not db.conn
    store.mark_early_paid("in_1")
    tenant_native = native_by_role["tenant"][0]
    assert any("set_config" in s for s in tenant_native.sql())
    assert any("ON CONFLICT DO NOTHING" in s for s in tenant_native.sql())


def test_the_backend_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("PAYPILOT_DB_BACKEND", raising=False)
    assert not pg_backend.backend_is_postgres()
    monkeypatch.setenv("PAYPILOT_DB_BACKEND", "Postgres")
    assert pg_backend.backend_is_postgres()


def test_postgres_backend_refuses_single_tenancy(monkeypatch):
    from app import store as store_mod

    monkeypatch.setenv("PAYPILOT_DB_BACKEND", "postgres")
    monkeypatch.setenv("PAYPILOT_TENANCY", "single")
    store_mod.reset_store(None)  # no registered store, so get_database() chooses the backend itself
    store_mod.reset_database()
    try:
        with pytest.raises(RuntimeError, match="multi"):
            store_mod.get_database()
    finally:
        store_mod.reset_database()
