"""The pre-deploy rehearsal: passes on a migratable legacy ledger, fails loudly otherwise."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from tests.test_tenancy_migration import _build_legacy

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("predeploy", ROOT / "scripts" / "predeploy_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_legacy_ledger_passes_and_the_original_is_untouched(tmp_path, capsys):
    legacy = tmp_path / "live.db"
    _build_legacy(legacy)
    before = legacy.read_bytes()
    assert _load().main([str(legacy)]) == 0
    out = capsys.readouterr().out
    assert "PASS: this ledger migrates cleanly" in out
    assert legacy.read_bytes() == before, "the rehearsal modified the file it was given"


def test_a_current_ledger_also_passes(tmp_path, capsys):
    from app.store import Database

    current = tmp_path / "current.db"
    Database(current, owner_tenant="demo").close()
    assert _load().main([str(current)]) == 0


def test_a_missing_file_and_a_non_database_are_could_not_run(tmp_path, capsys):
    mod = _load()
    assert mod.main([str(tmp_path / "nope.db")]) == 2
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 50)
    assert mod.main([str(junk)]) == 2
    assert mod.main([]) == 2


def test_a_ledger_owned_by_another_tenant_fails_the_rehearsal(tmp_path, capsys):
    """fly.toml binds the demo tenant; the migration refuses a ledger owned by 'default'."""
    from app.store import Database

    owned = tmp_path / "owned.db"
    Database(owned, owner_tenant="default").close()
    assert _load().main([str(owned)]) == 1
    out = capsys.readouterr().out
    assert "[1] migrate: exit 1" in out and "do not push" in out.lower()
