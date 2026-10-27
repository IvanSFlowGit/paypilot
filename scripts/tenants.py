#!/usr/bin/env python3
"""Tenant lifecycle for a multi-tenant PayPilot: create, suspend, token, secret, offboard.

Runs on the machine that holds the ledger, with the operator's access to the database file. That
is more authority than any token, so this script is the break-glass door: the ``/admin`` routes
call the same functions after ``authorize``. It never prints a secret it was given and prints a
token exactly once, at creation.

    python3 scripts/tenants.py bootstrap
    python3 scripts/tenants.py create alpha --name "Alpha Ltd" [--base-url https://paypilot.example]
    python3 scripts/tenants.py list
    python3 scripts/tenants.py show alpha
    python3 scripts/tenants.py suspend alpha --reason "invoice unpaid"
    python3 scripts/tenants.py resume alpha
    python3 scripts/tenants.py token create alpha tenant_approver [--label NAME]
    python3 scripts/tenants.py token revoke alpha TOKEN_ID
    python3 scripts/tenants.py token list alpha
    python3 scripts/tenants.py secret alpha stripe_webhook_secret     # value read from stdin or a prompt
    python3 scripts/tenants.py platform alpha --send on|off --recipient a@b.test [...] --from-domain acme.test
    python3 scripts/tenants.py doc alpha [--base-url URL]
    python3 scripts/tenants.py offboard alpha --yes-erase-everything --export-to FILE

Environment: PAYPILOT_DB_PATH (the ledger), PAYPILOT_TENANT_KEY (needed for secrets).
Exit: 0 done, 1 refused (reason printed), 2 could not run
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import authz, principals, tenant_admin, tenant_config  # noqa: E402
from app.store import (  # noqa: E402
    LegacySchemaError,
    get_database,
    get_platform,
    store_for,
)


def _refuse(code: str) -> int:
    print(f"REFUSED: {code}", file=sys.stderr)
    return 1


def _show_token(issued: principals.IssuedToken) -> None:
    print(f"token_id: {issued.token_id}")
    print(f"role: {issued.role}")
    print("token (shown ONCE, not recoverable):")
    print(issued.raw)


def cmd_bootstrap(args, platform, db) -> int:
    try:
        issued = principals.bootstrap(platform, label=args.label)
    except principals.GrantRefused as exc:
        return _refuse(exc.reason)
    _show_token(issued)
    return 0


def cmd_create(args, platform, db) -> int:
    try:
        out = tenant_admin.create_tenant(
            platform, db, args.tenant, args.name, slug=args.slug, base_url=args.base_url or ""
        )
    except tenant_admin.LifecycleError as exc:
        return _refuse(exc.code)
    print(f"tenant {args.tenant} created, status active, sending OFF")
    for name, url in out["webhooks"].items():
        print(f"webhook {name}: {url}")
    print(f"first admin token_id: {out['admin_token_id']}")
    print("first admin token (shown ONCE, not recoverable):")
    print(out["admin_token"])
    print("Next: store the signing secret with `secret`, then set the send switch with `platform`.")
    return 0


def cmd_list(args, platform, db) -> int:
    for t in platform.list_tenants():
        print(f"{t['id']}\t{t['status']}\t{t['name']}")
    return 0


def cmd_show(args, platform, db) -> int:
    try:
        print(tenant_admin.render_tenant_doc(platform, db, args.tenant, args.base_url or ""))
    except tenant_admin.LifecycleError as exc:
        return _refuse(exc.code)
    return 0


def cmd_doc(args, platform, db) -> int:
    return cmd_show(args, platform, db)


def cmd_suspend(args, platform, db) -> int:
    try:
        out = tenant_admin.suspend(platform, store_for(args.tenant), args.reason)
    except tenant_admin.LifecycleError as exc:
        return _refuse(exc.code)
    print(f"suspended; {out['approvals_expired']} pending approvals expired")
    return 0


def cmd_resume(args, platform, db) -> int:
    try:
        tenant_admin.resume(platform, args.tenant)
    except tenant_admin.LifecycleError as exc:
        return _refuse(exc.code)
    print("resumed")
    return 0


def cmd_token(args, platform, db) -> int:
    if args.action == "list":
        for t in platform.list_tokens(args.tenant):
            state = "revoked" if t["revoked_at"] else "live"
            print(f"{t['token_id']}\t{t['role']}\t{state}\t{t['label'] or ''}")
        return 0
    if args.action == "create":
        if args.role not in authz.ROLES:
            return _refuse("unknown_role")
        try:
            issued = principals.issue_for_operator(
                platform, role=args.role,
                tenant_id=None if args.role == authz.PLATFORM_ADMIN else args.tenant,
                label=args.label,
            )
        except principals.GrantRefused as exc:
            return _refuse(exc.reason)
        _show_token(issued)
        return 0
    operator = authz.Principal(role=authz.PLATFORM_ADMIN, tenant_id=None, token_id="operator")
    if not principals.revoke(platform, operator, args.token_id, "revoked by operator"):
        return _refuse("not_found")
    print("revoked")
    return 0


def cmd_secret(args, platform, db) -> int:
    value = sys.stdin.read().strip() if not sys.stdin.isatty() else getpass.getpass("value: ")
    if not value:
        return _refuse("empty_value")
    try:
        done = tenant_admin.rotate_secrets(db, args.tenant, {args.name: value})
    except tenant_admin.LifecycleError as exc:
        return _refuse(exc.code)
    print(f"rotated: {', '.join(done)} (value not shown)")
    return 0


def cmd_platform(args, platform, db) -> int:
    try:
        tenant_admin.set_platform_fields(
            db, args.tenant, send_enabled=args.send == "on",
            allowed_recipients=args.recipient or [], allowed_from_domains=args.from_domain,
        )
    except tenant_config.TenantConfigError as exc:
        return _refuse(f"invalid_config: {exc}")
    print(f"send {args.send}; recipients {args.recipient or []}; from-domains {args.from_domain}")
    return 0


def cmd_offboard(args, platform, db) -> int:
    if not args.yes_erase_everything:
        return _refuse("confirmation_required: pass --yes-erase-everything")
    if not args.export_to:
        return _refuse("export_required: pass --export-to FILE so the tenant gets what it is owed")
    try:
        out = tenant_admin.offboard(platform, store_for(args.tenant), confirm=True)
    except tenant_admin.LifecycleError as exc:
        return _refuse(exc.code)
    Path(args.export_to).write_text(json.dumps(out.pop("export"), indent=2, default=str), encoding="utf-8")
    os.chmod(args.export_to, 0o600)
    print("offboarded. per-table rows erased:")
    for table, n in out["erased"].items():
        print(f"  {table}: {n}")
    print(f"tokens revoked: {out['tokens_revoked']}; export written to {args.export_to}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args):
        parser = sub.add_parser(name)
        parser.set_defaults(fn=fn)
        for spec in args:
            names, kwargs = spec if isinstance(spec, tuple) else ((spec,), {})
            parser.add_argument(*names, **kwargs)
        return parser

    add("bootstrap", cmd_bootstrap, (("--label",), {}))
    add("create", cmd_create, "tenant", (("--name",), {"required": True}),
        (("--slug",), {}), (("--base-url",), {}))
    add("list", cmd_list)
    add("show", cmd_show, "tenant", (("--base-url",), {}))
    add("doc", cmd_doc, "tenant", (("--base-url",), {}))
    add("suspend", cmd_suspend, "tenant", (("--reason",), {"required": True}))
    add("resume", cmd_resume, "tenant")
    add("token", cmd_token, (("action",), {"choices": ("create", "revoke", "list")}), "tenant",
        (("role_or_id",), {"nargs": "?"}), (("--label",), {}))
    add("secret", cmd_secret, "tenant", "name")
    add("platform", cmd_platform, "tenant",
        (("--send",), {"choices": ("on", "off"), "required": True}),
        (("--recipient",), {"action": "append"}), (("--from-domain",), {"action": "append"}))
    add("offboard", cmd_offboard, "tenant",
        (("--yes-erase-everything",), {"action": "store_true"}), (("--export-to",), {}))
    return ap


def main(argv: list[str] | None = None) -> int:
    # Set here, not at import: this script only makes sense against a multi ledger, and an import
    # (a test, a REPL) must not flip the whole process into multi mode.
    os.environ["PAYPILOT_TENANCY"] = "multi"
    args = build_parser().parse_args(argv)
    if args.cmd == "token":
        args.role = args.role_or_id if args.action == "create" else None
        args.token_id = args.role_or_id if args.action == "revoke" else None
        if args.action in ("create", "revoke") and not args.role_or_id:
            return _refuse("missing_argument")
    try:
        db, platform = get_database(), get_platform()
    except LegacySchemaError as exc:
        print(f"CANNOT RUN: {type(exc).__name__}: ledger not migrated", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - could-not-run is its own answer
        print(f"CANNOT RUN: {type(exc).__name__}", file=sys.stderr)
        return 2
    return args.fn(args, platform, db)


if __name__ == "__main__":
    sys.exit(main())
