"""Tenant configuration (M3): one builder, encrypted secrets, no import-time tenant reads.

Offline and hermetic. The tests run against the isolated_store fixture (a private
SQLite file) and monkeypatched environments; nothing reaches a network or a real
Stripe account.
"""

from __future__ import annotations

import ast
import threading
import types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import stripe_client, tenant_config
from app.tenant_config import (
    DEMO_TENANT,
    SECRET_FIELDS,
    TENANT_KEY_ENV,
    TenantConfig,
    TenantKeyMissing,
    TenantSecretError,
    UnknownTenant,
    config_for,
    current_config,
    customers_allowed,
    save_tenant_config,
)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

SECRET_VALUE = "whsec_test_value_that_must_never_print"


@pytest.fixture
def key(monkeypatch):
    value = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(TENANT_KEY_ENV, value)
    return value


def _multi(monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")


# --- the object -----------------------------------------------------------------

def test_repr_and_str_never_show_a_secret():
    config = TenantConfig(tenant_id="tn_a", stripe_webhook_secret=SECRET_VALUE,
                          resend_api_key=SECRET_VALUE, pii_salt=SECRET_VALUE)
    assert SECRET_VALUE not in repr(config)
    assert SECRET_VALUE not in str(config)
    assert "stripe_webhook_secret=***" in repr(config)


def test_the_config_is_frozen():
    config = TenantConfig(tenant_id="tn_a")
    with pytest.raises(AttributeError):  # FrozenInstanceError is an AttributeError
        config.send_enabled = True  # type: ignore[misc]


# --- the single-mode environment fallback ---------------------------------------

def test_single_mode_with_no_row_reads_the_environment(isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "https://merchant.example/pay")
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    config = config_for("default", isolated_store.database)
    assert config.update_url == "https://merchant.example/pay"
    assert config.send_enabled is True


def test_single_mode_refuses_a_tenant_it_is_not_bound_to(isolated_store):
    with pytest.raises(UnknownTenant):
        config_for("tn_someone_else", isolated_store.database)


# --- multi mode: the row is the only source --------------------------------------

def test_multi_mode_unknown_tenant_raises_and_never_defaults(isolated_store, monkeypatch):
    _multi(monkeypatch)
    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "https://merchant.example/pay")
    with pytest.raises(UnknownTenant):
        config_for("tn_missing", isolated_store.database)


def test_multi_mode_has_no_ambient_config(isolated_store, monkeypatch):
    _multi(monkeypatch)
    with pytest.raises(UnknownTenant):
        current_config()


def test_config_for_returns_the_asked_tenants_own_row(isolated_store, key, monkeypatch):
    """Mutation target: a lookup that reads another tenant's row is caught here."""
    _multi(monkeypatch)
    db = isolated_store.database
    save_tenant_config(db, "tn_a", {"update_url": "https://a.example/u"}, {})
    save_tenant_config(db, "tn_b", {"update_url": "https://b.example/u"}, {})
    a = config_for("tn_a", db)
    b = config_for("tn_b", db)
    assert (a.tenant_id, a.update_url) == ("tn_a", "https://a.example/u")
    assert (b.tenant_id, b.update_url) == ("tn_b", "https://b.example/u")


def test_a_platform_only_field_cannot_be_written_by_a_tenant(isolated_store, key):
    with pytest.raises(tenant_config.TenantConfigError):
        save_tenant_config(isolated_store.database, "tn_a", {"send_enabled": True}, {})


# --- secrets ------------------------------------------------------------------------

def test_secrets_are_encrypted_at_rest_and_round_trip(isolated_store, key, monkeypatch):
    _multi(monkeypatch)
    db = isolated_store.database
    save_tenant_config(db, "tn_a", {}, {"stripe_webhook_secret": SECRET_VALUE})
    raw = db.conn.execute(
        "SELECT secrets_enc FROM tenant_config WHERE tenant_id = 'tn_a'"
    ).fetchone()["secrets_enc"]
    assert SECRET_VALUE.encode() not in bytes(raw)
    assert config_for("tn_a", db).stripe_webhook_secret == SECRET_VALUE


def test_no_key_means_no_secret_is_written(isolated_store, monkeypatch):
    monkeypatch.delenv(TENANT_KEY_ENV, raising=False)
    with pytest.raises(TenantKeyMissing):
        save_tenant_config(isolated_store.database, "tn_a", {}, {"pii_salt": SECRET_VALUE})
    count = isolated_store.database.conn.execute(
        "SELECT COUNT(*) FROM tenant_config"
    ).fetchone()[0]
    assert count == 0


def test_a_wrong_key_refuses_to_decrypt(isolated_store, key, monkeypatch):
    _multi(monkeypatch)
    db = isolated_store.database
    save_tenant_config(db, "tn_a", {}, {"pii_salt": SECRET_VALUE})
    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    with pytest.raises(TenantSecretError) as info:
        config_for("tn_a", db)
    assert SECRET_VALUE not in str(info.value)


def test_a_missing_key_refuses_to_decrypt(isolated_store, key, monkeypatch):
    _multi(monkeypatch)
    db = isolated_store.database
    save_tenant_config(db, "tn_a", {}, {"pii_salt": SECRET_VALUE})
    monkeypatch.delenv(TENANT_KEY_ENV)
    with pytest.raises(TenantKeyMissing):
        config_for("tn_a", db)


def test_a_tampered_blob_refuses(isolated_store, key, monkeypatch):
    _multi(monkeypatch)
    db = isolated_store.database
    save_tenant_config(db, "tn_a", {}, {"pii_salt": SECRET_VALUE})
    blob = bytearray(db.conn.execute(
        "SELECT secrets_enc FROM tenant_config WHERE tenant_id = 'tn_a'"
    ).fetchone()["secrets_enc"])
    blob[-4] ^= 0x01
    db.conn.execute("UPDATE tenant_config SET secrets_enc = ? WHERE tenant_id = 'tn_a'",
                    (bytes(blob),))
    db.conn.commit()
    with pytest.raises(TenantSecretError):
        config_for("tn_a", db)


def test_an_unknown_secret_name_is_refused():
    with pytest.raises(tenant_config.TenantConfigError):
        tenant_config.encrypt_secrets({"not_a_secret": "x"})


def test_the_secret_list_is_the_documented_set():
    assert "stripe_api_key" in SECRET_FIELDS and "imap_password" in SECRET_FIELDS


# --- the demo tenant ----------------------------------------------------------------

def test_the_demo_tenant_never_sends_whatever_it_is_configured_to(isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANT", DEMO_TENANT)
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")
    assert current_config().send_enabled is False


def test_customer_fixtures_resolve_for_demo_only():
    assert customers_allowed(DEMO_TENANT) is True
    assert customers_allowed("default") is False
    assert customers_allowed("tn_a") is False


def test_a_non_demo_tenant_reads_no_customer_fixtures(monkeypatch):
    from app import nodes

    monkeypatch.delenv("PAYPILOT_TENANT", raising=False)
    assert nodes.load_customer_records() == []


def test_the_demo_tenant_reads_customer_fixtures(monkeypatch):
    from app import nodes

    monkeypatch.setenv("PAYPILOT_TENANT", DEMO_TENANT)
    assert nodes.load_customer_records()


# --- Stripe: the key is passed per call, never assigned ---------------------------

def _stripe_global_key_writes(tree: ast.AST) -> list[int]:
    """Lines that set ``api_key`` on the stripe SDK module, however it was imported.

    Follows ``import stripe``, ``import stripe as s`` and the names those bind, for both
    ``obj.api_key = ...`` and ``setattr(obj, "api_key", ...)``.
    """
    aliases = {"stripe"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "stripe" and alias.asname:
                    aliases.add(alias.asname)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (isinstance(target, ast.Attribute) and target.attr == "api_key"
                        and isinstance(target.value, ast.Name) and target.value.id in aliases):
                    lines.append(node.lineno)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr"
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Name)
                and node.args[0].id in aliases and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "api_key"):
            lines.append(node.lineno)
    return lines


def test_no_module_assigns_the_stripe_sdk_global_key():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        for line in _stripe_global_key_writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP).as_posix()}:{line}")
    assert offenders == [], f"stripe.api_key is assigned globally at {offenders}"


def test_the_stripe_guard_catches_its_planted_forms():
    planted = ast.parse(
        "import stripe\n"
        "import stripe as s\n"
        "stripe.api_key = 'x'\n"
        "s.api_key = 'y'\n"
        "setattr(s, 'api_key', 'z')\n"
        "other.api_key = 'w'\n"
    )
    assert _stripe_global_key_writes(planted) == [3, 4, 5]


def test_two_tenants_concurrent_portal_calls_each_use_their_own_key(monkeypatch):
    seen: list[tuple[str, str | None]] = []
    local = threading.local()

    class _Session:
        @staticmethod
        def create(**kwargs):
            seen.append((local.tenant, kwargs.get("api_key")))
            return types.SimpleNamespace(url="https://billing.stripe.com/p/session/x")

    fake = types.SimpleNamespace(billing_portal=types.SimpleNamespace(Session=_Session))
    monkeypatch.setattr(stripe_client, "get_stripe", lambda *_a, **_k: fake)
    keys = {"tn_a": "sk_test_A", "tn_b": "sk_test_B"}
    monkeypatch.setattr(stripe_client, "api_key", lambda *_a, **_k: keys[local.tenant])

    def run(tenant):
        local.tenant = tenant
        for _ in range(20):
            stripe_client.create_portal_session(f"cus_{tenant}")

    threads = [threading.Thread(target=run, args=(t,)) for t in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 40
    for tenant, sent_key in seen:
        assert sent_key == keys[tenant], f"{tenant} sent with {sent_key!r}"


# --- the import-time guard ----------------------------------------------------------

#: Platform-level environment names that the app reads at import. Each is a deployment
#: cap or a storage location, never a tenant setting. The list is what the scan finds,
#: and ``test_the_allowlist_matches_what_the_scan_finds`` keeps it exact.
PLATFORM_IMPORT_TIME_NAMES = frozenset({
    "IDEMPOTENCY_MAX",
    "PAYPILOT_SEND_BACKOFF_SECONDS",
    "PAYPILOT_SEND_MAX_ATTEMPTS",
    "RATE_LIMIT_GLOBAL_MAX",
    "RATE_LIMIT_MAX",
    "RATE_LIMIT_MAX_IPS",
    "RATE_LIMIT_WINDOW_SECONDS",
})

#: Helpers whose first argument is an environment name, read at call time by the
#: callers that use them at module level (``_MAX_ATTEMPTS = _int_env("...", 3)``).
_ENV_HELPERS = frozenset({"_int_env", "_float_env"})


def _is_env_read(node: ast.AST) -> str | None:
    """The environment name read by ``node``, '?' if it cannot be known, else None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _ENV_HELPERS:
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
            return "?"
    if isinstance(node, ast.Call):
        func = node.func
        dotted = []
        cur = func
        while isinstance(cur, ast.Attribute):
            dotted.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            dotted.append(cur.id)
        path = ".".join(reversed(dotted))
        if path in ("os.getenv", "os.environ.get", "environ.get", "getenv"):
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
            return "?"
    if isinstance(node, ast.Subscript):
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "environ":
            return "?"
    return None


def _module_level_nodes(tree: ast.Module):
    """Every node that runs when the module is imported: not inside a function body."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # The body runs at call time; the defaults and decorators run at import.
            if isinstance(node, ast.Lambda):
                continue
            stack.extend(node.decorator_list)
            stack.extend(d for d in node.args.defaults if d is not None)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        stack.extend(ast.iter_child_nodes(node))


def import_time_environment_reads() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(APP.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _module_level_nodes(tree):
            name = _is_env_read(node)
            if name is not None:
                found.append((path.name, getattr(node, "lineno", 0), name))
    return found


def test_no_app_module_reads_a_tenant_setting_from_the_environment_at_import():
    offenders = [
        f"{mod}:{line} reads {name}"
        for mod, line, name in import_time_environment_reads()
        if name == "?" or name not in PLATFORM_IMPORT_TIME_NAMES
    ]
    assert offenders == [], f"tenant-level env read at import time: {offenders}"


def test_the_guard_sees_an_import_time_read_it_is_meant_to_catch(tmp_path):
    """A control: the scanner must flag a planted module-level tenant read."""
    planted = tmp_path / "planted.py"
    planted.write_text('import os\nUPDATE = os.getenv("PAYPILOT_UPDATE_URL")\n',
                       encoding="utf-8")
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    names = [n for n in (_is_env_read(x) for x in _module_level_nodes(tree)) if n]
    assert names == ["PAYPILOT_UPDATE_URL"]


def test_the_guard_ignores_a_read_inside_a_function(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text('import os\ndef f():\n    return os.getenv("PAYPILOT_UPDATE_URL")\n',
                       encoding="utf-8")
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    assert [n for n in (_is_env_read(x) for x in _module_level_nodes(tree)) if n] == []


def test_the_scan_is_not_vacuous():
    """The scanner must find the platform reads it is allowed to find."""
    names = {name for _, _, name in import_time_environment_reads()}
    assert names & PLATFORM_IMPORT_TIME_NAMES, "the scanner found no platform read at all"


def test_the_allowlist_matches_what_the_scan_finds():
    """Keeps the allowlist honest: a name listed here that nothing reads is a lie."""
    found = {name for _, _, name in import_time_environment_reads()}
    assert found == PLATFORM_IMPORT_TIME_NAMES, (
        f"allowlist drift: scan finds {sorted(found)}, allowlist has "
        f"{sorted(PLATFORM_IMPORT_TIME_NAMES)}")


def test_the_scan_sees_a_helper_read_at_import(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text('def _int_env(n, d):\n    return d\nX = _int_env("PAYPILOT_UPDATE_URL", 3)\n',
                       encoding="utf-8")
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    assert [n for n in (_is_env_read(x) for x in _module_level_nodes(tree)) if n] == [
        "PAYPILOT_UPDATE_URL"]


def test_env_is_not_read_at_import_by_safety():
    """Regression for the removed import-time read of PAYPILOT_UPDATE_URL."""
    tree = ast.parse((APP / "safety.py").read_text(encoding="utf-8"))
    assert all(_is_env_read(n) is None for n in _module_level_nodes(tree))

