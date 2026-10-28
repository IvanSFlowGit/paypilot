#!/usr/bin/env python3
"""M9: create, refresh and drop the per-tenant database logins (pp_t_<tenant>).

    python scripts/pg_roles.py provision TENANT
    python scripts/pg_roles.py drop TENANT
    python scripts/pg_roles.py sync          # every tenant in the ledger: provision live ones, drop gone ones,
                                             # then make the shared paypilot_tenant login unusable
    python scripts/pg_roles.py harden        # only that last step

Needs an ADMIN connection that can CREATE ROLE, which the application must never hold:
PAYPILOT_PG_ADMIN_USER / PAYPILOT_PG_ADMIN_PASSWORD plus PGHOST, PGDATABASE, PGPORT, PGSSLROOTCERT, and
PAYPILOT_PG_ROLE_KEY (>= 32 chars) from which passwords are derived. Nothing prints a password.
Exit 0 done, 1 refused, 2 could not run. Run it after creating or offboarding a tenant in roles mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("provision", "drop", "sync", "harden"):
        print(__doc__)
        return 1
    cmd = args[0]
    from app import decision_audit, pg_backend, pg_roles
    from app.store import GONE_STATUSES, Platform

    try:
        admin = decision_audit.open_connection(pg_backend._role_env("admin"))  # noqa: SLF001
        key = pg_roles.role_key()
    except Exception as exc:  # noqa: BLE001
        print(f"CANNOT RUN: {type(exc).__name__}: {str(exc)[:200]}")
        return 2
    try:
        if cmd == "harden":
            ok = pg_roles.harden(admin)
            print("shared login paypilot_tenant: " + ("cannot log in" if ok else "STILL ABLE TO LOG IN"))
            return 0 if ok else 2
        if cmd in ("provision", "drop"):
            if len(args) != 2:
                print("REFUSED: name exactly one tenant")
                return 1
            tenant = args[1]
            name = (pg_roles.provision(admin, tenant, key) if cmd == "provision"
                    else pg_roles.deprovision(admin, tenant))
            print(f"{cmd}: {name}")
            return 0
        db = pg_backend.PgDatabase(tenant_roles=False)
        try:
            tenants = Platform(db).list_tenants()
        finally:
            db.close()
        done, failed = 0, []
        for t in tenants:
            tid = t["id"]
            try:
                if t["status"] in GONE_STATUSES:
                    pg_roles.deprovision(admin, tid)
                    print(f"dropped     {tid}")
                else:
                    pg_roles.provision(admin, tid, key)
                    print(f"provisioned {tid}")
                done += 1
            except pg_roles.RoleError as exc:
                failed.append(tid)
                print(f"REFUSED {tid}: {exc}")
            except Exception as exc:  # noqa: BLE001 - one bad tenant must not leave the rest un-dropped
                code = exc.args[0].get("C") if exc.args and isinstance(exc.args[0], dict) else None
                failed.append(tid)
                print(f"FAILED  {tid}: {type(exc).__name__} sqlstate={code}")
        hardened = pg_roles.harden(admin)
        print("shared login paypilot_tenant: " + ("cannot log in" if hardened else "STILL ABLE TO LOG IN"))
        print(f"\nsync: {done} done, {len(failed)} failed" + (f" ({', '.join(failed)})" if failed else ""))
        return 2 if failed or not hardened else 0
    except pg_roles.RoleError as exc:
        print(f"REFUSED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - type only: a server message could quote a password
        code = exc.args[0].get("C") if exc.args and isinstance(exc.args[0], dict) else None
        print(f"CANNOT RUN: {type(exc).__name__} sqlstate={code}")
        return 2
    finally:
        try:
            admin.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
