#!/usr/bin/env python3
"""M8: copy a migrated SQLite ledger into the Postgres backend, then prove the copy.

    python scripts/pg_copy_ledger.py LEDGER.db            # dry run: counts and hashes of the SQLite side
    python scripts/pg_copy_ledger.py LEDGER.db --apply    # copy, then verify every table

The SQLite file must already be on the tenancy schema (python -m app.migrate_tenancy). The target must
be EMPTY: a non-empty target is refused, never merged into. The copy runs in one transaction on the
platform role (the only role with a policy spanning tenants), keeps message/transition/approval ids so
nothing that points at them breaks, resets the id sequences, and then re-reads BOTH sides and compares
a SHA-256 over every row of every table. A count match alone proves nothing about content.

Connection settings are the Postgres backend's (PGHOST, PGDATABASE, PGPORT, PGSSLROOTCERT,
PAYPILOT_PG_PLATFORM_USER, PAYPILOT_PG_PLATFORM_PASSWORD). Exit 0 copied and verified (or dry run
clean), 1 refused or mismatch, 2 could not run. STATUS: written, not run against a real Postgres.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Copy order and the exact columns, in schema order, with the key each table is hashed in.
TABLES: dict[str, tuple[tuple[str, ...], str]] = {
    "tenants": (("id", "slug", "name", "status", "status_reason", "config_json", "secrets_enc",
                 "verified_from_domains", "created_at", "updated_at"), "id"),
    "tenancy_meta": (("key", "value"), "key"),
    "tenant_config": (("tenant_id", "behaviour_json", "secrets_enc", "updated_at"), "tenant_id"),
    "api_tokens": (("token_id", "tenant_id", "role", "secret_hash", "label", "created_by",
                    "created_at", "last_used_at", "revoked_at", "revoked_reason"), "token_id"),
    "failures": (("tenant_id", "invoice_id", "customer_id", "amount_minor", "currency",
                  "attempt_count", "failure_code", "state", "holdout", "recovered_amount_minor",
                  "failed_at", "updated_at", "recovered_at", "stripe_customer_id",
                  "subscription_id"), "tenant_id, invoice_id"),
    "events": (("tenant_id", "event_id", "event_type", "invoice_id", "received_at"),
               "tenant_id, event_id"),
    "messages": (("id", "tenant_id", "invoice_id", "channel", "status", "provider_message_id",
                  "attempt", "error", "created_at", "sent_at"), "id"),
    "transitions": (("id", "tenant_id", "invoice_id", "from_state", "to_state", "reason", "at"), "id"),
    "approvals": (("id", "tenant_id", "invoice_id", "channel", "status", "recipient", "subject",
                   "body", "body_sha256", "token_hash", "created_at", "expires_at", "resolved_at",
                   "resolved_by", "error", "external_id", "gone_since"), "id"),
    "early_paid": (("tenant_id", "invoice_id", "seen_at"), "tenant_id, invoice_id"),
}
SERIAL_TABLES = ("messages", "transitions", "approvals")


class CopyRefused(Exception):
    pass


def digest(rows) -> str:
    """SHA-256 over every row in key order. repr keeps None, bytes and ints distinct from text."""
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(tuple(r)).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def sqlite_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    cols, order = TABLES[table]
    return [tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}")]


def pg_rows(native, table: str) -> list[tuple]:
    cols, order = TABLES[table]
    return [tuple(r) for r in native.run(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}")]


def check_source(conn: sqlite3.Connection) -> None:
    """Refuse a file that is not on the tenancy schema, or whose columns differ from the target's."""
    for table, (cols, _order) in TABLES.items():
        have = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not have:
            raise CopyRefused(f"{table}: missing in the SQLite file (run app.migrate_tenancy first)")
        missing = [c for c in cols if c not in have]
        if missing:
            raise CopyRefused(f"{table}: SQLite file lacks columns {missing}")


def check_target_empty(native) -> None:
    for table in TABLES:
        n = native.run(f"SELECT count(*) FROM {table}")[0][0]
        if n:
            raise CopyRefused(f"target table {table} already holds {n} rows; the copy never merges")


def copy(conn: sqlite3.Connection, native) -> dict[str, int]:
    counts: dict[str, int] = {}
    native.run("START TRANSACTION")
    try:
        for table, (cols, _order) in TABLES.items():
            rows = sqlite_rows(conn, table)
            sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
                   f"({', '.join(':c' + str(i) for i in range(len(cols)))})")
            for row in rows:
                native.run(sql, **{f"c{i}": v for i, v in enumerate(row)})
            counts[table] = len(rows)
        for table in SERIAL_TABLES:
            native.run(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"greatest((SELECT coalesce(max(id), 0) FROM {table}), 1), "
                f"(SELECT count(*) FROM {table}) > 0)"
            )
        native.run("COMMIT")
    except BaseException:
        native.run("ROLLBACK")
        raise
    return counts


def verify(conn: sqlite3.Connection, native) -> list[str]:
    """Compare count and content hash per table. Returns the list of problems (empty = identical)."""
    problems: list[str] = []
    for table in TABLES:
        a, b = sqlite_rows(conn, table), pg_rows(native, table)
        if len(a) != len(b):
            problems.append(f"{table}: {len(a)} rows in SQLite, {len(b)} in Postgres")
        elif digest(a) != digest(b):
            problems.append(f"{table}: same row count ({len(a)}) but different content")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ledger")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.ledger)
    if not path.is_file():
        print(f"CANNOT RUN: {path} is not a file")
        return 2
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        check_source(conn)
    except CopyRefused as exc:
        print(f"REFUSED: {exc}")
        return 1
    print("source:")
    for table in TABLES:
        rows = sqlite_rows(conn, table)
        print(f"  {table:13} {len(rows):6} rows  sha256 {digest(rows)[:16]}")
    if not args.apply:
        print("\ndry run: nothing written. Re-run with --apply to copy into Postgres.")
        return 0
    try:
        from app import pg_backend

        native = pg_backend._default_connect("platform")  # noqa: SLF001 - same settings as the app
    except Exception as exc:  # noqa: BLE001
        print(f"CANNOT RUN: could not connect to Postgres: {type(exc).__name__}: {str(exc)[:200]}")
        return 2
    try:
        check_target_empty(native)
        counts = copy(conn, native)
    except CopyRefused as exc:
        print(f"REFUSED: {exc}")
        return 1
    problems = verify(conn, native)
    if problems:
        print("\nMISMATCH after copy (the target now holds the copy; do not use it):")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"\ncopied and verified: {sum(counts.values())} rows across {len(counts)} tables, "
          "every table identical by count and content hash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
