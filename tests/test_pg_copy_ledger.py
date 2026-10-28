"""M8 copy tool against a fake Postgres connection: refusals, ordering, and the content hash."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pg_copy_ledger as m8  # noqa: E402

from app.store import Database  # noqa: E402


class FakeNative:
    def __init__(self, existing=None):
        self.existing = existing or {}
        self.calls = []
        self.store: dict[str, list] = {}

    def run(self, sql, **kw):
        self.calls.append((sql, kw))
        if sql.startswith("SELECT count(*)"):
            table = sql.split("FROM ")[1].strip()
            return [[self.existing.get(table, 0)]]
        if sql.startswith("INSERT INTO"):
            table = sql.split()[2]
            self.store.setdefault(table, []).append(tuple(kw[f"c{i}"] for i in range(len(kw))))
        if sql.startswith("SELECT ") and " ORDER BY " in sql:
            table = sql.split(" FROM ")[1].split()[0]
            return [list(r) for r in self.store.get(table, [])]
        return None


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    path = tmp_path / "l.db"
    db = Database(path, owner_tenant="default")
    db.conn.execute("INSERT INTO failures (tenant_id, invoice_id, customer_id, amount_minor, currency, "
                    "failure_code, state, failed_at, updated_at) VALUES "
                    "('t','in_1','c',500,'eur','x','failed','n','n')")
    db.conn.execute("INSERT INTO messages (tenant_id, invoice_id, channel, status, created_at) "
                    "VALUES ('t','in_1','email','sent','n')")
    db.conn.commit()
    db.close()
    return path


def test_a_source_without_the_tenancy_schema_is_refused(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE failures (invoice_id TEXT)")
    with pytest.raises(m8.CopyRefused):
        m8.check_source(c)


def test_a_non_empty_target_is_refused_not_merged():
    with pytest.raises(m8.CopyRefused, match="failures"):
        m8.check_target_empty(FakeNative(existing={"failures": 3}))


def test_copy_preserves_ids_and_resets_sequences(ledger):
    conn = sqlite3.connect(ledger)
    native = FakeNative()
    counts = m8.copy(conn, native)
    assert counts["failures"] == 1 and counts["messages"] == 1
    assert native.calls[0][0] == "START TRANSACTION" and native.calls[-1][0] == "COMMIT"
    inserted = [c for c in native.calls if c[0].startswith("INSERT INTO messages")]
    assert "id," in inserted[0][0].split("(")[1]  # the id column is copied, not regenerated
    assert sum("setval" in c[0] for c in native.calls) == 3


def test_verify_passes_on_identical_content_and_fails_on_a_changed_value(ledger):
    conn = sqlite3.connect(ledger)
    native = FakeNative()
    m8.copy(conn, native)
    assert m8.verify(conn, native) == []
    row = list(native.store["failures"][0])
    row[3] = 501  # same row count, one value different
    native.store["failures"][0] = tuple(row)
    problems = m8.verify(conn, native)
    assert problems == ["failures: same row count (1) but different content"]


def test_verify_reports_a_missing_row(ledger):
    conn = sqlite3.connect(ledger)
    native = FakeNative()
    m8.copy(conn, native)
    native.store["messages"].clear()
    assert any("messages: 1 rows in SQLite, 0 in Postgres" in p for p in m8.verify(conn, native))


def test_a_failure_part_way_rolls_back(ledger):
    class Boom(FakeNative):
        def run(self, sql, **kw):
            if sql.startswith("INSERT INTO messages"):
                raise RuntimeError("boom")
            return super().run(sql, **kw)

    native = Boom()
    with pytest.raises(RuntimeError):
        m8.copy(sqlite3.connect(ledger), native)
    assert native.calls[-1][0] == "ROLLBACK"


def test_dry_run_writes_nothing_and_exits_zero(ledger, capsys):
    assert m8.main([str(ledger)]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "failures" in out


def test_the_column_lists_match_the_schema_files():
    import re

    from app.pg_backend import SCHEMA_PATH
    from app.store import _TABLE_DDL

    pg = SCHEMA_PATH.read_text()
    for table, (cols, _o) in m8.TABLES.items():
        body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", pg, re.S).group(1)
        pg_cols = [ln.split()[0] for ln in body.splitlines()
                   if ln.strip() and not ln.strip().startswith(("--", "PRIMARY"))]
        assert list(cols) == pg_cols, table
        sq = _TABLE_DDL[table]
        assert all(c in sq for c in cols), table
