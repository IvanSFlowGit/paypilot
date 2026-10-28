"""The Postgres adapter, against a fake pg8000.native connection.

This proves the TRANSLATION and the TRANSACTION DISCIPLINE (setting first, rollback on error, error
mapping, no idle-in-transaction on a read). It does not prove row-level security: that needs a real
server, and scripts/pg_rls_check.py is where it is proved.
"""

from __future__ import annotations

import sqlite3
import time

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
    drops = [p for p in parts if p.startswith("DROP FUNCTION IF EXISTS")]
    assert len(drops) == 3
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


# -- concurrency and reconnect (reviewer findings 1 and 3) ----------------------------------------


def test_a_reader_on_another_thread_cannot_commit_a_half_written_transaction():
    import threading
    import time

    native = FakeNative({"FROM failures": (["invoice_id"], [["in_1"]], 1)})
    conn = PgConnection(native, tenant_id="acme")
    started, release = threading.Event(), threading.Event()

    def writer():
        conn.execute("UPDATE failures SET state = ? WHERE tenant_id = ?", ("x", "acme"))
        started.set()
        release.wait(5)
        conn.execute("UPDATE failures SET state = ? WHERE tenant_id = ?", ("y", "acme"))
        conn.commit()

    t = threading.Thread(target=writer)
    t.start()
    started.wait(5)
    seen = []
    reader = threading.Thread(target=lambda: seen.append(conn.execute("SELECT invoice_id FROM failures")))
    reader.start()
    time.sleep(0.2)
    assert not seen, "the read must wait for the open transaction, not interleave"
    assert "COMMIT" not in native.sql()
    release.set()
    t.join(5)
    reader.join(5)
    sqls = native.sql()
    assert seen and sqls.index("COMMIT") < len(sqls) - 1  # writer committed whole, then the read ran


def test_a_lock_wait_that_never_ends_is_a_transient_error_not_a_hang():
    conn = PgConnection(FakeNative(), tenant_id="acme")
    conn.LOCK_TIMEOUT = 0.1
    import threading

    conn.execute("UPDATE failures SET state = ?", ("x",))  # transaction stays open on this thread
    out = []

    def other():
        try:
            conn.execute("SELECT 1")
        except sqlite3.OperationalError as exc:
            out.append(exc)

    t = threading.Thread(target=other)
    t.start()
    t.join(5)
    assert out and "locked" in str(out[0])


def test_a_dead_connection_is_replaced_on_the_next_call():
    natives = [FakeNative(raise_on="UPDATE failures"), FakeNative()]
    natives[0].error_state = "08006"
    made = []

    def reconnect():
        made.append(1)
        return natives[1]

    conn = PgConnection(natives[0], tenant_id="acme", reconnect=reconnect)
    with pytest.raises(Exception):  # noqa: B017, PT011
        conn.execute("UPDATE failures SET state = ?", ("x",))
    assert not conn.in_transaction
    conn.execute("UPDATE failures SET state = ?", ("y",))
    assert made == [1] and any("UPDATE" in q for q in natives[1].sql())
    conn.commit()


def test_the_schema_no_longer_ships_cross_tenant_functions():
    text = pg_backend.SCHEMA_PATH.read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
    assert "SECURITY DEFINER" not in code
    assert code.count("CREATE OR REPLACE FUNCTION") == 1 and "pp_current_tenant" in code
    assert "GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO paypilot_tenant" in text
    assert "GRANT paypilot_platform TO paypilot_owner" in text


def test_idle_connections_are_closed_and_reconnect_on_next_use():
    natives = []

    def connect(role):
        n = FakeNative()
        natives.append(n)
        return n

    db = PgDatabase(connect=connect)
    c = db.conn_for("acme")
    c.execute("SELECT 1")  # a read: no transaction left open
    assert db.reap(max_idle=999) == 0, "recently used connections stay open"
    time.sleep(0.02)
    closed = db.reap(max_idle=0.01)
    assert closed >= 1 and natives[1].closed
    c.execute("SELECT 1")  # same wrapper object, new server connection
    assert len(natives) == 3 and not natives[2].closed
    assert db.conn_for("acme") is c


def test_a_connection_inside_a_transaction_is_never_reaped():
    db = PgDatabase(connect=lambda role: FakeNative())
    c = db.conn_for("acme")
    c.execute("UPDATE failures SET state = ?", ("x",))
    time.sleep(0.02)
    db.reap(max_idle=0.01)
    assert c.in_transaction and not c._native.closed
    c.commit()


def test_a_real_deployment_starts_the_reaper_and_close_stops_it(monkeypatch):
    monkeypatch.setattr(pg_backend, "_default_connect", lambda role: FakeNative())
    db = PgDatabase()
    assert db._reaper is not None and db._reaper.is_alive()
    db.close()
    db._reaper.join(2)
    assert not db._reaper.is_alive()


# -- https-only for anything that carries a token (audit triage of urlopen findings) -----------------


def test_the_bank_client_transport_refuses_non_https():
    from app import mercury_client

    for url in ("http://api.example.test/x", "file:///etc/passwd", "ftp://h/x"):
        with pytest.raises(mercury_client.MercuryError, match="https"):
            mercury_client.default_transport("GET", url, {}, 1.0)


def test_the_consent_walk_will_not_send_a_token_over_plain_http_to_a_remote_host(monkeypatch):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import consent_walk

    for url in ("http://paypilot.fly.dev/x", "file:///etc/passwd", "//h/x"):
        with pytest.raises(SystemExit):
            consent_walk._require_https(url)
    consent_walk._require_https("https://paypilot.fly.dev/x")
    consent_walk._require_https("http://127.0.0.1:8000/x")


def test_the_token_rotation_verify_refuses_a_non_https_host():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import rotate_decision_token

    with pytest.raises(SystemExit):
        rotate_decision_token.verify("http://evil.example.test")
