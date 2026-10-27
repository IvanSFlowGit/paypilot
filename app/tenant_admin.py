# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Tenant lifecycle: create, suspend, resume, offboard, and the per-tenant operations.

One implementation, two doors. ``scripts/tenants.py`` calls these functions with the operator's
database access; the ``/admin`` routes call the same functions after :func:`app.authz.authorize`.
Neither door contains its own copy of the rules.

Nothing here sends anything. Suspension and offboarding only ever REMOVE capability.
"""

from __future__ import annotations

import re
from typing import Any

from app import principals, tenant_config
from app.audit import audit_security_event
from app.store import Database, Platform, Store

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
WEBHOOK_PATHS = ("stripe", "recharge", "chargebee")


class LifecycleError(Exception):
    """A lifecycle operation was refused. ``code`` is stable and safe to return to a caller."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def webhook_urls(tenant_id: str, base_url: str = "") -> dict[str, str]:
    base = base_url.rstrip("/")
    return {name: f"{base}/t/{tenant_id}/webhooks/{name}" for name in WEBHOOK_PATHS}


def create_tenant(
    platform: Platform, db: Database, tenant_id: str, name: str, *, slug: str | None = None,
    base_url: str = "",
) -> dict[str, Any]:
    """Register a tenant, give it an empty configuration, mint its first admin token.

    The token is in the result ONCE; it cannot be recovered. A new tenant sends nothing: the send
    switch, the recipient allowlist and the sender domains are all empty until the platform
    operator sets them, and its webhook refuses until a signing secret is stored.
    """
    if not isinstance(name, str) or not name.strip():
        raise LifecycleError("name_required")
    slug = (slug or tenant_id.replace("_", "-")).lower()
    if not _SLUG_RE.match(slug):
        raise LifecycleError("invalid_slug")
    if platform.tenant_row(tenant_id) is not None:
        raise LifecycleError("tenant_exists")
    try:
        platform.register_tenant(tenant_id, slug, name.strip())
    except ValueError as exc:  # invalid id
        raise LifecycleError("invalid_tenant_id") from exc
    except Exception as exc:  # noqa: BLE001 - a UNIQUE clash on slug
        raise LifecycleError("slug_taken") from exc
    tenant_config.save_tenant_config(db, tenant_id, {}, {})
    token = principals.issue_for_operator(
        platform, role="tenant_admin", tenant_id=tenant_id, label="first admin"
    )
    audit_security_event(
        event="tenant_created", detail=f"tenant {tenant_id} created", severity="info"
    )
    return {
        "tenant": platform.tenant_row(tenant_id),
        "webhooks": webhook_urls(tenant_id, base_url),
        "admin_token": token.raw,
        "admin_token_id": token.token_id,
    }


def suspend(platform: Platform, store: Store, reason: str) -> dict[str, Any]:
    """Suspend a tenant: pending approvals expire now, sends and decisions are refused.

    Reason is required so the row says why. Closing events (paid, deleted) are still applied by
    the webhook, so the ledger stays true and no paid customer is dunned.
    """
    reason = (reason or "").strip()
    if not reason:
        raise LifecycleError("reason_required")
    row = platform.tenant_row(store.tenant_id)
    if row is None or row["status"] == "offboarded":
        raise LifecycleError("not_found")
    if row["status"] == "suspended":
        raise LifecycleError("already_suspended")
    platform.set_tenant_status(store.tenant_id, "suspended", reason)
    expired = store.expire_all_pending_approvals(by="suspension")
    audit_security_event(
        event="tenant_suspended",
        detail=f"tenant {store.tenant_id} suspended; {len(expired)} pending approvals expired",
        severity="warning",
    )
    return {"status": "suspended", "approvals_expired": len(expired)}


def resume(platform: Platform, tenant_id: str) -> dict[str, Any]:
    row = platform.tenant_row(tenant_id)
    if row is None or row["status"] == "offboarded":
        raise LifecycleError("not_found")
    if row["status"] != "suspended":
        raise LifecycleError("not_suspended")
    platform.set_tenant_status(tenant_id, "active", None)
    audit_security_event(
        event="tenant_resumed", detail=f"tenant {tenant_id} resumed", severity="warning"
    )
    return {"status": "active"}


def offboard(
    platform: Platform, store: Store, *, confirm: bool, export: bool = True
) -> dict[str, Any]:
    """Export what the tenant is owed, then erase its ledger rows and revoke its tokens.

    Irreversible, so it needs ``confirm=True`` and prints per-table counts. The tenant row stays,
    status ``offboarded``, so the id cannot be reused and a webhook for it keeps failing uniformly.
    """
    if not confirm:
        raise LifecycleError("confirmation_required")
    row = platform.tenant_row(store.tenant_id)
    if row is None or row["status"] == "offboarded":
        raise LifecycleError("not_found")
    exported = store.export_tenant() if export else None
    platform.set_tenant_status(store.tenant_id, "offboarded", "offboarded")
    revoked = platform.revoke_tenant_tokens(store.tenant_id, "tenant offboarded")
    erased = store.erase_tenant()
    config_rows = platform.delete_tenant_config(store.tenant_id)
    audit_security_event(
        event="tenant_offboarded",
        detail=f"tenant {store.tenant_id} offboarded; erased {sum(erased.values())} rows",
        severity="warning",
    )
    return {
        "status": "offboarded",
        "erased": erased,
        "config_rows_deleted": config_rows,
        "tokens_revoked": revoked,
        "export": exported,
    }


def update_config(db: Database, tenant_id: str, behaviour: dict[str, Any]) -> None:
    """Tenant-editable behaviour fields only (platform-only fields are refused downstream)."""
    try:
        tenant_config.save_tenant_config(db, tenant_id, behaviour, {})
    except tenant_config.TenantConfigError as exc:
        raise LifecycleError("invalid_config") from exc


def rotate_secrets(db: Database, tenant_id: str, secrets: dict[str, str]) -> list[str]:
    """Write-only secret rotation. Returns the NAMES rotated, never a value."""
    unknown = set(secrets) - set(tenant_config.SECRET_FIELDS)
    if unknown or not secrets:
        raise LifecycleError("invalid_secret_field")
    try:
        tenant_config.save_tenant_config(db, tenant_id, {}, secrets)
    except tenant_config.TenantConfigError as exc:
        raise LifecycleError("secret_store_unavailable") from exc
    audit_security_event(
        event="tenant_secrets_rotated",
        detail=f"tenant {tenant_id} rotated {sorted(secrets)}",
        severity="info",
    )
    return sorted(secrets)


def set_platform_fields(
    db: Database, tenant_id: str, *, send_enabled: bool, allowed_recipients: list[str],
    allowed_from_domains: list[str] | None = None,
) -> None:
    """PLATFORM OPERATOR ONLY. The send switch, recipient allowlist and sender domains."""
    tenant_config.set_platform_fields(
        db, tenant_id, send_enabled=send_enabled, allowed_recipients=allowed_recipients,
        allowed_from_domains=allowed_from_domains,
    )
    audit_security_event(
        event="tenant_platform_fields_changed",
        detail=f"tenant {tenant_id}: send_enabled={bool(send_enabled)}",
        severity="warning",
    )


def render_tenant_doc(platform: Platform, db: Database, tenant_id: str, base_url: str = "") -> str:
    """A per-tenant operations sheet, generated from live state. Names secrets, never values.

    Regenerate it after any change; it is derived, so it cannot drift from the tenant it describes.
    """
    row = platform.tenant_row(tenant_id)
    if row is None:
        raise LifecycleError("not_found")
    cfg = tenant_config.config_for(tenant_id, db)
    secrets_set = {
        name: bool(getattr(cfg, name, "")) for name in sorted(tenant_config.SECRET_FIELDS)
    }
    tokens = platform.list_tokens(tenant_id)
    live = [t for t in tokens if not t["revoked_at"]]
    lines = [
        f"# Tenant {tenant_id}",
        "",
        f"* Name: {row['name']}",
        f"* Status: {row['status']}" + (f" ({row['status_reason']})" if row["status_reason"] else ""),
        f"* Created: {row['created_at']}",
        "",
        "## What it can do right now",
        "",
        f"* Sending: {'ON' if cfg.send_enabled else 'OFF (every message is a dry run)'}",
        f"* Approval mode: {cfg.approval_mode}",
        f"* Recipient allowlist: {', '.join(cfg.allowed_recipients) or 'empty (nobody can be mailed)'}",
        f"* Sender domains: {', '.join(cfg.allowed_from_domains) or 'none (the platform sender is used)'}",
        f"* Approvers: {', '.join(cfg.approver_emails) or 'none'}",
        "",
        "## Webhook URLs",
        "",
    ]
    for name, url in webhook_urls(tenant_id, base_url).items():
        key = {"stripe": "stripe_webhook_secret", "recharge": "recharge_client_secret",
               "chargebee": "chargebee_webhook_secret"}[name]
        state = "secret stored" if secrets_set.get(key) else "NO SECRET: deliveries are refused"
        lines.append(f"* {name}: `{url}` ({state})")
    lines += ["", "## Secrets (names only)", ""]
    lines += [f"* {n}: {'set' if v else 'not set'}" for n, v in secrets_set.items()]
    lines += ["", f"## Tokens ({len(live)} live of {len(tokens)})", ""]
    for t in tokens:
        state = "revoked" if t["revoked_at"] else "live"
        lines.append(f"* {t['token_id']} {t['role']} {state} (created {t['created_at']}, "
                     f"last used {t['last_used_at'] or 'never'})")
    lines += [
        "",
        "## Operating notes",
        "",
        "* Suspending expires pending approvals, drops dunning events and keeps applying closing ones.",
        "* Offboarding exports, then erases the ledger rows and revokes every token. It cannot be undone.",
        "* Secrets are write-only: rotate with `scripts/tenants.py secret`, never read back.",
        "",
    ]
    return "\n".join(lines)
