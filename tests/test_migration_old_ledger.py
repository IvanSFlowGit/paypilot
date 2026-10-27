"""A ledger older than some tables migrates, and the missing tables appear empty.

The live demo ledger predates the approvals feature: it has no ``approvals`` table. The migration
used to crash on it ("no such table: approvals"), which the start script turns into a dead app.
Found by rehearsing the migration on a copy of the real file (scripts/predeploy_check.py).
"""

from __future__ import annotations

import sqlite3

from app.migrate_tenancy import migrate
from tests.test_tenancy_migration import _build_legacy


def test_a_legacy_ledger_without_an_approvals_table_migrates(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    path = tmp_path / "old.db"
    _build_legacy(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS approvals")
    conn.commit()
    conn.close()

    result = migrate(path, legacy_tenant="demo")

    assert result.status == "migrated"
    assert result.rows["approvals"] == 0
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(approvals)")}
    assert "tenant_id" in cols
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(DISTINCT tenant_id) FROM failures").fetchone()[0] == 1
    conn.close()
    assert migrate(path, legacy_tenant="demo").status == "current"


def test_the_pre_migration_backup_of_such_a_ledger_still_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    path = tmp_path / "old.db"
    _build_legacy(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS approvals")
    conn.commit()
    conn.close()
    result = migrate(path, legacy_tenant="demo")
    assert result.backup and result.backup_sha256
