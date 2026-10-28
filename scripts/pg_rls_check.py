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
        (work / "pw").write_text("x\n")
        # scram-sha-256, not trust: the per-tenant logins must actually authenticate with the derived
        # password, or the test would pass whatever the app derived.
        subprocess.run([pgbin / "initdb", "-D", str(data), "-A", "scram-sha-256", "-U", "super",
                        "--pwfile", str(work / "pw")],
                       check=True, capture_output=True)
        subprocess.run([pgbin / "pg_ctl", "-D", str(data), "-w", "-l", str(work / "log"), "start",
                        "-o", f"-p {port} -k {sock} -c listen_addresses=127.0.0.1"],
                       check=True, capture_output=True)
        started = True
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = (getattr(exc, "stderr", b"") or b"").decode(errors="replace")[-400:]
        shutil.rmtree(work, ignore_errors=True)
        return cannot_run(f"cluster did not start: {exc} {detail}")

    def connect(user: str, db: str = "postgres", password: str = "x"):
        return pg.Connection(user=user, host="127.0.0.1", port=port, database=db, password=password)

    try:
        sup = connect("super")
        for r in ("paypilot_owner", "paypilot_tenant", "paypilot_platform"):
            sup.run(f"CREATE ROLE {r} LOGIN PASSWORD 'x'")
        sup.run("GRANT paypilot_platform TO paypilot_owner")
        sup.run("CREATE DATABASE pp OWNER paypilot_owner")
        sup.close()
        # PG15+: a non-superuser cannot create in `public` unless granted. Found on the first real
        # run (SQLSTATE 42501 at the first CREATE TABLE). The operator does this once for a real DB.
        sup = connect("super", "pp")
        sup.run("GRANT ALL ON SCHEMA public TO paypilot_owner")
        sup.run("GRANT USAGE ON SCHEMA public TO paypilot_tenant, paypilot_platform")
        # ALTER FUNCTION ... OWNER TO needs the NEW owner to hold CREATE on the schema. Granted for
        # the apply and revoked straight after, so the platform role does not keep it.
        sup.run("GRANT CREATE ON SCHEMA public TO paypilot_platform")
        sup.close()

        from app import pg_backend

        owner = connect("paypilot_owner", "pp")
        # Apply statement by statement and report EVERY failure, not only the first, so one real run
        # shows the whole list instead of one error per run.
        errors = []
        statements = pg_backend.split_statements(pg_backend.SCHEMA_PATH.read_text(encoding="utf-8"))
        for st in statements:
            try:
                owner.run(st)
            except Exception as exc:  # noqa: BLE001
                arg = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
                errors.append((" ".join(st.split())[:90], arg.get("C"), str(arg.get("M", exc))[:140]))
        if errors:
            for head, code, msg in errors:
                print(f"FAIL  schema statement {code}: {msg}\n      in: {head}")
            check("schema applies", False, f"{len(errors)} of {len(statements)} statements failed")
            print("\nStopping: nothing after the schema can be checked until it applies.")
            return 1
        n = len(statements)
        check("schema applies", n > 20, f"{n} statements")
        try:
            pg_backend.apply_schema(owner)
            check("schema re-applies cleanly (idempotent)", True)
        except Exception as exc:  # noqa: BLE001
            check("schema re-applies cleanly (idempotent)", False, str(exc)[:200])
        owner.close()
        sup = connect("super", "pp")
        sup.run("REVOKE CREATE ON SCHEMA public FROM paypilot_platform")
        sup.close()

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
        unprotected = [r[0] for r in rls.run(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND has_table_privilege('paypilot_tenant', c.oid, 'SELECT') "
            "AND NOT (c.relrowsecurity AND c.relforcerowsecurity)")]
        check("every table the tenant role can read has forced RLS", not unprotected, str(unprotected))
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

        # ---- M8: copy a real SQLite ledger into a second, empty database and prove the copy ----
        sys.path.insert(0, str(ROOT / "scripts"))
        import pg_copy_ledger as m8

        from app.store import Database
        from app.store import Platform as SqlitePlatform

        sup = connect("super")
        sup.run("CREATE DATABASE pp2 OWNER paypilot_owner")
        sup.close()
        sup = connect("super", "pp2")
        sup.run("GRANT ALL ON SCHEMA public TO paypilot_owner")
        sup.run("GRANT USAGE ON SCHEMA public TO paypilot_tenant, paypilot_platform")
        sup.run("GRANT CREATE ON SCHEMA public TO paypilot_platform")
        sup.close()
        owner2 = connect("paypilot_owner", "pp2")
        pg_backend.apply_schema(owner2)
        owner2.close()
        sup = connect("super", "pp2")
        sup.run("REVOKE CREATE ON SCHEMA public FROM paypilot_platform")
        sup.close()

        lite = Database(work / "ledger.db", owner_tenant="default")
        lp = SqlitePlatform(lite)
        for t in ("alpha", "beta"):
            lp.register_tenant(t, t, t.upper())
        for t, n in (("alpha", 3), ("beta", 2)):
            st = Store(lite, tenant_id=t)
            for i in range(n):
                st.record_failure(invoice_id=f"in_{t}_{i}", customer_id="c", amount_minor=100 + i,
                                  currency="eur", failure_code="card_declined", attempt_count=1)
                st.record_message(invoice_id=f"in_{t}_{i}", status="sent", provider_message_id=f"p_{t}_{i}")
            st.create_approval(invoice_id=f"in_{t}_0", channel="email", recipient="r@x.test",
                               subject="it's a 'quoted' subject; with semicolon", body="b\nline",
                               token_hash="h", expires_at="2099-01-01T00:00:00")
            st.mark_early_paid(f"in_{t}_early")
        lite.conn.commit()
        import sqlite3 as _sq

        src = _sq.connect(work / "ledger.db")
        m8.check_source(src)
        target = connect("paypilot_platform", "pp2")
        try:
            counts, problems = m8.copy_and_verify(src, target)
            check("M8 copies every row", sum(counts.values()) > 15, str(counts))
            check("M8 copy is identical by count and content hash, every table", not problems, str(problems))
            try:
                m8.check_target_empty(target)
                check("M8 refuses a second copy into a non-empty target", False, "no refusal")
            except m8.CopyRefused:
                check("M8 refuses a second copy into a non-empty target", True)
        finally:
            target.close()
        db2 = pg_backend.PgDatabase(connect=lambda role: connect(role_user[role], "pp2"))
        nxt = Store(db2, tenant_id="alpha").record_message(invoice_id="in_alpha_0", status="queued")
        top = max(r[0] for r in src.execute("SELECT id FROM messages"))
        check("M8 sequence continues past the copied ids", nxt > top, f"next {nxt}, copied max {top}")
        check("M8 copied data is still isolated per tenant",
              Store(db2, tenant_id="beta").get_failure("in_alpha_0") is None
              and Store(db2, tenant_id="alpha").get_failure("in_alpha_0") is not None)
        db2.close()
        lite.close()

        # ---- M9: one login per tenant; the database reads the tenant from WHO is connected ----
        from app import pg_roles

        rkey = b"r" * 40

        def tenant_login(t, key=rkey):
            return connect(pg_roles.role_name(t), "pp", pg_roles.derive_password(t, key))

        admin = connect("super", "pp")
        for t in ("alpha", "beta"):
            pg_roles.provision(admin, t, rkey)
        members = admin.run("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                            "WHERE r.rolname IN ('pp_t_alpha', 'pp_t_beta')")[0][0]
        check("per-tenant roles are members of nothing (SET ROLE has nowhere to go)", members == 0, str(members))
        attrs = admin.run("SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname = 'pp_t_alpha'")
        check("a per-tenant role is not superuser, BYPASSRLS or CREATEROLE", attrs == [[False, False, False]], str(attrs))
        pw_stored = admin.run("SELECT rolpassword FROM pg_authid WHERE rolname = 'pp_t_alpha'")[0][0]
        check("the stored password is a SCRAM verifier, not cleartext",
              str(pw_stored).startswith("SCRAM-SHA-256$") and pg_roles.derive_password("alpha", rkey) not in str(pw_stored))

        a = tenant_login("alpha")
        check("pp_t_alpha logs in with the DERIVED password under scram-sha-256", True)
        try:
            tenant_login("alpha", b"w" * 40)
            check("a wrong key cannot log in", False, "connected")
        except Exception as exc:  # noqa: BLE001
            check("a wrong key cannot log in (28P01)", "28P01" in str(exc), str(exc)[:80])
        seen = [r[0] for r in a.run("SELECT invoice_id FROM failures")]
        check("pp_t_alpha with no setting sees exactly its own rows", seen == ["in_a"], str(seen))
        a.run("START TRANSACTION")
        a.run("SELECT set_config('paypilot.tenant_id', 'beta', true)")
        seen = [r[0] for r in a.run("SELECT invoice_id FROM failures")]
        a.run("COMMIT")
        check("pp_t_alpha setting the tenant to beta still sees only alpha (the shared-role hole is closed)",
              seen == ["in_a"], str(seen))
        for target in ("paypilot_tenant", "pp_t_beta", "paypilot_platform", "paypilot_owner"):
            try:
                a.run(f"SET ROLE {target}")
                a.run("RESET ROLE")
                check(f"pp_t_alpha cannot SET ROLE {target}", False, "SET ROLE succeeded")
            except Exception as exc:  # noqa: BLE001
                check(f"pp_t_alpha cannot SET ROLE {target}", "42501" in str(exc), str(exc)[:100])
        try:
            a.run("INSERT INTO failures (tenant_id, invoice_id, customer_id, amount_minor, currency, "
                  "failure_code, state, failed_at, updated_at) VALUES "
                  "('beta','in_y','c',1,'eur','x','s','n','n')")
            check("pp_t_alpha cannot write a row for beta", False, "insert succeeded")
        except Exception as exc:  # noqa: BLE001
            check("pp_t_alpha cannot write a row for beta", "42501" in str(exc), str(exc)[:100])
        for table in ("api_tokens", "tenants", "tenant_config", "tenancy_meta"):
            try:
                a.run(f"SELECT * FROM {table}")
                check(f"pp_t_alpha cannot read platform table {table}", False, "select succeeded")
            except Exception as exc:  # noqa: BLE001
                check(f"pp_t_alpha cannot read platform table {table}", "42501" in str(exc), str(exc)[:100])
        # CVE-2018-1058 shape: shadow a function pp_current_tenant() calls. Must be refused outright.
        try:
            a.run("CREATE FUNCTION public.substr(name, integer) RETURNS text LANGUAGE sql "
                  "AS $$SELECT 'beta'::text$$")
            check("pp_t_alpha cannot create a function in schema public", False, "CREATE FUNCTION succeeded")
        except Exception as exc:  # noqa: BLE001
            check("pp_t_alpha cannot create a function in schema public", "42501" in str(exc), str(exc)[:100])
        seen = [r[0] for r in a.run("SELECT invoice_id FROM failures")]
        check("the tenant function still answers alpha after that attempt", seen == ["in_a"], str(seen))
        # An ordinary role may change its OWN password. The attacker sets a known one; sync must repair it.
        a.run("ALTER ROLE CURRENT_USER PASSWORD 'known'")
        a.close()
        try:
            tenant_login("alpha")
            check("after an in-session password change the derived password no longer works", False, "still works")
        except Exception:  # noqa: BLE001
            check("after an in-session password change the derived password no longer works", True)
        pg_roles.provision(admin, "alpha", rkey)
        tenant_login("alpha").close()
        check("re-provisioning (sync) restores the derived password", True)
        try:
            connect("pp_t_alpha", "pp", "known")
            check("re-provisioning revokes the attacker's chosen password", False, "known password still works")
        except Exception as exc:  # noqa: BLE001
            check("re-provisioning revokes the attacker's chosen password (28P01)", "28P01" in str(exc), str(exc)[:80])

        db3 = pg_backend.PgDatabase(
            connect=lambda role, tenant=None: (
                connect("paypilot_platform", "pp") if tenant is None else tenant_login(tenant)),
            tenant_roles=True,
        )
        sa, sb = Store(db3, tenant_id="alpha"), Store(db3, tenant_id="beta")
        check("Store in roles mode: each tenant reads its own, not the other's",
              sa.get_failure("in_a") is not None and sa.get_failure("in_b") is None
              and sb.get_failure("in_b") is not None and sb.get_failure("in_a") is None)
        db3.close()

        pg_roles.deprovision(admin, "beta")
        try:
            pg_roles.deprovision(admin, "beta")
            check("deprovision is idempotent (a second sync does not abort)", True)
        except Exception as exc:  # noqa: BLE001
            check("deprovision is idempotent (a second sync does not abort)", False, str(exc)[:100])
        try:
            tenant_login("beta")
            check("a dropped tenant role can no longer log in", False, "connected")
        except Exception as exc:  # noqa: BLE001
            check("a dropped tenant role can no longer log in (28P01)", "28P01" in str(exc), str(exc)[:80])
        gone = admin.run("SELECT count(*) FROM pg_roles WHERE rolname = 'pp_t_beta'")[0][0]
        check("deprovision leaves no role behind", gone == 0, str(gone))
        pg_roles.provision(admin, "beta", rkey)
        tenant_login("beta").close()
        check("a tenant can be re-provisioned after being dropped", True)
        check("harden() reports the shared login unusable", pg_roles.harden(admin))
        check("harden() is idempotent", pg_roles.harden(admin))
        try:
            connect("paypilot_tenant", "pp")
            check("the shared paypilot_tenant login can no longer log in", False, "connected")
        except Exception as exc:  # noqa: BLE001
            check("the shared paypilot_tenant login can no longer log in",
                  "28000" in str(exc) or "28P01" in str(exc), str(exc)[:90])
        admin.close()
    finally:
        if started:
            subprocess.run([pgbin / "pg_ctl", "-D", str(data), "-m", "immediate", "stop"],
                           capture_output=True)
        shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - an unexpected error is "could not run", never a pass
        print(f"CANNOT RUN: unexpected {type(exc).__name__}: {str(exc)[:300]}")
        print("This is NOT a pass: the checks did not complete.")
        raise SystemExit(2) from None
