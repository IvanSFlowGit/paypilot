# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Explicit migration of the recovery ledger to the tenant-scoped schema.

Run as ``python -m app.migrate_tenancy`` from the container start script, before the
app serves traffic. It is never run by opening a database: :class:`app.store.Database`
refuses a pre-tenancy file instead.

Procedure. Every refusal point is before anything is changed, or rolls back.

1. A missing file is refused unless ``--allow-create`` is passed.
2. ``BEGIN IMMEDIATE`` takes the write lock, waiting at most ``lock_timeout``
   (5 seconds). Running out is exit 2. Everything below happens under that lock,
   so the legacy count, the tenant decision, the backup and the digests describe
   ONE state.
3. Legacy rows exist and ``PAYPILOT_LEGACY_TENANT`` is unset or empty: refuse.
   The owner is never guessed. The target must equal ``PAYPILOT_TENANT`` (the
   tenant the app reads) and be ``default``, ``demo`` or a registered tenant.
4. The backup is taken with the SQLite backup API from a second connection, so
   it includes uncheckpointed WAL pages. The backup is then reopened and its
   per-table digests must equal the digests taken under the lock. A backup that
   is not the migrated-from state is removed and the run is refused.
5. The rebuild runs in the same transaction: each table is copied with
   ``tenant_id`` set, the legacy table is dropped and the new one renamed, the
   AUTOINCREMENT sequence is carried, and legacy early-paid markers become
   ``early_paid`` rows.
6. Verification measures the whole. A content hash per table over every column
   except ``tenant_id`` must equal the pre-migration hash, the row count must
   equal the old count, and the per-tenant breakdown must be exactly
   ``{target: old_count}``. Any mismatch rolls back. The backup stays on disk.

Exit codes: 0 migrated, created or already current; 1 refused (nothing changed,
or verification failed and was rolled back); 2 could not run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.store import (
    _DEFAULT_DB_PATH,
    _TABLE_DDL,
    MIGRATED_TABLES,
    TENANT_TABLES,
    Database,
    _apply_schema,
    _check_tenant_id,
    _is_legacy,
    configured_tenant,
    tenancy_mode,
)

EXIT_MIGRATED = 0
EXIT_REFUSED = 1
EXIT_COULD_NOT_RUN = 2

#: Row order for the content hash. Each key is unique within its table.
_ORDER_BY = {
    "failures": "invoice_id",
    "events": "event_id",
    "messages": "id",
    "transitions": "id",
    "approvals": "id",
}

_AUTOINCREMENT = ("messages", "transitions", "approvals")

#: Targets that need no row in ``tenants``.
_RESERVED_TARGETS = ("default", "demo")


class MigrationRefused(Exception):
    """A precondition failed, or verification did not match. Exit 1."""


class MigrationFailed(RuntimeError):
    """The run could not complete and may need a person (exit 2)."""


@dataclass
class MigrationResult:
    """What happened. ``status`` is ``migrated``, ``created`` or ``current``."""

    status: str
    backup: Path | None = None
    backup_sha256: str | None = None
    tenant: str | None = None
    rows: dict[str, int] = field(default_factory=dict)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if "tenant_id" in cols:
        raise MigrationRefused(f"{table} already has tenant_id; the file is mixed, refusing")
    return cols


def _digest(conn: sqlite3.Connection, table: str, columns: list[str]) -> tuple[int, str]:
    """Row count and a sha256 over every row, ordered and serialised canonically."""
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {_ORDER_BY[table]}"
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(list(row), separators=(",", ":"), ensure_ascii=True, default=str).encode()
        )
        digest.update(b"\n")
    return len(rows), digest.hexdigest()


def _legacy_digests(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    return {t: _digest(conn, t, _legacy_columns(conn, t)) for t in MIGRATED_TABLES}


def _legacy_row_count(conn: sqlite3.Connection) -> int:
    return sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in MIGRATED_TABLES)


def _valid_target(conn: sqlite3.Connection, tenant: str) -> str:
    """A target must be a reserved name or a tenant already registered."""
    try:
        target = _check_tenant_id(tenant)
    except ValueError as exc:
        raise MigrationRefused(str(exc)) from exc
    if target in _RESERVED_TARGETS:
        return target
    has_tenants = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tenants'"
    ).fetchone()
    if has_tenants and conn.execute("SELECT 1 FROM tenants WHERE id = ?", (target,)).fetchone():
        return target
    raise MigrationRefused(
        f"target tenant {target!r} is neither default, demo nor a registered tenant"
    )


def _decide_target(conn: sqlite3.Connection, legacy_tenant: str | None, legacy_rows: int) -> str:
    """The tenant the legacy rows move into. Never guessed, never defaulted when rows exist."""
    if legacy_rows:
        if not legacy_tenant:  # None and "" both refuse
            raise MigrationRefused(
                "legacy rows exist and PAYPILOT_LEGACY_TENANT is unset or empty; "
                "refusing to guess which tenant owns them"
            )
        target = _valid_target(conn, legacy_tenant)
        try:
            configured = configured_tenant()
        except ValueError as exc:
            raise MigrationRefused(str(exc)) from exc
        if target != configured:
            raise MigrationRefused(
                f"target {target!r} differs from PAYPILOT_TENANT={configured!r}; the app "
                "would read an empty tenant after this migration"
            )
        return target
    # No rows: the file is owned by the tenant the app will read. A different
    # PAYPILOT_LEGACY_TENANT is a contradiction, not a choice, so it refuses.
    configured = _configured_or_refuse()
    _refuse_contradiction(legacy_tenant, configured)
    return _valid_target(conn, configured)


def _configured_or_refuse() -> str:
    try:
        return configured_tenant()
    except ValueError as exc:
        raise MigrationRefused(str(exc)) from exc


def _refuse_contradiction(legacy_tenant: str | None, configured: str) -> None:
    """A PAYPILOT_LEGACY_TENANT naming another tenant is a contradiction, not a choice.

    Shared by every branch that can create or own a file, so no branch is the odd one
    out: the empty-legacy case and the allow-create case refuse identically.
    """
    if legacy_tenant and legacy_tenant != configured:
        raise MigrationRefused(
            f"PAYPILOT_LEGACY_TENANT={legacy_tenant!r} differs from PAYPILOT_TENANT="
            f"{configured!r}; a file is owned by the tenant the app reads"
        )


def _read_owner(conn: sqlite3.Connection) -> str | None:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tenancy_meta'"
    ).fetchone() is None:
        return None
    row = conn.execute(
        "SELECT value FROM tenancy_meta WHERE key = 'owner_tenant'"
    ).fetchone()
    return row[0] if row else None


def _reconcile_current(conn: sqlite3.Connection, configured: str) -> None:
    """Check an already-current file against the configured tenant, under the lock.

    In single mode an owner that differs is refused. No owner is backfilled, but only
    when every row in every tenant table, early_paid included, already belongs to the
    configured tenant: a file with rows for someone else and no recorded owner is
    refused, because nothing in the file says whose it is.

    In multi mode a file holds several tenants by design, so there is no single owner
    to compare or to backfill. The file is reported current and nothing is written.
    """
    _apply_schema(conn)  # an older current file may predate tenancy_meta
    if tenancy_mode() == "multi":
        return
    owner = _read_owner(conn)
    if owner is not None:
        if owner != configured:
            raise MigrationRefused(
                f"the file is owned by tenant {owner!r} but PAYPILOT_TENANT={configured!r}; "
                "refusing to report it as current for another tenant"
            )
        return
    others: set[str] = set()
    for table in TENANT_TABLES:
        others.update(r[0] for r in conn.execute(f"SELECT DISTINCT tenant_id FROM {table}"))
    others.discard(configured)
    if others:
        raise MigrationRefused(
            f"no owner is recorded and rows belong to {sorted(others)}, not {configured!r}; "
            "no owner can be established from this file, so it is refused until the owner "
            "is restored by hand"
        )
    conn.execute(
        "INSERT OR REPLACE INTO tenancy_meta (key, value) VALUES ('owner_tenant', ?)",
        (configured,),
    )


def _probe_kind(conn: sqlite3.Connection) -> str:
    """``legacy`` or ``current`` for a PayPilot ledger; refuses anything else."""
    if _is_legacy(conn):
        return "legacy"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(failures)")}
    if "tenant_id" in cols:
        return "current"
    raise MigrationRefused(
        "not a PayPilot ledger: the file has no failures table carrying tenant_id"
    )


def _take_backup(source: sqlite3.Connection, db_path: Path, expected) -> tuple[Path, str]:
    """Back up through the SQLite API and prove the copy holds the migrated-from state.

    The copy is written to ``<name>.partial``, verified, and only then renamed to its
    final name. Any failure removes the partial file, so a ``.bak`` that exists is
    always one that was verified.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = db_path.with_name(f"{db_path.name}.pre-tenancy-{stamp}.bak")
    partial = dest.with_name(f"{dest.name}.partial")
    if dest.exists():
        raise MigrationRefused(f"backup path {dest} already exists; refusing to overwrite")
    try:
        target = sqlite3.connect(str(partial))
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
        check = sqlite3.connect(str(partial))
        try:
            held = _legacy_digests(check)
        finally:
            check.close()
        if held != expected:
            raise MigrationRefused(
                "backup does not hold the pre-migration rows; removed it and refused"
            )
        os.replace(partial, dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return dest, _sha256_file(dest)


def _rollback(conn: sqlite3.Connection, backup: Path | None) -> None:
    """Undo an open transaction. A failed rollback is reported, with the backup's location."""
    if not conn.in_transaction:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        where = f" The pre-migration copy remains at {backup}." if backup else ""
        raise MigrationFailed(
            f"rollback failed ({exc}); the database may be partially migrated.{where}"
        ) from exc


def _rebuild(
    conn: sqlite3.Connection,
    target: str,
    before: dict[str, tuple[int, str]],
    backup: Path,
    after_copy: Callable[[sqlite3.Connection], None] | None,
) -> dict[str, int]:
    """Copy every migrated table into the target tenant and verify it. Caller commits."""
    columns = {t: _legacy_columns(conn, t) for t in MIGRATED_TABLES}
    sequences: dict[str, int] = {}
    for table in _AUTOINCREMENT:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
        if row is not None:
            sequences[table] = int(row[0])

    for table in MIGRATED_TABLES:
        conn.execute(_TABLE_DDL[table].format(name=f"{table}__new"))
        cols = ", ".join(columns[table])
        conn.execute(
            f"INSERT INTO {table}__new (tenant_id, {cols}) SELECT ?, {cols} FROM {table}",
            (target,),
        )
    for table in MIGRATED_TABLES:
        conn.execute(f"DROP TABLE {table}")
    for table in MIGRATED_TABLES:
        conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
    for table, seq in sequences.items():
        cur = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (seq, table)
        )
        if cur.rowcount == 0:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, seq))
    _apply_schema(conn)

    # Legacy markers: the salted rows in events become early_paid rows. The events
    # rows stay, as history, so the events hash is unchanged.
    conn.execute(
        "INSERT OR IGNORE INTO early_paid (tenant_id, invoice_id, seen_at) "
        "SELECT ?, invoice_id, received_at FROM events "
        "WHERE event_type = 'invoice.paid.early' AND invoice_id IS NOT NULL",
        (target,),
    )

    # The owner is recorded in the same transaction as the move: a file that holds
    # rows for a tenant always says which tenant that is.
    conn.execute(
        "INSERT OR REPLACE INTO tenancy_meta (key, value) VALUES ('owner_tenant', ?)",
        (target,),
    )

    if after_copy is not None:
        after_copy(conn)

    counts: dict[str, int] = {}
    for table in MIGRATED_TABLES:
        after = _digest(conn, table, columns[table])
        if after != before[table]:
            raise MigrationRefused(
                f"{table}: content hash differs after copy; rolled back. "
                f"The original is unchanged and the backup remains at {backup}."
            )
        total = before[table][0]
        per_tenant = dict(
            conn.execute(f"SELECT tenant_id, COUNT(*) FROM {table} GROUP BY tenant_id").fetchall()
        )
        expected = {target: total} if total else {}
        if per_tenant != expected:
            raise MigrationRefused(
                f"{table}: per-tenant rows {per_tenant} != {expected}; rolled back. "
                f"The original is unchanged and the backup remains at {backup}."
            )
        counts[table] = total
    return counts


def migrate(
    path: str | Path,
    *,
    legacy_tenant: str | None,
    allow_create: bool = False,
    lock_timeout: float = 5.0,
    _after_copy: Callable[[sqlite3.Connection], None] | None = None,
) -> MigrationResult:
    """Migrate the file at ``path``. Raises :class:`MigrationRefused` on any refusal.

    ``lock_timeout`` bounds how long the write lock is awaited. Running out is a
    "could not run" (exit 2), not a refusal.

    ``_after_copy`` is a test seam: it runs inside the transaction after the copy
    and before verification, so a test can corrupt the copy and prove the check
    catches it.
    """
    db_path = Path(path)
    if not db_path.exists():
        if not allow_create:
            raise MigrationRefused(
                f"no database at {db_path}. Pass --allow-create to create the current schema."
            )
        # The same contradiction check as the empty-legacy branch, BEFORE the file
        # exists: a refused create must leave nothing on disk.
        configured = _configured_or_refuse()
        _refuse_contradiction(legacy_tenant, configured)
        Database(db_path, owner_tenant=configured).close()
        return MigrationResult("created")

    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=lock_timeout)
    reader: sqlite3.Connection | None = None
    backup: Path | None = None
    try:
        try:
            _probe_kind(conn)  # refuses a non-ledger before any lock is taken
        except sqlite3.OperationalError:
            raise  # a held lock is "could not run" (exit 2), not "not a ledger"
        except sqlite3.DatabaseError as exc:
            raise MigrationRefused(f"{db_path} is not a readable SQLite database ({exc})") from exc
        configured = _configured_or_refuse()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _probe_kind(conn) == "current":
                _reconcile_current(conn, configured)
                conn.execute("COMMIT")
                return MigrationResult("current")
            target = _decide_target(conn, legacy_tenant, _legacy_row_count(conn))
            before = _legacy_digests(conn)
            reader = sqlite3.connect(str(db_path))
            backup, backup_sha = _take_backup(reader, db_path, before)
            rows = _rebuild(conn, target, before, backup, _after_copy)
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn, backup)
            raise
        return MigrationResult(
            "migrated", backup=backup, backup_sha256=backup_sha, tenant=target, rows=rows
        )
    finally:
        if reader is not None:
            reader.close()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrate_tenancy")
    parser.add_argument(
        "--allow-create",
        action="store_true",
        help="create the current schema when no database file exists",
    )
    args = parser.parse_args(argv)
    path = os.getenv("PAYPILOT_DB_PATH") or str(_DEFAULT_DB_PATH)
    legacy = os.getenv("PAYPILOT_LEGACY_TENANT")
    try:
        result = migrate(path, legacy_tenant=legacy, allow_create=args.allow_create)
    except MigrationRefused as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    except (sqlite3.Error, OSError, MigrationFailed, ValueError) as exc:
        print(f"COULD NOT RUN: {exc}")
        return EXIT_COULD_NOT_RUN

    if result.status == "current":
        print(f"already current: {path} has the tenant-scoped schema. No change.")
    elif result.status == "created":
        print(f"created: {path} now has the current schema.")
    else:
        counts = ", ".join(f"{t}={n}" for t, n in result.rows.items())
        print(f"migrated {path} into tenant {result.tenant!r}: {counts}")
        print(f"backup: {result.backup} sha256={result.backup_sha256}")
    return EXIT_MIGRATED


if __name__ == "__main__":
    sys.exit(main())
