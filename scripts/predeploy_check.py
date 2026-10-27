#!/usr/bin/env python3
"""Rehearse the tenancy migration on a COPY of the live ledger before a deploy.

A push to main deploys by itself, and the container start script migrates the ledger and refuses
to serve if the migration refuses. So the one thing to know before pushing is whether THIS ledger
migrates. This script answers it without touching the live file:

    fly ssh sftp get /data/paypilot.db /somewhere/ledger-copy.db -a paypilot
    python3 scripts/predeploy_check.py /somewhere/ledger-copy.db

It copies the file (and its -wal/-shm if present) into a temporary directory, runs the real
migration against the copy as the demo tenant (what fly.toml sets), checks /health answers 200 on
the migrated copy, then runs the migration again to prove it is a no-op. It prints counts and
verdicts only, never row contents.

Exit: 0 every step passed, 1 a step failed (the live ledger would NOT deploy cleanly), 2 could not run.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("failures", "events", "messages", "transitions", "approvals")


def _counts(path: Path) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        # Raises DatabaseError for a file that is not a database, which is could-not-run and must
        # not be swallowed by the per-table handler below.
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        for table in TABLES:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed names
            except sqlite3.Error:
                out[table] = None
    finally:
        conn.close()
    return out


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: predeploy_check.py PATH_TO_LEDGER_COPY", file=sys.stderr)
        return 2
    source = Path(argv[0])
    if not source.is_file():
        print(f"CANNOT RUN: {source} is not a file", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "ledger.db"
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(source) + suffix)
            if side.is_file():
                shutil.copy2(side, Path(str(work) + suffix))
        try:
            before = _counts(work)
        except sqlite3.Error as exc:
            print(f"CANNOT RUN: not a readable SQLite file ({type(exc).__name__})", file=sys.stderr)
            return 2
        print(f"before: {before}")

        env = {**os.environ, "PAYPILOT_DB_PATH": str(work), "PAYPILOT_TENANT": "demo",
               "PAYPILOT_LEGACY_TENANT": "demo", "PAYPILOT_TENANCY": "single"}
        env.pop("PAYPILOT_TENANT_KEY", None)
        py = sys.executable

        step1 = _run([py, "-m", "app.migrate_tenancy", "--allow-create"], env)
        print(f"[1] migrate: exit {step1.returncode}: {step1.stdout.strip().splitlines()[-1:] or ''}")
        if step1.returncode != 0:
            print("FAIL: the live ledger would refuse to start. Do not push.")
            return 1 if step1.returncode == 1 else 2

        after = _counts(work)
        ok_counts = all(before[t] is None or before[t] == after[t] for t in TABLES)
        print(f"[2] row counts preserved: {'PASS' if ok_counts else 'FAIL'} {after}")

        health = _run([
            py, "-c",
            "from fastapi.testclient import TestClient; from app.api import app; "
            "r = TestClient(app).get('/health'); print(r.status_code); raise SystemExit(0 if r.status_code == 200 else 1)",
        ], env)
        print(f"[3] /health on the migrated copy: {'PASS (200)' if health.returncode == 0 else 'FAIL'}")

        again = _run([py, "-m", "app.migrate_tenancy"], env)
        idempotent = again.returncode == 0 and "already current" in again.stdout
        print(f"[4] second run is a no-op: {'PASS' if idempotent else 'FAIL'}")

        if ok_counts and health.returncode == 0 and idempotent:
            print("PASS: this ledger migrates cleanly and the app serves it.")
            return 0
        print("FAIL: do not push.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
