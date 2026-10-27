# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Per-tenant configuration: behaviour, and secrets encrypted at rest.

ONE BUILDER. :func:`config_for` is the only way a tenant's settings reach code.
Every value is parsed ONCE here into a typed field; no call site reads a raw
environment string for a tenant-level setting. :func:`config_of` takes the
tenant from a tenant-bound store, so a caller that holds a store holds the right
configuration by construction.

TWO SOURCES, AND THEY DO NOT MIX.

* ``single`` mode, the tenant named by ``PAYPILOT_TENANT`` (default ``default``):
  if a ``tenant_config`` row exists it is used; otherwise the configuration is read
  from the environment exactly as the deployment has always set it.
* ``multi`` mode: the row is the only source. A tenant with no row is an
  :class:`UnknownTenant`. There is no environment fallback and no default.

SECRETS are encrypted with ``PAYPILOT_TENANT_KEY`` (Fernet, as ``app/bank/consent.py``).
No key, no store. They are decrypted at the point of use and never shown: the
dataclass hides them from ``repr`` and ``str``.

PLATFORM-LEVEL, NOT HERE: deployment storage paths, the public approval base URL,
the model provider key and model names, the model price table, the rate limiter,
the demo and test flags, the platform bootstrap token, and observability keys.
:data:`TENANT_ENV_FIELDS` is the closed list of environment names that ARE
tenant-level. The guard tests and the inventory both read it from here.
"""

from __future__ import annotations

import json
import math
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken

from app.safety import DEFAULT_UPDATE_URL

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.store import Database

DEFAULT_TENANT = "default"
DEMO_TENANT = "demo"
TENANT_KEY_ENV = "PAYPILOT_TENANT_KEY"
REDACTED = "***"

#: Defaults. Owned here; the modules that used to hold them import them from here.
DEFAULT_TTL_HOURS = 48
DEFAULT_GRACE_MINUTES = 15
DEFAULT_HOLDOUT_SEED = "paypilot-holdout-v1"
DEFAULT_MAX_TOUCHES = 3
DEFAULT_COOLDOWN_HOURS = 24.0
APPROVAL_MODES: tuple[str, ...] = ("off", "email", "gmail", "imap")
DEFAULT_APPROVAL_MODE = "email"
DEFAULT_FROM = "PayPilot <billing@streamflow.solutions>"
DEFAULT_IMAP_SECURITY = "ssl"
DEFAULT_IMAP_DRAFTS = "Drafts"
DEFAULT_IMAP_SENT = "Sent"

_TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class TenantConfigError(RuntimeError):
    """The configuration could not be built. Never raised to mean 'use a default'."""


class UnknownTenant(TenantConfigError):
    """No configuration exists for this tenant, and none may be assumed."""


class TenantKeyMissing(TenantConfigError):
    """``PAYPILOT_TENANT_KEY`` is unset or malformed. Secrets are refused."""


class TenantSecretError(TenantConfigError):
    """An encrypted secret would not decrypt. The message carries no key material."""


#: Secret fields. Encrypted at rest; never printed, never returned by a route.
SECRET_FIELDS: tuple[str, ...] = (
    "stripe_api_key",
    "stripe_webhook_secret",
    "recharge_client_secret",
    "chargebee_webhook_secret",
    "resend_api_key",
    "pii_salt",
    "gmail_client_id",
    "gmail_client_secret",
    "gmail_refresh_token",
    "imap_password",
)

#: THE closed list of tenant-level environment names, each mapped to its TenantConfig
#: field. Everything else in app/ is platform-level. Read by the guard tests and by the
#: inventory generator, so the list is written once.
TENANT_ENV_FIELDS: dict[str, str] = {
    "PAYPILOT_APPROVAL_MODE": "approval_mode",
    "PAYPILOT_APPROVER_EMAILS": "approver_emails",
    "PAYPILOT_APPROVAL_TTL_HOURS": "approval_ttl_hours",
    "PAYPILOT_SEND_EMAIL": "send_enabled",
    "PAYPILOT_ALLOWED_RECIPIENTS": "allowed_recipients",
    "PAYPILOT_FROM_EMAIL": "from_address",
    "PAYPILOT_BUSINESS_NAME": "business_name",
    "PAYPILOT_UPDATE_URL": "update_url",
    "PAYPILOT_PORTAL_RETURN_URL": "portal_return_url",
    "PAYPILOT_ALLOWED_LINK_HOSTS": "allowed_link_hosts",
    "PAYPILOT_HOLDOUT_PCT": "holdout_pct",
    "PAYPILOT_HOLDOUT_SEED": "holdout_seed",
    "PAYPILOT_AI_DISCLOSURE": "ai_disclosure",
    "PAYPILOT_MAX_TOUCHES": "max_touches",
    "PAYPILOT_SEND_COOLDOWN_HOURS": "send_cooldown_hours",
    "PAYPILOT_HIGH_VALUE_ANNUAL": "high_value_annual",
    "PAYPILOT_LLM_DRAFT": "llm_draft",
    "PAYPILOT_LLM_BUDGET_USD": "llm_budget_usd",
    "PAYPILOT_GMAIL_GRACE_MINUTES": "gmail_grace_minutes",
    "PAYPILOT_IMAP_HOST": "imap_host",
    "PAYPILOT_IMAP_PORT": "imap_port",
    "PAYPILOT_IMAP_USER": "imap_user",
    "PAYPILOT_IMAP_SECURITY": "imap_security",
    "PAYPILOT_IMAP_DRAFTS_FOLDER": "imap_drafts_folder",
    "PAYPILOT_IMAP_SENT_FOLDER": "imap_sent_folder",
    "STRIPE_API_KEY": "stripe_api_key",
    "STRIPE_SECRET_KEY": "stripe_api_key",
    "STRIPE_WEBHOOK_SECRET": "stripe_webhook_secret",
    "RECHARGE_CLIENT_SECRET": "recharge_client_secret",
    "CHARGEBEE_WEBHOOK_SECRET": "chargebee_webhook_secret",
    "RESEND_API_KEY": "resend_api_key",
    "PAYPILOT_PII_SALT": "pii_salt",
    "PAYPILOT_GOOGLE_CLIENT_ID": "gmail_client_id",
    "PAYPILOT_GOOGLE_CLIENT_SECRET": "gmail_client_secret",
    "PAYPILOT_GMAIL_REFRESH_TOKEN": "gmail_refresh_token",
    "PAYPILOT_IMAP_PASSWORD": "imap_password",
}


def _check_tenant_id(tenant_id: object) -> str:
    if not isinstance(tenant_id, str) or not _TENANT_ID_RE.match(tenant_id):
        raise UnknownTenant(f"invalid tenant id {tenant_id!r}")
    return tenant_id


def _csv(raw: str | None) -> tuple[str, ...]:
    return tuple(p.strip().lower() for p in (raw or "").split(",") if p.strip())


def _emails(raw: str | None) -> tuple[str, ...]:
    """Same rule as the approver list always was: lowercase, must contain '@', de-duplicated."""
    seen: list[str] = []
    for part in (raw or "").split(","):
        addr = part.strip().lower()
        if addr and "@" in addr and addr not in seen:
            seen.append(addr)
    return tuple(seen)


def _switch_on(raw: str | None) -> bool:
    """The exact rule a live-sending switch has always used.

    Case-sensitive after stripping: ``True``, ``TRUE`` and ``Yes`` are NOT on. Loosening
    this would turn a dry-run deployment into a live one, so it must not change.
    """
    return (raw or "").strip() in ("1", "true", "yes")


def _budget(raw: str | None) -> float | None:
    """The LLM ceiling in USD. Unset or blank means no ceiling configured.

    Invalid or negative values become a ceiling of ZERO, which refuses any spend. They must
    not become "no ceiling", because that would silently remove the limit.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def _float_or(raw: str | None, default: float, minimum: float | None = None) -> float:
    try:
        value = float(raw) if raw not in (None, "") else default
    except (ValueError, OverflowError):
        return default
    return max(minimum, value) if minimum is not None else value


def _int_or(raw: str | None, default: int, minimum: int | None = None) -> int:
    try:
        value = int(raw) if raw not in (None, "") else default
    except (ValueError, OverflowError):
        return default
    return max(minimum, value) if minimum is not None else value


def _approval_mode(raw: str | None) -> str:
    """Unknown or empty resolves to email: a typo must not turn unattended sending on."""
    value = (raw or "").strip().lower()
    return value if value in APPROVAL_MODES else DEFAULT_APPROVAL_MODE


def _holdout(raw: str | None) -> int:
    """Clamped to 0-100, never rejected. Non-numeric or infinite falls to 0 (no withholding)."""
    try:
        value = int(float(raw or "0"))
    except (ValueError, OverflowError):
        return 0
    return max(0, min(100, value))


def _high_value(raw: str | None) -> float | None:
    if not (raw or "").strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _optional_float(raw: str | None) -> float | None:
    if not (raw or "").strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _port(raw: str | None) -> int | None:
    """The exact rule main has always used: ASCII digits only. ``+993``, ``-1`` and ``5_0`` are
    not ports, and superscript digits (which ``isdigit()`` accepts) are refused."""
    value = (raw or "").strip()
    return int(value) if value.isascii() and value.isdigit() else None


@dataclass(frozen=True)
class TenantConfig:
    """Everything one tenant's code paths may read. Frozen; secrets hidden."""

    tenant_id: str
    # Behaviour: typed, parsed once.
    approval_mode: str = DEFAULT_APPROVAL_MODE
    approval_ttl_hours: int = DEFAULT_TTL_HOURS
    approver_emails: tuple[str, ...] = ()
    send_enabled: bool = False
    allowed_recipients: tuple[str, ...] = ()
    from_address: str = DEFAULT_FROM
    business_name: str = ""
    update_url: str = DEFAULT_UPDATE_URL
    portal_return_url: str = ""
    #: None means "not set" (the built-in Stripe defaults apply). An empty tuple means "set, and no
    #: host is allowed", which is what a list of only separators meant on main.
    allowed_link_hosts: tuple[str, ...] | None = None
    #: Platform-only: the domains a tenant may send FROM. A tenant's from_address outside these is
    #: replaced by the built-in default, so tenant A cannot send as tenant B's domain.
    allowed_from_domains: tuple[str, ...] = ()
    holdout_pct: int = 0
    holdout_seed: str = DEFAULT_HOLDOUT_SEED
    ai_disclosure: str = ""
    max_touches: int = DEFAULT_MAX_TOUCHES
    send_cooldown_hours: float = DEFAULT_COOLDOWN_HOURS
    high_value_annual: float | None = None
    llm_draft: bool = False
    llm_budget_usd: float | None = None
    gmail_grace_minutes: int = DEFAULT_GRACE_MINUTES
    imap_host: str = ""
    imap_port: int | None = None
    imap_user: str = ""
    imap_security: str = DEFAULT_IMAP_SECURITY
    imap_drafts_folder: str = DEFAULT_IMAP_DRAFTS
    imap_sent_folder: str = DEFAULT_IMAP_SENT
    # Secrets. Hidden from repr; decrypted only at the point of use.
    stripe_api_key: str = field(default="", repr=False)
    stripe_webhook_secret: str = field(default="", repr=False)
    recharge_client_secret: str = field(default="", repr=False)
    chargebee_webhook_secret: str = field(default="", repr=False)
    resend_api_key: str = field(default="", repr=False)
    pii_salt: str = field(default="", repr=False)
    gmail_client_id: str = field(default="", repr=False)
    gmail_client_secret: str = field(default="", repr=False)
    gmail_refresh_token: str = field(default="", repr=False)
    imap_password: str = field(default="", repr=False)

    def __repr__(self) -> str:
        shown = ", ".join(
            f"{name}={getattr(self, name)!r}"
            for name in (
                "tenant_id", "approval_mode", "approval_ttl_hours", "send_enabled",
                "from_address", "business_name", "update_url", "max_touches",
            )
        )
        secrets = ", ".join(f"{name}={REDACTED}" for name in SECRET_FIELDS)
        return f"TenantConfig({shown}, {secrets})"

    __str__ = __repr__

    def behaviour_view(self) -> TenantConfig:
        """The same configuration with every secret blank. The only form that may enter graph state."""
        return replace(self, **{name: "" for name in SECRET_FIELDS})


#: The full configuration of the recovery currently running, secrets included. It lives in a
#: context variable, never in graph state, so a tracer that serialises the state cannot see it.
_RUN_CONFIG: ContextVar[TenantConfig | None] = ContextVar("paypilot_run_config", default=None)


@contextmanager
def running_config(config: TenantConfig):
    """Hold the full config for the duration of one recovery run."""
    token = _RUN_CONFIG.set(config)
    try:
        yield
    finally:
        _RUN_CONFIG.reset(token)


def run_config(state_view: object = None) -> TenantConfig:
    """The config a node uses, never None.

    The full config of the running recovery if one is held; else the graph state's view; else the
    single-mode deployment config. In multi mode with no tenant bound the last step raises
    UnknownTenant, which is the correct outcome: a run with no tenant has no config to use.
    """
    held = _RUN_CONFIG.get()
    if held is not None:
        return held
    if state_view is not None:
        return state_view  # type: ignore[return-value]
    return current_config()


#: Fields where an empty value is itself the value. A set-but-empty PAYPILOT_UPDATE_URL is
#: empty on main, and the output guard then refuses every link. It is refused, not defaulted.
_BLANK_IS_VALUE = frozenset({"update_url"})


def _env_raw() -> dict[str, Any]:
    """Every tenant-level environment value, keyed by field, exactly as the environment spells it.

    Where two names feed one field (STRIPE_API_KEY, then STRIPE_SECRET_KEY), the first one that is
    set and non-empty wins, as it always has.
    """
    raw: dict[str, Any] = {}
    for env_name, field_name in TENANT_ENV_FIELDS.items():
        value = os.environ.get(env_name)
        if field_name not in raw or (raw[field_name] in (None, "") and value not in (None, "")):
            raw[field_name] = value
    return raw


def _overlay(base: TenantConfig, raw: dict[str, Any], *, source: str) -> TenantConfig:
    """Apply raw values to ``base`` through the ONE parser table (:func:`_coerce`).

    A value that is unset (None), or blank where blank is not itself a value, keeps the base. The
    same rules apply to the environment and to a stored row, so the two cannot read one setting two
    ways. A stored from_address is honoured only inside the platform's allowed_from_domains.
    """
    values: dict[str, Any] = {}
    for name, value in raw.items():
        if name not in _BEHAVIOUR_FIELDS and name not in SECRET_FIELDS:
            continue
        if value is None or (isinstance(value, str) and not value and name not in _BLANK_IS_VALUE):
            continue
        coerced = _coerce(name, value)
        if coerced is None or (isinstance(coerced, str) and not coerced and name not in _BLANK_IS_VALUE):
            continue
        values[name] = coerced
    if source == "row" and "from_address" in values:
        allowed = {d.lower() for d in values.get("allowed_from_domains", base.allowed_from_domains)}
        domain = parseaddr(values["from_address"])[1].rpartition("@")[2].lower()
        if domain not in allowed:
            values["from_address"] = DEFAULT_FROM
    return _finish(replace(base, **values))


def _from_env(tenant_id: str) -> TenantConfig:
    """The single-mode configuration: exactly what the environment has always said, parsed by
    the same table a stored row uses."""
    return _overlay(TenantConfig(tenant_id=tenant_id), _env_raw(), source="env")


#: Behaviour fields a stored row may carry. Values are coerced to the field's type.
_BEHAVIOUR_FIELDS = (
    "approval_mode", "approval_ttl_hours", "approver_emails", "send_enabled",
    "allowed_recipients", "from_address", "business_name", "update_url",
    "portal_return_url", "allowed_link_hosts", "holdout_pct", "holdout_seed",
    "ai_disclosure", "max_touches", "send_cooldown_hours", "high_value_annual",
    "llm_draft", "llm_budget_usd", "gmail_grace_minutes", "imap_host", "imap_port",
    "imap_user", "imap_security", "imap_drafts_folder", "imap_sent_folder",
    "allowed_from_domains",
)

#: Platform-level values a tenant may never set. The send switch and the recipient
#: allowlist are the operator's decision, not the tenant's.
PLATFORM_ONLY_FIELDS: frozenset[str] = frozenset({
    "send_enabled",       # the live-sending kill switch
    "allowed_recipients", # who may be mailed at all
    "llm_draft",          # whether the platform's model key is spent on drafting
    "llm_budget_usd",     # the ceiling on that spend: a tenant must not raise its own
    "allowed_from_domains",  # which domains a tenant may send as
})


def _fernet() -> Fernet:
    raw = os.environ.get(TENANT_KEY_ENV)
    if not raw:
        raise TenantKeyMissing(
            f"{TENANT_KEY_ENV} is not set. Tenant secrets are refused rather than "
            "stored or read in plaintext; generate one with "
            "cryptography.fernet.Fernet.generate_key()"
        )
    try:
        return Fernet(raw.encode("ascii"))
    except Exception:  # a malformed key is a refusal, never a fallback
        raise TenantKeyMissing(f"{TENANT_KEY_ENV} is set but is not valid for Fernet") from None


def encrypt_secrets(secrets: dict[str, str]) -> bytes:
    unknown = set(secrets) - set(SECRET_FIELDS)
    if unknown:
        raise TenantConfigError(f"not a secret field: {sorted(unknown)}")
    return _fernet().encrypt(json.dumps(secrets, sort_keys=True).encode("utf-8"))


def decrypt_secrets(blob: bytes) -> dict[str, str]:
    try:
        data = json.loads(_fernet().decrypt(blob).decode("utf-8"))
    except InvalidToken:
        raise TenantSecretError("tenant secrets do not decrypt with this key") from None
    except (ValueError, UnicodeDecodeError):
        raise TenantSecretError("tenant secrets are not a valid record") from None
    if not isinstance(data, dict):
        raise TenantSecretError("tenant secrets are not a valid record")
    return {k: str(v) for k, v in data.items() if k in SECRET_FIELDS}


def save_tenant_config(
    db: Database, tenant_id: str, behaviour: dict[str, Any], secrets: dict[str, str]
) -> None:
    """Write one tenant's row. Refuses unknown fields and platform-only fields."""
    tid = _check_tenant_id(tenant_id)
    unknown = set(behaviour) - set(_BEHAVIOUR_FIELDS)
    if unknown:
        raise TenantConfigError(f"not a tenant behaviour field: {sorted(unknown)}")
    platform = set(behaviour) & PLATFORM_ONLY_FIELDS
    if platform:
        raise TenantConfigError(f"platform-only field, not tenant-editable: {sorted(platform)}")
    # MERGE, never replace: a save names the fields it changes. Fields it does not name,
    # including the operator's platform fields and every secret not passed here, are kept.
    # The read and the write happen under ONE lock. Read-then-write-later let a concurrent
    # operator change (the send kill switch) be overwritten by this save's stale copy.
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with db.lock:
        row = db.conn.execute(
            "SELECT behaviour_json, secrets_enc FROM tenant_config WHERE tenant_id = ?", (tid,)
        ).fetchone()
        existing_behaviour = json.loads(row["behaviour_json"] or "{}") if row else {}
        existing_secrets = decrypt_secrets(row["secrets_enc"]) if row and row["secrets_enc"] else {}
        merged_behaviour = {**existing_behaviour, **behaviour}
        merged_secrets = {**existing_secrets, **{k: v for k, v in secrets.items() if v}}
        blob = encrypt_secrets(merged_secrets)  # refuses before any write when the key is absent
        db.conn.execute(
            "INSERT INTO tenant_config (tenant_id, behaviour_json, secrets_enc, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET "
            "behaviour_json = excluded.behaviour_json, "
            "secrets_enc = excluded.secrets_enc, updated_at = excluded.updated_at",
            (tid, json.dumps(merged_behaviour, sort_keys=True), blob, now),
        )
        db.conn.commit()


def set_platform_fields(
    db: Database, tenant_id: str, *, send_enabled: bool, allowed_recipients: list[str],
    llm_draft: bool | None = None, llm_budget_usd: object = None,
    allowed_from_domains: list[str] | None = None,
) -> None:
    """PLATFORM OPERATOR ONLY: the platform-only settings a tenant may never change itself.

    The send switch and the recipient allowlist are always written. The LLM switch, its budget
    and the sender-domain allowlist are written when given (None leaves the stored value as it is).
    A tenant cannot set any of these (:func:`save_tenant_config` refuses them).
    """
    tid = _check_tenant_id(tenant_id)
    with db.lock:
        row = db.conn.execute(
            "SELECT behaviour_json FROM tenant_config WHERE tenant_id = ?", (tid,)
        ).fetchone()
        behaviour = json.loads(row["behaviour_json"]) if row else {}
        behaviour["send_enabled"] = bool(send_enabled)
        behaviour["allowed_recipients"] = [str(a).strip().lower() for a in allowed_recipients]
        if llm_draft is not None:
            behaviour["llm_draft"] = bool(llm_draft)
        if llm_budget_usd is not None:
            behaviour["llm_budget_usd"] = llm_budget_usd
        if allowed_from_domains is not None:
            behaviour["allowed_from_domains"] = [str(d).strip().lower() for d in allowed_from_domains]
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if row:
            db.conn.execute(
                "UPDATE tenant_config SET behaviour_json = ?, updated_at = ? WHERE tenant_id = ?",
                (json.dumps(behaviour, sort_keys=True), now, tid),
            )
        else:
            db.conn.execute(
                "INSERT INTO tenant_config (tenant_id, behaviour_json, secrets_enc, updated_at) "
                "VALUES (?, ?, NULL, ?)",
                (tid, json.dumps(behaviour, sort_keys=True), now),
            )
        db.conn.commit()


def _load_row(db: Database, tenant_id: str) -> dict[str, Any] | None:
    with db.lock:
        row = db.conn.execute(
            "SELECT behaviour_json, secrets_enc FROM tenant_config WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    if row is None:
        return None
    behaviour = json.loads(row["behaviour_json"] or "{}")
    secrets = decrypt_secrets(row["secrets_enc"]) if row["secrets_enc"] else {}
    return {"behaviour": behaviour, "secrets": secrets}


def _coerce(name: str, value: Any) -> Any:
    """Parse a stored JSON value into the field's type, through the same rules as the env path."""
    if value is None:
        return None
    if name == "approver_emails":
        return _emails(",".join(value)) if isinstance(value, list) else _emails(str(value))
    if name in ("allowed_recipients", "allowed_from_domains"):
        return tuple(str(v).strip().lower() for v in value) if isinstance(value, list) else _csv(str(value))
    if name == "allowed_link_hosts":
        # A list is set (even empty: no host allowed). A string is unset when blank, otherwise set.
        if isinstance(value, list):
            return tuple(str(v).strip().lower() for v in value if str(v).strip())
        text = str(value).strip()
        return _csv(text) if text else None
    if name == "imap_security":
        return str(value).strip().lower()
    if name in ("update_url", "pii_salt", "imap_password"):
        return str(value)  # exact, as main reads it: no stripping, and "" stays ""
    if name == "approval_mode":
        return _approval_mode(str(value))
    if name == "holdout_pct":
        return _holdout(str(value))
    if name == "approval_ttl_hours":
        return _int_or(str(value), DEFAULT_TTL_HOURS, 1)
    if name == "max_touches":
        return _int_or(str(value), DEFAULT_MAX_TOUCHES, 1)
    if name == "gmail_grace_minutes":
        return _int_or(str(value), DEFAULT_GRACE_MINUTES, 1)
    if name == "send_cooldown_hours":
        return _float_or(str(value), DEFAULT_COOLDOWN_HOURS, 0.0)
    if name in ("high_value_annual",):
        return _high_value(str(value))
    if name == "llm_budget_usd":
        return _budget(str(value))
    if name == "imap_port":
        return _port(str(value))
    if name == "send_enabled" or name == "llm_draft":
        return bool(value) if isinstance(value, bool) else _switch_on(str(value))
    return str(value).strip()


def _platform_base(tenant_id: str) -> TenantConfig:
    """The platform-level values a tenant falls back to when its own row does not set them.

    Design (docs/multi-tenancy.md): the shared Resend key, the default touch cap and the
    default cooldown are deployment settings. A tenant overrides them in its row. Nothing
    else is read from the environment in multi mode.
    """
    return TenantConfig(
        tenant_id=tenant_id,
        max_touches=_int_or(os.environ.get("PAYPILOT_MAX_TOUCHES"), DEFAULT_MAX_TOUCHES, 1),
        send_cooldown_hours=_float_or(
            os.environ.get("PAYPILOT_SEND_COOLDOWN_HOURS"), DEFAULT_COOLDOWN_HOURS, 0.0
        ),
        resend_api_key=(os.environ.get("RESEND_API_KEY") or "").strip(),
    )


def _from_row(tenant_id: str, row: dict[str, Any], base: TenantConfig) -> TenantConfig:
    """Overlay a stored row on ``base``, through the same parser table as the environment."""
    raw: dict[str, Any] = dict(row["behaviour"])
    raw.update({k: v for k, v in row["secrets"].items() if k in SECRET_FIELDS})
    return _overlay(base, raw, source="row")


def _finish(config: TenantConfig) -> TenantConfig:
    """Rules that hold for every source. The demo tenant never sends."""
    if config.tenant_id == DEMO_TENANT and config.send_enabled:
        return replace(config, send_enabled=False)
    return config


def config_for(tenant_id: str, db: Database | None = None) -> TenantConfig:
    """The one way to obtain a tenant's configuration.

    Raises :class:`UnknownTenant` for an unknown tenant, in every mode. It never
    returns a default config for a tenant it does not know.
    """
    from app.store import configured_tenant, tenancy_mode

    tid = _check_tenant_id(tenant_id)
    single_here = tenancy_mode() == "single" and tid == configured_tenant()
    if db is not None:
        row = _load_row(db, tid)
        if row is not None:
            # Single mode: the row lays OVER the environment, field by field. Multi mode: the
            # row lays over the platform defaults only, never over an environment tenant.
            base = _from_env(tid) if single_here else _platform_base(tid)
            return _from_row(tid, row, base)
    if single_here:
        return _finish(_from_env(tid))
    if tid == DEMO_TENANT:
        # The reserved demo tenant has built-in defaults and no secrets: it sends nothing
        # and reads only the fixtures. It is known, not guessed at, so it is not refused.
        return _finish(TenantConfig(tenant_id=tid, send_enabled=False))
    raise UnknownTenant(f"no configuration for tenant {tid!r}")


def platform_config() -> TenantConfig:
    """The deployment's environment configuration, for scripts with no ledger.

    The canary, the evaluation kit and the preflight run without a database. They read the
    environment only: never a ledger, never a tenant row, and never raise for a missing key.
    """
    from app.store import configured_tenant

    return _from_env(configured_tenant())


def current_config() -> TenantConfig:
    """The configuration of the tenant this deployment is bound to (single mode only).

    Multi mode has no ambient tenant, so this raises there. Anything that runs for a
    tenant takes that tenant's config, or the store it is bound to, instead.
    """
    from app.store import configured_tenant, get_store, tenancy_mode

    if tenancy_mode() == "multi":
        raise UnknownTenant("PAYPILOT_TENANCY=multi: there is no ambient tenant config")
    return config_for(configured_tenant(), db=get_store().database)


def config_of(store) -> TenantConfig:
    """The configuration of the tenant a store is bound to.

    A tenant-bound store carries its tenant, so this is per tenant by construction. A
    stand-in without a tenant (a test double) falls back to :func:`current_config`,
    which in multi mode raises rather than guessing.
    """
    tid = getattr(store, "tenant_id", None)
    db = getattr(store, "database", None)
    if tid is None or db is None:
        return current_config()
    return config_for(tid, db)


def customers_allowed(tenant_id: str) -> bool:
    """Whether ``data/customers.json`` may be read on behalf of this tenant."""
    return tenant_id == DEMO_TENANT


__all__ = [
    "APPROVAL_MODES",
    "DEFAULT_TENANT",
    "DEMO_TENANT",
    "PLATFORM_ONLY_FIELDS",
    "SECRET_FIELDS",
    "TENANT_ENV_FIELDS",
    "TENANT_KEY_ENV",
    "TenantConfig",
    "TenantConfigError",
    "TenantKeyMissing",
    "TenantSecretError",
    "UnknownTenant",
    "config_for",
    "config_of",
    "current_config",
    "customers_allowed",
    "save_tenant_config",
    "set_platform_fields",
]
