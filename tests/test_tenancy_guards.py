"""Tenancy guards from rework cycle 2.

* A file records its owner tenant; a process bound to another tenant refuses it.
* Multi mode refuses the no-argument Store, not just get_store().
* A backup is written to a partial name and only renamed after it verifies.
* A forged early-paid webhook marks nothing.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from app import migrate_tenancy
from app.store import (
    AmbientTenantError,
    Database,
    LegacySchemaError,
    Store,
    TenantMismatchError,
    get_store,
    reset_store,
)

ROOT = Path(__file__).resolve().parent.parent
MIGRATED = "demo"


def _legacy_schema() -> str:
    """The pre-tenancy schema, read from the migration test's own fixture so the two
    cannot drift apart."""
    import importlib.util as util

    spec = util.spec_from_file_location("tenancy_migration_fixture", ROOT / "tests" / "test_tenancy_migration.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LEGACY_SCHEMA


def _legacy_file(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_legacy_schema())
    conn.execute(
        "INSERT INTO failures (invoice_id, customer_id, amount_minor, currency, failure_code, "
        "state, failed_at, updated_at, stripe_customer_id) VALUES ('in_1', 'cus_1', 4900, 'eur', "
        "'card_declined', 'failed', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00', "
        "'cus_st_1')"
    )
    conn.commit()
    conn.close()


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    """A legacy file migrated into ``demo``, with PAYPILOT_TENANT=demo while migrating."""
    path = tmp_path / "ledger.db"
    _legacy_file(path)
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    monkeypatch.setenv("PAYPILOT_TENANT", MIGRATED)
    result = migrate_tenancy.migrate(path, legacy_tenant=MIGRATED)
    assert result.status == "migrated"
    monkeypatch.delenv("PAYPILOT_TENANT", raising=False)
    reset_store(None)
    return path


# --- owner recorded in the file -------------------------------------------------


def test_the_migration_records_the_owner_in_the_file(migrated):
    db = Database(migrated)
    try:
        assert db.owner_tenant() == MIGRATED
    finally:
        db.close()


def test_an_unset_tenant_refuses_get_store_after_migrating_into_demo(migrated):
    """The operator who forgot PAYPILOT_TENANT must be refused, not shown an empty
    ledger that reads as 'no data held'."""
    with pytest.raises(TenantMismatchError, match="demo"):
        get_store()
    with pytest.raises(TenantMismatchError):
        Store(migrated)


def test_the_gdpr_export_script_refuses_an_unset_tenant(migrated):
    export = _script("gdpr_export")
    with pytest.raises(TenantMismatchError):
        export.export_subject(customer_id="cus_1")


def test_the_gdpr_erase_script_refuses_an_unset_tenant(migrated):
    erase = _script("gdpr_erase")
    with pytest.raises(TenantMismatchError):
        erase.erase_subject(customer_id="cus_1")


def test_the_matching_tenant_reads_the_ledger(migrated, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANT", MIGRATED)
    store = get_store()
    try:
        assert store.get_failure("in_1")["stripe_customer_id"] == "cus_st_1"
    finally:
        reset_store(None)
        store.close()


def test_a_fresh_database_is_owned_by_the_tenant_it_was_created_for(tmp_path, monkeypatch):
    """Creation records the owner in the same step as the schema, so no fresh file is
    ever unowned. A later process bound to another tenant is refused."""
    path = tmp_path / "fresh.db"
    monkeypatch.delenv("PAYPILOT_TENANT", raising=False)
    first = Store(path)
    try:
        assert first.tenant_id == "default"
        assert first._db.owner_tenant() == "default"
    finally:
        first.close()
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    with pytest.raises(TenantMismatchError, match="default"):
        Store(path)
    # Single mode: an explicit bind to a tenant other than the owner is refused too.
    with pytest.raises(TenantMismatchError, match="default"):
        Store(path, "demo")
    owner = Store(path, "default")
    try:
        assert owner.tenant_id == "default", "binding the owner is always allowed"
    finally:
        owner.close()


def test_a_file_created_through_an_explicit_bind_is_owned_by_that_tenant(tmp_path, monkeypatch):
    """Creation stamps the tenant the store is bound to, not the environment's. The
    environment says ``default`` here; the file is still ``demo``'s."""
    path = tmp_path / "explicit.db"
    monkeypatch.delenv("PAYPILOT_TENANT", raising=False)
    created = Store(path, "demo")
    try:
        assert created._db.owner_tenant() == "demo"
    finally:
        created.close()
    with pytest.raises(TenantMismatchError, match="demo"):
        Store(path)


def test_an_invalid_tenant_leaves_no_file_or_schema_behind(tmp_path, monkeypatch):
    """The tenant is validated before the database is opened, so a refused bind is
    not a half built file. Both forms: from the environment, and explicit."""
    monkeypatch.setenv("PAYPILOT_TENANT", "Demo")
    env_path = tmp_path / "env" / "new.db"
    with pytest.raises(ValueError):
        Store(env_path)
    assert not env_path.exists()
    monkeypatch.delenv("PAYPILOT_TENANT", raising=False)
    explicit_path = tmp_path / "explicit" / "new.db"
    with pytest.raises(ValueError):
        Store(explicit_path, "Demo")
    assert not explicit_path.exists()


def test_the_allow_create_branch_records_the_owner(tmp_path, monkeypatch):
    path = tmp_path / "created" / "paypilot.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    assert migrate_tenancy.main(["--allow-create"]) == migrate_tenancy.EXIT_MIGRATED
    db = Database(path)
    try:
        assert db.owner_tenant() == "demo"
    finally:
        db.close()


def test_a_multi_mode_database_handle_cannot_be_wrapped_without_a_tenant(tmp_path, monkeypatch):
    """Store(Database(path)) is the same ambient form as Store(path) and must refuse too."""
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    db = Database(tmp_path / "multi-handle.db")
    try:
        with pytest.raises(AmbientTenantError):
            Store(db)
        assert Store(db, "tn_alpha").tenant_id == "tn_alpha"
    finally:
        db.close()


def test_a_legacy_file_still_refuses_to_open_before_migration(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_file(path)
    with pytest.raises(LegacySchemaError):
        Database(path)


# --- multi mode has no ambient Store --------------------------------------------


def test_multi_mode_refuses_a_store_without_an_explicit_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    path = tmp_path / "multi.db"
    with pytest.raises(AmbientTenantError):
        Store(path)
    with pytest.raises(AmbientTenantError):
        Store(path, None)
    explicit = Store(path, "tn_alpha")
    try:
        assert explicit.tenant_id == "tn_alpha"
    finally:
        explicit.close()


def test_single_mode_store_path_still_works_for_demo_loop_callers(tmp_path, monkeypatch):
    """scripts/demo_loop.py calls Store(args.db) in single mode. It must still bind."""
    monkeypatch.delenv("PAYPILOT_TENANCY", raising=False)
    store = Store(tmp_path / "demo_loop.db")
    try:
        assert store.tenant_id == "default"
    finally:
        store.close()


# --- backup: written partial, verified, then renamed ----------------------------


def test_a_partial_backup_that_fails_verification_leaves_no_file_behind(tmp_path, monkeypatch):
    path = tmp_path / "partial.db"
    _legacy_file(path)
    monkeypatch.setenv("PAYPILOT_TENANT", MIGRATED)
    real = migrate_tenancy._legacy_digests
    calls = {"n": 0}

    def forged_on_the_backup_check(conn):
        calls["n"] += 1
        digests = real(conn)
        if calls["n"] == 2:  # the first call is the pre-migration hash; the second checks the backup
            return {t: (count, "forged") for t, (count, _) in digests.items()}
        return digests

    monkeypatch.setattr(migrate_tenancy, "_legacy_digests", forged_on_the_backup_check)
    with pytest.raises(migrate_tenancy.MigrationRefused, match="backup does not hold"):
        migrate_tenancy.migrate(path, legacy_tenant=MIGRATED)
    assert list(tmp_path.glob("*.bak")) == [], "an unverified backup must not get a .bak name"
    assert list(tmp_path.glob("*.partial")) == [], "the partial copy must be removed"
    with pytest.raises(LegacySchemaError):
        Database(path)


# --- a webhook cannot forge the early-paid marker -------------------------------


def test_a_forged_invoice_paid_early_webhook_marks_nothing(isolated_store, monkeypatch):
    from fastapi.testclient import TestClient

    from app import api as api_module

    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "1")
    body = (
        b'{"id": "evt_forge_1", "type": "invoice.paid.early", "data": {"object": '
        b'{"id": "in_victim", "invoice": "in_victim", "customer": "cus_victim"}}}'
    )
    client = TestClient(api_module.app)
    response = client.post(
        "/webhooks/stripe", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code < 500
    assert isolated_store.was_paid_early("in_victim") is False
