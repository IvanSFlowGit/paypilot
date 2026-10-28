#!/usr/bin/env python3
"""Prove row-level security on a throwaway Postgres: start a cluster, apply the schema, attack it.

Run this on a machine that can start Postgres and can `pip install pg8000` (the session sandbox that
wrote app/pg_backend.py could do neither). It never touches a real database: it makes its own cluster
in a temp directory on a free local port and removes it at the end.

    python scripts/pg_rls_check.py

Exit 0: every check passed. Exit 1: a check FAILED (isolation did not hold). Exit 2: could not run
(no Postgres binaries, no pg8000, cluster would not start): NOT a pass.

Set PAYPILOT_PG_BIN to the directory holding initdb/pg_ctl if it is not on PATH or at the Homebrew
default.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BIN_CANDIDATES = ("/opt/homebrew/opt/postgresql@15/bin", "/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/15/bin")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def find_bin() -> Path | None:
    for c in (os.environ.get("PAYPILOT_PG_BIN"), *BIN_CANDIDATES):
        if c and (Path(c) / "initdb").exists():
            return Path(c)
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cannot_run(msg: str) -> int:
    print(f"CANNOT RUN: {msg}\nThis is NOT a pass: nothing was checked.")
    return 2


def main() -> int:
    try:
        import pg8000.native as pg
    except ImportError:
        return cannot_run("pg8000 is not installed (pip install pg8000)")
    pgbin = find_bin()
    if pgbin is None:
        return cannot_run("initdb not found (set PAYPILOT_PG_BIN)")

    work = Path(tempfile.mkdtemp(prefix="pp_rls_"))
    data, sock, port = work / "data", work / "sock", free_port()
    sock.mkdir()
    started = False
    try:
        subprocess.run([pgbin / "initdb", "-D", str(data), "-A", "trust", "-U", "super"],
                       check=True, capture_output=True)
        subprocess.run([pgbin / "pg_ctl", "-D", str(data), "-w", "-l", str(work / "log"), "start",
                        "-o", f"-p {port} -k {sock} -c listen_addresses=127.0.0.1"],
                       check=True, capture_output=True)
        started = True
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = (getattr(exc, "stderr", b"") or b"").decode(errors="replace")[-400:]
        shutil.rmtree(work, ignore_errors=True)
        return cannot_run(f"cluster did not start: {exc} {detail}")

    def connect(user: str, db: str = "postgres"):
        return pg.Connection(user=user, host="127.0.0.1", port=port, database=db, password="x")

    try:
        sup = connect("super")
        for r in ("paypilot_owner", "paypilot_tenant", "paypilot_platform"):
            sup.run(f"CREATE ROLE {r} LOGIN")
        sup.run("GRANT paypilot_platform TO paypilot_owner")
        sup.run("CREATE DATABASE pp OWNER paypilot_owner")
        sup.close()

        from app import pg_backend

        owner = connect("paypilot_owner", "pp")
        n = pg_backend.apply_schema(owner)
        check("schema applies", n > 20, f"{n} statements")
        try:
            pg_backend.apply_schema(owner)
            check("schema re-applies cleanly (idempotent)", True)
        except Exception as exc:  # noqa: BLE001
            check("schema re-applies cleanly (idempotent)", False, str(exc)[:200])
        owner.close()

        rls = connect("super", "pp")
        flags = {r[0]: (r[1], r[2]) for r in rls.run(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname IN ('failures','events','messages','transitions','approvals','early_paid')")}
        check("RLS enabled AND forced on all six tenant tables",
              len(flags) == 6 and all(v == (True, True) for v in flags.values()), str(flags))
        roles = {r[0]: (r[1], r[2]) for r in rls.run(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'paypilot_%'")}
        check("no paypilot role is superuser or BYPASSRLS",
              all(v == (False, False) for v in roles.values()), str(roles))
        rls.close()

        # Drive it through the real Store, via PgDatabase, with trust-auth connections.
        os.environ["PAYPILOT_TENANCY"] = "multi"
        from app.store import Store

        role_user = {"platform": "paypilot_platform", "tenant": "paypilot_tenant"}
        db = pg_backend.PgDatabase(connect=lambda role: connect(role_user[role], "pp"))
        platform_conn = db.conn
        for t in ("alpha", "beta"):
            platform_conn.execute(
                "INSERT INTO tenants (id, slug, name, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', 'n', 'n')", (t, t, t))
            platform_conn.commit()
        a, b = Store(db, tenant_id="alpha"), Store(db, tenant_id="beta")
        a.record_failure(invoice_id="in_a", customer_id="c", amount_minor=100, currency="eur",
                         failure_code="card_declined", attempt_count=1)
        b.record_failure(invoice_id="in_b", customer_id="c", amount_minor=200, currency="eur",
                         failure_code="card_declined", attempt_count=1)
        check("each tenant sees its own row through the Store",
              a.get_failure("in_a") is not None and b.get_failure("in_b") is not None)
        check("a tenant cannot read another tenant's row through the Store",
              a.get_failure("in_b") is None and b.get_failure("in_a") is None)

        # Raw SQL as the tenant role, with the application's own WHERE clause removed.
        raw = connect("paypilot_tenant", "pp")
        unset = raw.run("SELECT count(*) FROM failures")[0][0]
        check("tenant role with NO setting sees zero rows (fail closed)", unset == 0, str(unset))
        raw.run("START TRANSACTION")
        raw.run("SELECT set_config('paypilot.tenant_id', 'alpha', true)")
        mine = raw.run("SELECT invoice_id FROM failures")
        check("tenant role with its setting sees only its rows, no WHERE needed",
              [r[0] for r in mine] == ["in_a"], str(mine))
        try:
            raw.run("INSERT INTO failures (tenant_id, invoice_id, customer_id, amount_minor, currency, "
                    "failure_code, state, failed_at, updated_at) VALUES "
                    "('beta','in_x','c',1,'eur','x','s','n','n')")
            check("tenant role cannot WRITE a row for another tenant", False, "insert succeeded")
        except Exception as exc:  # noqa: BLE001
            check("tenant role cannot WRITE a row for another tenant", "42501" in str(exc) or
                  "row-level security" in str(exc), str(exc)[:160])
        raw.run("ROLLBACK")
        raw.run("START TRANSACTION")
        raw.run("SELECT set_config('paypilot.tenant_id', 'alpha', true)")
        raw.run("UPDATE failures SET state = 'hacked' WHERE invoice_id = 'in_b'")
        raw.run("COMMIT")
        sup2 = connect("super", "pp")
        state_b = sup2.run("SELECT state FROM failures WHERE invoice_id = 'in_b'")[0][0]
        check("tenant role cannot UPDATE another tenant's row", state_b != "hacked", state_b)
        sup2.close()
        raw.run("START TRANSACTION")
        raw.run("SELECT set_config('paypilot.tenant_id', 'alpha', true)")
        after_commit = raw.run("SELECT current_setting('paypilot.tenant_id', true)")[0][0]
        raw.run("COMMIT")
        reset = raw.run("SELECT nullif(current_setting('paypilot.tenant_id', true), '')")[0][0]
        check("the setting does not survive the transaction", after_commit == "alpha" and reset is None,
              str(reset))
        for table in ("api_tokens", "tenants", "tenant_config", "tenancy_meta"):
            try:
                raw.run(f"SELECT * FROM {table}")
                check(f"tenant role cannot read platform table {table}", False, "select succeeded")
            except Exception as exc:  # noqa: BLE001
                check(f"tenant role cannot read platform table {table}", "42501" in str(exc), str(exc)[:120])
        raw.close()

        aid = a.create_approval(invoice_id="in_a", channel="email", recipient="r@x.test", subject="s",
                                body="b", token_hash="h", expires_at="2099-01-01T00:00:00")
        from app.store import Platform

        plat = Platform(db)
        check("Platform resolves an approval's tenant across tenants", plat.approval_tenant(aid) == "alpha")
        check("Platform lists tenants with pending approvals", "alpha" in plat.tenants_with_pending_approvals())
        db.close()
    finally:
        if started:
            subprocess.run([pgbin / "pg_ctl", "-D", str(data), "-m", "immediate", "stop"],
                           capture_output=True)
        shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
