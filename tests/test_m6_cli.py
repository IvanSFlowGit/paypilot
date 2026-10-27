"""M6: the lifecycle CLI and the generated per-tenant sheet."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import authz, principals
from app.store import get_platform, reset_database, store_for

ROOT = Path(__file__).resolve().parents[1]


def _load_cli():
    spec = importlib.util.spec_from_file_location("tenants_cli", ROOT / "scripts" / "tenants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli(monkeypatch, isolated_store, capsys):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.setenv("PAYPILOT_TENANT_KEY", Fernet.generate_key().decode("ascii"))
    reset_database()
    mod = _load_cli()

    def run(*argv, stdin: str | None = None):
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = mod.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    yield run
    reset_database()


def _token(out: str) -> str:
    return re.search(r"pp_[0-9a-f]{8}_[0-9a-f]{48}", out).group(0)


def test_importing_the_cli_does_not_switch_the_process_to_multi_mode(monkeypatch):
    monkeypatch.delenv("PAYPILOT_TENANCY", raising=False)
    _load_cli()
    import os

    assert os.environ.get("PAYPILOT_TENANCY") in (None, "single")


def test_bootstrap_prints_a_working_platform_admin_token_once_and_then_refuses(cli):
    code, out, _ = cli("bootstrap")
    assert code == 0 and "ONCE" in out
    principal = principals.resolve(get_platform(), f"Bearer {_token(out)}")
    assert principal.role == authz.PLATFORM_ADMIN
    code, _, err = cli("bootstrap")
    assert code == 1 and "already_bootstrapped" in err


def test_create_prints_webhooks_and_a_first_admin_token_and_sends_nothing(cli):
    code, out, _ = cli("create", "alpha", "--name", "Alpha Ltd", "--base-url", "https://pp.example")
    assert code == 0
    assert "https://pp.example/t/alpha/webhooks/stripe" in out and "sending OFF" in out
    principal = principals.resolve(get_platform(), f"Bearer {_token(out)}")
    assert (principal.role, principal.tenant_id) == ("tenant_admin", "alpha")
    from app.store import get_database
    from app.tenant_config import config_for

    cfg = config_for("alpha", get_database())
    assert cfg.send_enabled is False and cfg.allowed_recipients == ()
    assert cli("create", "alpha", "--name", "Again")[0] == 1


def test_secret_comes_from_stdin_and_is_never_echoed(cli):
    cli("create", "alpha", "--name", "Alpha")
    code, out, err = cli("secret", "alpha", "stripe_webhook_secret", stdin="whsec_NEVERPRINT\n")
    assert code == 0 and "NEVERPRINT" not in out + err
    code, doc, _ = cli("doc", "alpha")
    assert "stripe_webhook_secret: set" in doc and "NEVERPRINT" not in doc
    assert cli("secret", "alpha", "not_a_field", stdin="x")[0] == 1
    assert cli("secret", "alpha", "stripe_webhook_secret", stdin="")[0] == 1


def test_the_generated_sheet_never_contains_a_token_or_a_secret(cli):
    _, out, _ = cli("create", "alpha", "--name", "Alpha")
    raw = _token(out)
    cli("secret", "alpha", "resend_api_key", stdin="re_SECRETVALUE")
    _, doc, _ = cli("doc", "alpha", "--base-url", "https://pp.example")
    assert raw not in doc and "SECRETVALUE" not in doc
    assert "NO SECRET: deliveries are refused" in doc  # stripe secret not yet stored
    assert "Sending: OFF" in doc and "https://pp.example/t/alpha/webhooks/stripe" in doc


def test_platform_fields_turn_sending_on_only_through_the_operator_command(cli):
    cli("create", "alpha", "--name", "Alpha")
    code, out, _ = cli("platform", "alpha", "--send", "on", "--recipient", "ap@acme.test",
                       "--from-domain", "acme.test")
    assert code == 0
    _, doc, _ = cli("doc", "alpha")
    assert "Sending: ON" in doc and "ap@acme.test" in doc and "acme.test" in doc


def test_token_create_list_revoke_and_the_role_rules(cli):
    cli("create", "alpha", "--name", "Alpha")
    code, out, _ = cli("token", "create", "alpha", "tenant_viewer", "--label", "board")
    assert code == 0
    tid = re.search(r"token_id: (\w+)", out).group(1)
    assert principals.resolve(get_platform(), f"Bearer {_token(out)}").role == "tenant_viewer"
    assert cli("token", "create", "alpha", "nonsense")[0] == 1
    assert cli("token", "create", "nosuch", "tenant_viewer")[0] == 1
    _, listing, _ = cli("token", "list", "alpha")
    assert tid in listing and "board" in listing
    assert cli("token", "revoke", "alpha", tid)[0] == 0
    assert principals.resolve(get_platform(), f"Bearer {_token(out)}") is None
    assert cli("token", "revoke", "alpha", tid)[0] == 1


def test_suspend_requires_a_reason_and_resume_requires_a_suspension(cli):
    cli("create", "alpha", "--name", "Alpha")
    assert cli("resume", "alpha")[0] == 1
    code, out, _ = cli("suspend", "alpha", "--reason", "unpaid")
    assert code == 0 and "approvals expired" in out
    assert cli("suspend", "alpha", "--reason", "again")[0] == 1
    assert cli("resume", "alpha")[0] == 0


def test_offboard_needs_both_flags_exports_then_erases(cli, tmp_path):
    cli("create", "alpha", "--name", "Alpha")
    store_for("alpha").record_failure(invoice_id="in_1", customer_id="c1", amount_minor=100,
                                      currency="eur", failure_code="expired_card")
    target = tmp_path / "alpha-export.json"
    assert cli("offboard", "alpha")[0] == 1
    assert cli("offboard", "alpha", "--yes-erase-everything")[0] == 1
    assert store_for("alpha").get_failure("in_1") is not None
    code, out, _ = cli("offboard", "alpha", "--yes-erase-everything", "--export-to", str(target))
    assert code == 0 and "failures: 1" in out
    exported = json.loads(target.read_text())
    assert exported["failures"][0]["invoice_id"] == "in_1"
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert store_for("alpha").get_failure("in_1") is None
    assert cli("offboard", "alpha", "--yes-erase-everything", "--export-to", str(target))[0] == 1


def test_a_legacy_ledger_is_could_not_run_not_a_traceback(monkeypatch, tmp_path, capsys):
    from tests.test_tenancy_migration import _build_legacy

    legacy = tmp_path / "legacy.db"
    _build_legacy(legacy)
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(legacy))
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    reset_database()
    from app.store import reset_store

    reset_store(None)
    try:
        assert _load_cli().main(["list"]) == 2
        assert "CANNOT RUN" in capsys.readouterr().err
    finally:
        reset_database()
