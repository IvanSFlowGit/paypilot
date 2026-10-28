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

import os
import re
from typing import Any

from app import principals, tenant_config
from app.audit import audit_security_event
from app.store import GONE_STATUSES, Database, Platform, Store

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
    from app import pg_backend, pg_roles

    if pg_backend.backend_is_postgres() and pg_roles.roles_enabled():
        try:
            pg_roles.role_name(tenant_id)  # the id must fit a database role name (63 bytes), not be truncated
        except pg_roles.RoleError:
            raise LifecycleError("tenant_id_too_long_for_database_role") from None
    slug = (slug or tenant_id.replace("_", "-")).lower()
    if not _SLUG_RE.match(slug):
        raise LifecycleError("invalid_slug")
    if platform.tenant_row(tenant_id) is not None:
        raise LifecycleError("tenant_exists")
    if tenant_config.has_stored_config(db, tenant_id):
        # A configuration row with no tenant is somebody pre-seeding settings (sending on, a
        # wildcard allowlist). Creating over it would adopt them. Refuse and make a human look.
        raise LifecycleError("config_exists_without_tenant")
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
    if row is None or row["status"] in GONE_STATUSES:
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
    if row is None or row["status"] in GONE_STATUSES:
        raise LifecycleError("not_found")
    if row["status"] != "suspended":
        raise LifecycleError("not_suspended")
    platform.set_tenant_status(tenant_id, "active", None)
    audit_security_event(
        event="tenant_resumed", detail=f"tenant {tenant_id} resumed", severity="warning"
    )
    return {"status": "active"}


def require_live(platform: Platform, tenant_id: str) -> dict:
    """The tenant's row, or LifecycleError ``not_found`` if it is unknown or offboarded.

    Every operation that WRITES for a tenant goes through this first, so a typo cannot create a
    configuration row for a tenant that does not exist yet, and an offboarded tenant's
    configuration cannot be brought back.
    """
    row = platform.tenant_row(tenant_id)
    if row is None or row["status"] in GONE_STATUSES:
        raise LifecycleError("not_found")
    return row


def erase_client_rows(client_id: str, decision_audit=None) -> dict[str, Any]:
    """Erase the stores keyed by client id, which in multi mode IS the tenant id.

    The decision audit holds the full request JSON of every ``/decide`` call, and bank consents
    hold the client's consent records (including access tokens). Both survive a ledger-only erase,
    so offboarding clears them too. A failure RAISES, which leaves the tenant ``offboarding`` so
    the operator can run the erase again; every step is idempotent. Consents are deleted without
    the encryption key.
    """
    from pathlib import Path

    from app.bank.consent import CONSENT_DB_ENV, erase_client_at
    from app.decision_audit import SqliteDecisionAudit

    out: dict[str, Any] = {}
    audit = decision_audit if decision_audit is not None else SqliteDecisionAudit()
    out.update(audit.erase_client(client_id))
    consent_path = Path(os.getenv(CONSENT_DB_ENV) or "consents.db")
    if consent_path.is_file():
        out["consents"] = erase_client_at(consent_path, client_id)
    else:
        out["consents"] = f"none: no consent database at {consent_path}"
    return out


def export_everything(store: Store, decision_audit=None) -> dict[str, Any]:
    """The ledger plus the decision history, for the offboarding export.

    Bank consents are erased but NOT exported: they hold access tokens, which are not the client's
    data to receive.
    """
    from app.decision_audit import SqliteDecisionAudit

    audit = decision_audit if decision_audit is not None else SqliteDecisionAudit()
    out = store.export_tenant()
    out.update(audit.export_client(store.tenant_id))
    out["not_exported"] = "bank consents (they hold access tokens) are erased, not exported"
    return out


def begin_offboarding(platform: Platform, store: Store) -> dict[str, Any]:
    """Take the tenant out of service. Nothing is erased yet.

    From here tokens do not resolve, webhooks answer uniformly, no mail leaves and pending approvals
    are closed, so the export taken next describes a ledger that can no longer change. Safe to call
    again on a tenant that is already ``offboarding`` (a retry).
    """
    row = platform.tenant_row(store.tenant_id)
    if row is None or row["status"] == "offboarded":
        raise LifecycleError("not_found")
    if row["status"] != "offboarding":
        platform.set_tenant_status(store.tenant_id, "offboarding", "offboarding")
    revoked = platform.revoke_tenant_tokens(store.tenant_id, "tenant offboarding")
    expired = store.expire_all_pending_approvals(by="offboarding")
    audit_security_event(
        event="tenant_offboarding_started",
        detail=f"tenant {store.tenant_id}: out of service, {len(expired)} approvals closed",
        severity="warning",
    )
    return {"status": "offboarding", "tokens_revoked": revoked}


def finish_offboarding(
    platform: Platform, store: Store, *, confirm: bool, decision_audit=None
) -> dict[str, Any]:
    """Erase everything the tenant owns, then mark it ``offboarded`` LAST.

    Irreversible, so it needs ``confirm=True``, and the tenant must already be ``offboarding``. Any
    failure leaves it ``offboarding`` and this function can be run again. The tenant row stays, so
    the id cannot be reused and a webhook for it keeps failing uniformly.
    """
    if not confirm:
        raise LifecycleError("confirmation_required")
    row = platform.tenant_row(store.tenant_id)
    if row is None or row["status"] == "offboarded":
        raise LifecycleError("not_found")
    if row["status"] != "offboarding":
        raise LifecycleError("start_required")
    erased = store.erase_tenant()
    config_rows = platform.delete_tenant_config(store.tenant_id)
    other = erase_client_rows(store.tenant_id, decision_audit)
    platform.set_tenant_status(store.tenant_id, "offboarded", "offboarded")
    audit_security_event(
        event="tenant_offboarded",
        detail=f"tenant {store.tenant_id} offboarded; erased {sum(erased.values())} rows",
        severity="warning",
    )
    return {
        "status": "offboarded",
        "erased": erased,
        "config_rows_deleted": config_rows,
        "other_stores": other,
    }


def offboard(
    platform: Platform, store: Store, *, confirm: bool, export: bool = True, decision_audit=None
) -> dict[str, Any]:
    """Begin, optionally export in memory, finish. The CLI and HTTP doors run the steps apart."""
    if not confirm:
        raise LifecycleError("confirmation_required")
    begun = begin_offboarding(platform, store)
    exported = export_everything(store, decision_audit) if export else None
    done = finish_offboarding(platform, store, confirm=True, decision_audit=decision_audit)
    return {**done, "tokens_revoked": begun["tokens_revoked"], "export": exported}


def update_config(db: Database, tenant_id: str, behaviour: dict[str, Any]) -> None:
    """Tenant-editable behaviour fields only (platform-only fields are refused downstream)."""
    require_live(Platform(db), tenant_id)
    try:
        tenant_config.save_tenant_config(db, tenant_id, behaviour, {})
    except tenant_config.TenantConfigError as exc:
        raise LifecycleError("invalid_config") from exc


def rotate_secrets(db: Database, tenant_id: str, secrets: dict[str, str]) -> list[str]:
    """Write-only secret rotation. Returns the NAMES rotated, never a value."""
    require_live(Platform(db), tenant_id)
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
    require_live(Platform(db), tenant_id)
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
