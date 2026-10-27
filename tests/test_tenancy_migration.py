"""The explicit tenancy migration (app/migrate_tenancy.py, docs/multi-tenancy.md).

The legacy database is built from ``_SCHEMA`` exactly as it was at HEAD before
this milestone, so the test runs against the schema a real deployment holds, not
against a schema this code wrote. Rows are realistic and include approvals with
non-contiguous ids and a sequence high-water mark above the largest live id.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app import migrate_tenancy
from app.store import Database, LegacySchemaError, Store, TenantMismatchError

#: ``_SCHEMA`` from app/store.py at HEAD, before the tenancy milestone.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    invoice_id              TEXT PRIMARY KEY,
    customer_id             TEXT NOT NULL,
    amount_minor            INTEGER NOT NULL,
    currency                TEXT NOT NULL,
    attempt_count           INTEGER NOT NULL DEFAULT 1,
    failure_code            TEXT NOT NULL,
    state                   TEXT NOT NULL,
    holdout                 INTEGER NOT NULL DEFAULT 0,
    recovered_amount_minor  INTEGER,
    failed_at               TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    recovered_at            TEXT,
    -- Stripe's own identifiers, kept alongside customer_id (which may be a
    -- local/demo id resolved from metadata). Closing events name the Stripe
    -- customer or subscription, so matching needs the real ones.
    stripe_customer_id      TEXT,
    subscription_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_failures_state ON failures(state);
CREATE INDEX IF NOT EXISTS idx_failures_customer ON failures(customer_id);

-- Processed Stripe event ids. Survives restarts, which an in-process cache does
-- not, so a redelivered invoice.paid cannot be counted as a second recovery.
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    invoice_id   TEXT,
    received_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id           TEXT NOT NULL,
    channel              TEXT NOT NULL,
    status               TEXT NOT NULL,
    provider_message_id  TEXT,
    attempt              INTEGER NOT NULL DEFAULT 1,
    error                TEXT,
    created_at           TEXT NOT NULL,
    sent_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_invoice ON messages(invoice_id);

-- Append-only history of every state move, so a disputed number can be traced
-- back to the events that produced it.
CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_invoice ON transitions(invoice_id);

-- Human approval of outbound mail. One row per message awaiting (or having had)
-- a person's decision. ``body`` is the exact text that will leave, frozen at
-- creation, and ``body_sha256`` pins it, so what the approver read is what is
-- sent. The raw approval token is never stored, only its hash.
CREATE TABLE IF NOT EXISTS approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      TEXT NOT NULL,
    channel         TEXT NOT NULL,
    status          TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    body_sha256     TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT,
    error           TEXT,
    external_id     TEXT,
    gone_since      TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_invoice ON approvals(invoice_id);
-- At most one open request per invoice: a Stripe retry storm must not stack
-- five approval emails in the approver's inbox.
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_one_pending
    ON approvals(invoice_id) WHERE status = 'pending';
"""

LEGACY_TABLES = ("failures", "events", "messages", "transitions", "approvals")
MIGRATED_TENANT = "demo"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_legacy(path: Path) -> None:
    """A realistic pre-tenancy ledger with non-contiguous approval ids."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO failures (invoice_id, customer_id, amount_minor, currency, "
        "attempt_count, failure_code, state, holdout, recovered_amount_minor, failed_at, "
        "updated_at, recovered_at, stripe_customer_id, subscription_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("in_1", "cus_1", 4900, "eur", 2, "card_declined", "messaged", 0, None,
             "2026-09-01T09:00:00+00:00", "2026-09-02T09:00:00+00:00", None,
             "cus_st_1", "sub_1"),
            ("in_2", "cus_2", 1500, "gbp", 1, "insufficient_funds", "recovered", 1, 1500,
             "2026-09-03T09:00:00+00:00", "2026-09-05T09:00:00+00:00",
             "2026-09-05T09:00:00+00:00", "cus_st_2", None),
            ("in_3", "cus_3", 800, "usd", 1, "expired_card", "failed", 0, None,
             "2026-09-06T09:00:00+00:00", "2026-09-06T09:00:00+00:00", None, None, None),
        ],
    )
    conn.executemany(
        "INSERT INTO events (event_id, event_type, invoice_id, received_at) VALUES (?, ?, ?, ?)",
        [
            ("evt_1", "invoice.payment_failed", "in_1", "2026-09-01T09:00:01+00:00"),
            ("evt_2", "invoice.paid", "in_2", "2026-09-05T09:00:01+00:00"),
            # The salted early-paid marker from the old scheme. It cannot be
            # re-derived after a restart, so it is carried as the history it is.
            ("\x00internal:early-paid:0123456789abcdef0123456789abcdef",
             "invoice.paid.early", "in_9", "2026-09-07T09:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO messages (id, invoice_id, channel, status, provider_message_id, "
        "attempt, error, created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "in_1", "email", "sent", "re_1", 1, None,
             "2026-09-01T10:00:00+00:00", "2026-09-01T10:00:00+00:00"),
            (2, "in_1", "email", "bounced", "re_2", 2, "mailbox full",
             "2026-09-02T10:00:00+00:00", "2026-09-02T10:00:00+00:00"),
            (5, "in_2", "email", "sent", "re_5", 1, None,
             "2026-09-03T10:00:00+00:00", "2026-09-03T10:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO transitions (id, invoice_id, from_state, to_state, reason, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "in_1", "failed", "messaged", "dunning", "2026-09-01T10:00:00+00:00"),
            (2, "in_2", "failed", "recovered", "invoice.paid", "2026-09-05T09:00:00+00:00"),
        ],
    )
    # Ids 3, 7 and 12 are live; id 20 was created and deleted, so the sequence
    # high-water mark is 20 while the largest live id is 12.
    conn.executemany(
        "INSERT INTO approvals (id, invoice_id, channel, status, recipient, subject, body, "
        "body_sha256, token_hash, created_at, expires_at, resolved_at, resolved_by, error, "
        "external_id, gone_since) "
        "VALUES (?, ?, 'email', ?, 'ana@example.test', 'Your payment', 'Please update.', "
        "?, ?, '2026-09-01T11:00:00+00:00', '2026-12-31T00:00:00+00:00', ?, ?, ?, ?, ?)",
        [
            (3, "in_1", "pending", "sha3", "tok3", None, None, None, "draft-3", None),
            (7, "in_2", "approved", "sha7", "tok7", "2026-09-04T11:00:00+00:00", "ana",
             None, "draft-7", None),
            (12, "in_3", "expired", "sha12", "tok12", "2026-09-06T11:00:00+00:00", "system",
             "timed out", None, "2026-09-07T00:00:00+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO approvals (id, invoice_id, channel, status, recipient, subject, body, "
        "body_sha256, token_hash, created_at, expires_at) VALUES (20, 'in_gone', 'email', "
        "'expired', 'x', 'x', 'x', 'x', 'x', '2026-09-01T00:00:00+00:00', "
        "'2026-09-01T01:00:00+00:00')"
    )
    conn.execute("DELETE FROM approvals WHERE id = 20")
    conn.commit()
    conn.close()


def _rows(path: Path, table: str, *, drop_tenant: bool) -> list[tuple]:
    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if not (drop_tenant and c == "tenant_id")]
    order = {"failures": "invoice_id", "events": "event_id"}.get(table, "id")
    rows = conn.execute(
        f"SELECT {', '.join(keep)} FROM {table} ORDER BY {order}"
    ).fetchall()
    conn.close()
    return rows


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A legacy database at PAYPILOT_DB_PATH. The app reads tenant ``demo``."""
    path = tmp_path / "legacy.db"
    _build_legacy(path)
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    monkeypatch.delenv("PAYPILOT_LEGACY_TENANT", raising=False)
    monkeypatch.setenv("PAYPILOT_TENANT", MIGRATED_TENANT)
    return path


def _run(monkeypatch, tenant: str | None, *extra: str) -> int:
    if tenant is None:
        monkeypatch.delenv("PAYPILOT_LEGACY_TENANT", raising=False)
    else:
        monkeypatch.setenv("PAYPILOT_LEGACY_TENANT", tenant)
    return migrate_tenancy.main(list(extra))


# --- refusals --------------------------------------------------------------------


def test_opening_a_legacy_file_refuses_and_changes_no_byte(legacy):
    before = _sha(legacy)
    with pytest.raises(LegacySchemaError):
        Database(legacy)
    with pytest.raises(LegacySchemaError):
        Store(legacy)
    assert _sha(legacy) == before, "a refused open must not rewrite the file"


def test_unset_tenant_is_refused_when_legacy_rows_exist(legacy, monkeypatch, capsys):
    before = _sha(legacy)
    assert _run(monkeypatch, None) == migrate_tenancy.EXIT_REFUSED
    assert "PAYPILOT_LEGACY_TENANT" in capsys.readouterr().out
    assert _sha(legacy) == before
    assert list(legacy.parent.glob("*.bak")) == [], "no backup is taken for a refusal"


def test_a_malformed_tenant_is_refused(legacy, monkeypatch):
    before = _sha(legacy)
    assert _run(monkeypatch, "Not A Tenant") == migrate_tenancy.EXIT_REFUSED
    assert _sha(legacy) == before


def test_a_path_that_cannot_be_opened_as_a_database_could_not_run(tmp_path, monkeypatch):
    not_a_db = tmp_path / "is-a-directory"
    not_a_db.mkdir()
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(not_a_db))
    assert _run(monkeypatch, "demo") == migrate_tenancy.EXIT_COULD_NOT_RUN


# --- the migration itself ---------------------------------------------------------


def test_the_migrated_rows_match_the_legacy_rows_column_for_column(legacy, monkeypatch):
    """The independent check: legacy rows compared against migrated rows, not the
    migration's own hash. Every column except tenant_id, every row, in order."""
    before = {t: _rows(legacy, t, drop_tenant=False) for t in LEGACY_TABLES}
    assert all(before[t] for t in LEGACY_TABLES), "the fixture must hold rows in every table"
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    conn = sqlite3.connect(legacy)
    for table in LEGACY_TABLES:
        assert _rows(legacy, table, drop_tenant=True) == [
            tuple(row) for row in before[table]
        ], f"{table} changed in migration"
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == len(before[table])
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id != ?", (MIGRATED_TENANT,)
        ).fetchone()[0] == 0, f"{table} has rows outside the target tenant"
    conn.close()


def test_approval_ids_survive_and_the_sequence_continues(legacy, monkeypatch):
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    store = Store(legacy, MIGRATED_TENANT)
    try:
        assert [r["id"] for r in store.list_approvals(limit=100)] == [12, 7, 3]
        new_id = store.create_approval(
            invoice_id="in_new", channel="email", recipient="x", subject="s", body="b",
            token_hash="t", expires_at="2099-01-01T00:00:00+00:00",
        )
    finally:
        store.close()
    assert new_id == 21, "the AUTOINCREMENT sequence (20) must carry over"


def test_the_legacy_failures_are_visible_to_the_target_tenant_only(legacy, monkeypatch):
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    # One file, two tenants: multi mode. In single mode the owner refuses the bind.
    with pytest.raises(TenantMismatchError, match="owned by tenant"):
        Store(legacy, "tn_other")
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    demo = Store(legacy, MIGRATED_TENANT)
    other = Store(legacy, "tn_other")
    try:
        assert demo.get_failure("in_1")["amount_minor"] == 4900
        assert demo.get_failure("in_1")["stripe_customer_id"] == "cus_st_1"
        assert other.get_failure("in_1") is None
        assert demo.sent_message_count("in_1") == 2
        assert demo.was_paid_early("in_9") is True, "the legacy marker becomes an early_paid row"
        assert other.was_paid_early("in_9") is False
    finally:
        demo.close()
        other.close()


def test_a_backup_holds_every_pre_migration_row(legacy, monkeypatch):
    """The backup comes from the SQLite backup API, which is not byte-preserving, so
    it is checked by content: every table, every row, every column, in order."""
    before = {t: _rows(legacy, t, drop_tenant=False) for t in LEGACY_TABLES}
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    backups = list(legacy.parent.glob("legacy.db.pre-tenancy-*.bak"))
    assert len(backups) == 1, "exactly one backup beside the database"
    assert {t: _rows(backups[0], t, drop_tenant=False) for t in LEGACY_TABLES} == before


def test_a_corrupted_copy_fails_verification_and_leaves_the_original_untouched(legacy):
    """Swap two columns in one table after the copy. Counts still match; the hash must not."""
    before = _sha(legacy)

    def swap_columns(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE messages SET channel = status, status = channel")

    with pytest.raises(migrate_tenancy.MigrationRefused, match="messages: content hash"):
        migrate_tenancy.migrate(legacy, legacy_tenant=MIGRATED_TENANT, _after_copy=swap_columns)
    assert _sha(legacy) == before, "the rollback must leave the original file byte-identical"
    with pytest.raises(LegacySchemaError):
        Database(legacy)


def test_a_corrupted_copy_is_refused_by_the_command(legacy, monkeypatch, capsys):
    real = migrate_tenancy.migrate

    def corrupt(path, *, legacy_tenant, allow_create=False, _after_copy=None):
        def swap(conn):
            conn.execute("UPDATE events SET event_type = invoice_id, invoice_id = event_type")

        return real(path, legacy_tenant=legacy_tenant, allow_create=allow_create, _after_copy=swap)

    monkeypatch.setattr(migrate_tenancy, "migrate", corrupt)
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "rolled back" in out
    assert "backup remains at" in out


def test_a_second_run_is_a_no_op(legacy, monkeypatch, capsys):
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    after_first = _sha(legacy)
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    assert "already current" in capsys.readouterr().out
    assert _sha(legacy) == after_first


def test_an_empty_legacy_ledger_needs_no_tenant(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.close()
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    assert _run(monkeypatch, None) == migrate_tenancy.EXIT_MIGRATED
    Database(path).close()


def test_a_missing_database_is_refused_unless_creation_is_explicit(tmp_path, monkeypatch, capsys):
    path = tmp_path / "fresh" / "paypilot.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    assert _run(monkeypatch, None) == migrate_tenancy.EXIT_REFUSED
    assert "--allow-create" in capsys.readouterr().out
    assert not path.exists(), "a refused run must not create a file"


def test_a_missing_database_is_created_with_the_explicit_flag(tmp_path, monkeypatch, capsys):
    path = tmp_path / "fresh" / "paypilot.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    assert _run(monkeypatch, None, "--allow-create") == migrate_tenancy.EXIT_MIGRATED
    assert "created" in capsys.readouterr().out
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(failures)")}
    conn.close()
    assert "tenant_id" in cols


def test_the_legacy_early_paid_marker_becomes_an_early_paid_row(legacy, monkeypatch):
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    conn = sqlite3.connect(legacy)
    rows = conn.execute("SELECT tenant_id, invoice_id FROM early_paid").fetchall()
    assert rows == [(MIGRATED_TENANT, "in_9")]
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'invoice.paid.early'"
    ).fetchone()[0] == 1, "the events history is carried, not rewritten"
    assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0
    conn.close()


def test_the_target_must_equal_the_tenant_the_app_reads(legacy, monkeypatch, capsys):
    monkeypatch.delenv("PAYPILOT_TENANT", raising=False)  # the app would read default
    before = _sha(legacy)
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_REFUSED
    assert "PAYPILOT_TENANT" in capsys.readouterr().out
    assert _sha(legacy) == before
    assert list(legacy.parent.glob("*.bak")) == []


def test_an_empty_legacy_tenant_is_refused_when_rows_exist(legacy, monkeypatch, capsys):
    before = _sha(legacy)
    assert _run(monkeypatch, "") == migrate_tenancy.EXIT_REFUSED
    assert "unset or empty" in capsys.readouterr().out
    assert _sha(legacy) == before


@pytest.mark.parametrize("target", ["tn_bravo", "Demo", "not-a-tenant"])
def test_a_target_outside_default_demo_or_registered_is_refused(legacy, monkeypatch, target):
    monkeypatch.setenv("PAYPILOT_TENANT", target)
    before = _sha(legacy)
    assert _run(monkeypatch, target) == migrate_tenancy.EXIT_REFUSED
    assert _sha(legacy) == before


def test_single_mode_get_store_reads_the_migrated_ledger(legacy, monkeypatch):
    """The public app must read the rows it migrated. Migrating into demo and then
    reading the default tenant would present an empty ledger with no error."""
    from app import store as store_module

    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    monkeypatch.delenv("PAYPILOT_TENANCY", raising=False)
    store_module.reset_store(None)
    app_store = store_module.get_store()
    try:
        assert app_store.tenant_id == MIGRATED_TENANT
        assert app_store.get_failure("in_1")["amount_minor"] == 4900
    finally:
        store_module.reset_store(None)
        app_store.close()


def test_a_writer_holding_the_lock_makes_the_run_exit_2_and_changes_nothing(legacy, monkeypatch):
    before = _sha(legacy)
    holder = sqlite3.connect(legacy, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_COULD_NOT_RUN
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _sha(legacy) == before
    assert list(legacy.parent.glob("*.bak")) == []


def test_a_wal_database_with_uncheckpointed_rows_is_backed_up_whole(tmp_path, monkeypatch):
    """Rows still in the WAL are not in the main file: a byte copy of the main file
    would hold no tables at all. The backup must hold every row."""
    path = tmp_path / "wal.db"
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executescript(LEGACY_SCHEMA)
    writer.execute(
        "INSERT INTO failures (invoice_id, customer_id, amount_minor, currency, failure_code, "
        "state, failed_at, updated_at) VALUES ('in_w', 'cus_w', 100, 'eur', 'x', 'failed', "
        "'2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
    )
    assert (tmp_path / "wal.db-wal").stat().st_size > 0, "the rows must sit in the WAL"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    monkeypatch.setenv("PAYPILOT_TENANT", MIGRATED_TENANT)
    try:
        assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    finally:
        writer.close()
    backups = list(tmp_path.glob("wal.db.pre-tenancy-*.bak"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 1
    finally:
        backup.close()


def test_migration_leaves_the_tenant_table_empty_and_present(legacy, monkeypatch):
    assert _run(monkeypatch, MIGRATED_TENANT) == migrate_tenancy.EXIT_MIGRATED
    conn = sqlite3.connect(legacy)
    assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0
    conn.close()


