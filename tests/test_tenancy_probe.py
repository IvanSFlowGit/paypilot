"""The migration reports "current" only for a real tenant-scoped ledger. Any other
existing file is refused (exit 1) with a message that says what it is."""

from __future__ import annotations

import sqlite3

import pytest

from app import migrate_tenancy


@pytest.mark.parametrize("kind", ["zero_bytes", "not_sqlite", "sqlite_without_ledger"])
def test_an_existing_file_that_is_not_a_ledger_is_refused(tmp_path, monkeypatch, capsys, kind):
    path = tmp_path / "suspect.db"
    if kind == "zero_bytes":
        path.write_bytes(b"")
    elif kind == "not_sqlite":
        path.write_bytes(b"this is a text file, not a database\n" * 200)
    else:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    monkeypatch.delenv("PAYPILOT_LEGACY_TENANT", raising=False)
    before = path.read_bytes()

    assert migrate_tenancy.main([]) == migrate_tenancy.EXIT_REFUSED
    out = capsys.readouterr().out
    assert out.startswith("REFUSED:"), out
    assert "already current" not in out, "a non-ledger must never be reported as current"
    assert path.read_bytes() == before, "the file is left as it was"
