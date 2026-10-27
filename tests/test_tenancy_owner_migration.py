"""Owner handling in the migration (rework cycle 3).

* Every file records an owner. A current file without one is backfilled only when
  every row already belongs to the configured tenant; otherwise it is refused.
* An owner that differs from the configured tenant is refused.
* With no legacy rows the owner is the configured tenant, and a contradicting
  PAYPILOT_LEGACY_TENANT is refused.
* A lock that cannot be taken is "could not run" (exit 2), including in the probe.
* The owner row is written inside the transaction that moves the rows.
* An unverified backup leaves no .bak and no .partial, also on an interrupt.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from app import migrate_tenancy
from app.store import Database, LegacySchemaError

ROOT = Path(__file__).resolve().parent.parent


def _legacy_schema() -> str:
    spec = importlib.util.spec_from_file_location(
        "tenancy_migration_fixture", ROOT / "tests" / "test_tenancy_migration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LEGACY_SCHEMA


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_file(path: Path, *, with_failure: bool) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_legacy_schema())
    if with_failure:
        conn.execute(
            "INSERT INTO failures (invoice_id, customer_id, amount_minor, currency, "
            "failure_code, state, failed_at, updated_at) VALUES ('in_1', 'cus_1', 100, 'eur', "
            "'x', 'failed', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
        )
    conn.commit()
    conn.close()


def _current_file(path: Path, owner: str | None, tenant_of_rows: str | None = None) -> None:
    """A current-schema file, optionally with its owner row removed or rows added."""
    db = Database(path)
    if tenant_of_rows is not None:
        db.conn.execute(
            "INSERT INTO failures (tenant_id, invoice_id, customer_id, amount_minor, currency, "
            "failure_code, state, failed_at, updated_at) VALUES (?, 'in_1', 'cus_1', 100, 'eur', "
            "'x', 'failed', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')",
            (tenant_of_rows,),
        )
    db.conn.execute("DELETE FROM tenancy_meta")
    if owner is not None:
        db.conn.execute(
            "INSERT INTO tenancy_meta (key, value) VALUES ('owner_tenant', ?)", (owner,)
        )
    db.conn.commit()
    db.close()


def _owner(path: Path) -> str | None:
    db = Database(path)
    try:
        return db.owner_tenant()
    finally:
        db.close()


# --- no legacy rows: the owner is the configured tenant --------------------------


def test_an_empty_legacy_file_is_owned_by_the_configured_tenant(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    _legacy_file(path, with_failure=False)
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    assert migrate_tenancy.migrate(path, legacy_tenant=None).status == "migrated"
    assert _owner(path) == "demo"


def test_an_empty_legacy_file_refuses_a_contradicting_legacy_tenant(tmp_path, monkeypatch):
    path = tmp_path / "empty-contradiction.db"
    _legacy_file(path, with_failure=False)
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    before = _sha(path)
    with pytest.raises(migrate_tenancy.MigrationRefused, match="differs from PAYPILOT_TENANT"):
        migrate_tenancy.migrate(path, legacy_tenant="default")
    assert _sha(path) == before


# --- current files: owner checks under the lock ---------------------------------


def test_a_current_file_owned_by_another_tenant_is_refused(tmp_path, monkeypatch):
    """The reviewer's case: a file owned by demo, reported as current for default."""
    path = tmp_path / "owned.db"
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    _current_file(path, owner="demo")
    monkeypatch.setenv("PAYPILOT_TENANT", "default")
    before = _sha(path)
    with pytest.raises(migrate_tenancy.MigrationRefused, match="owned by tenant 'demo'"):
        migrate_tenancy.migrate(path, legacy_tenant=None)
    assert _sha(path) == before


def test_a_current_file_without_an_owner_is_backfilled_when_its_rows_match(tmp_path, monkeypatch):
    path = tmp_path / "backfill.db"
    monkeypatch.setenv("PAYPILOT_TENANT", "default")
    _current_file(path, owner=None, tenant_of_rows="default")
    assert migrate_tenancy.migrate(path, legacy_tenant=None).status == "current"
    assert _owner(path) == "default"


def test_a_current_file_without_an_owner_and_foreign_rows_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "foreign.db"
    monkeypatch.setenv("PAYPILOT_TENANT", "default")
    _current_file(path, owner=None, tenant_of_rows="tn_bravo")
    before = _sha(path)
    with pytest.raises(migrate_tenancy.MigrationRefused, match="tn_bravo"):
        migrate_tenancy.migrate(path, legacy_tenant=None)
    assert _sha(path) == before
    assert _owner(path) is None, "nothing was backfilled"


def test_the_command_reports_a_foreign_owner_as_refused_not_current(tmp_path, monkeypatch, capsys):
    path = tmp_path / "command.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    _current_file(path, owner="demo")
    monkeypatch.setenv("PAYPILOT_TENANT", "default")
    assert migrate_tenancy.main([]) == migrate_tenancy.EXIT_REFUSED
    out = capsys.readouterr().out
    assert out.startswith("REFUSED:") and "already current" not in out


# --- lock handling in the probe --------------------------------------------------


def test_a_held_lock_during_the_probe_is_could_not_run_not_refused(tmp_path, monkeypatch, capsys):
    path = tmp_path / "held.db"
    _legacy_file(path, with_failure=True)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        real = migrate_tenancy.migrate

        def quick(p, **kwargs):
            return real(p, lock_timeout=0.2, **kwargs)

        monkeypatch.setattr(migrate_tenancy, "migrate", quick)
        monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
        monkeypatch.setenv("PAYPILOT_LEGACY_TENANT", "default")
        monkeypatch.setenv("PAYPILOT_TENANT", "default")
        assert migrate_tenancy.main([]) == migrate_tenancy.EXIT_COULD_NOT_RUN
        assert "COULD NOT RUN" in capsys.readouterr().out
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# --- the owner row is written inside the transaction ----------------------------


def test_the_owner_row_is_written_inside_the_transaction(tmp_path, monkeypatch):
    path = tmp_path / "in-txn.db"
    _legacy_file(path, with_failure=True)
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    seen = {}

    def observe(conn):
        seen["owner"] = conn.execute(
            "SELECT value FROM tenancy_meta WHERE key = 'owner_tenant'"
        ).fetchone()[0]
        seen["in_transaction"] = conn.in_transaction

    result = migrate_tenancy.migrate(path, legacy_tenant="demo", _after_copy=observe)
    assert result.status == "migrated"
    assert seen == {"owner": "demo", "in_transaction": True}


# --- unverified backups leave nothing behind, also on an interrupt --------------


@pytest.mark.parametrize("interrupt", [False, True], ids=["refused", "keyboard_interrupt"])
def test_an_unverified_backup_leaves_no_bak_and_no_partial(tmp_path, monkeypatch, interrupt):
    path = tmp_path / "unverified.db"
    _legacy_file(path, with_failure=True)
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    real = migrate_tenancy._legacy_digests
    calls = {"n": 0}

    def fail_the_backup_check(conn):
        calls["n"] += 1
        digests = real(conn)
        if calls["n"] == 2:  # the first call is the pre-migration hash
            partial = list(tmp_path.glob("*.partial"))
            assert len(partial) == 1, "the backup is written to a partial name first"
            assert list(tmp_path.glob("*.bak")) == [], "no .bak exists before verification"
            if interrupt:
                raise KeyboardInterrupt
            return {t: (count, "forged") for t, (count, _) in digests.items()}
        return digests

    monkeypatch.setattr(migrate_tenancy, "_legacy_digests", fail_the_backup_check)
    expected = KeyboardInterrupt if interrupt else migrate_tenancy.MigrationRefused
    with pytest.raises(expected):
        migrate_tenancy.migrate(path, legacy_tenant="demo")
    assert list(tmp_path.glob("*.bak")) == []
    assert list(tmp_path.glob("*.partial")) == []
    with pytest.raises(LegacySchemaError):
        Database(path)  # still the legacy schema: the rollback reached the file
