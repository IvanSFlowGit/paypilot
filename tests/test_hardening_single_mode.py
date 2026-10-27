# ruff: noqa: F811  (pytest helpers imported by name from test_approval)
"""Single-mode behaviours that M3 changed by accident and the reviewer found.

Each one restores what main did, or states the difference in docs/tenant-config.md.
"""

from __future__ import annotations

import logging

import pytest

from app import api
from app.store import reset_store
from tests.test_approval import APPROVER, BASE, CUSTOMER, _preflight, _ready_env  # noqa: F401

MESSAGE = "PAYPILOT_UPDATE_URL not set"


def _warned(caplog) -> bool:
    return any(MESSAGE in r.getMessage() for r in caplog.records)


# -- the boot warning for a missing card-update URL -----------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_unset_or_empty_update_url_warns_at_boot_as_main_did(monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv("PAYPILOT_UPDATE_URL", raising=False)
    else:
        monkeypatch.setenv("PAYPILOT_UPDATE_URL", value)
    with caplog.at_level(logging.WARNING, logger="paypilot"):
        api._warn_default_update_url()
    assert _warned(caplog), f"no warning for PAYPILOT_UPDATE_URL={value!r}"


def test_a_configured_update_url_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "https://pay.merchant.test/card")
    with caplog.at_level(logging.WARNING, logger="paypilot"):
        api._warn_default_update_url()
    assert not _warned(caplog)


# -- the preflight reads the environment only -----------------------------------------------


def test_the_preflight_never_opens_or_creates_a_ledger(tmp_path, monkeypatch):
    """It runs on an operator's laptop against an env file. It used to call approval_mode(),
    sending_enabled() and allowed_recipients() with no config, which opened the ledger (and created
    it) and could report a stored row's values instead of the env file's."""
    ledger = tmp_path / "must_not_exist.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(ledger))
    reset_store(None)
    _ready_env(monkeypatch)
    try:
        problems, notes = _preflight().check()
    finally:
        reset_store(None)
    assert not ledger.exists(), "the preflight created a ledger"
    assert problems == [], problems
    assert any("approval mode: email" in n for n in notes)


def test_the_preflight_reports_the_env_file_not_a_stored_row(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from app.store import Database
    from app.tenant_config import TENANT_KEY_ENV, save_tenant_config, set_platform_fields

    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    path = tmp_path / "with_row.db"
    db = Database(path, owner_tenant="default")
    save_tenant_config(db, "default", {"approval_mode": "off"}, {})
    set_platform_fields(db, "default", send_enabled=True, allowed_recipients=[CUSTOMER])
    db.conn.close()
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(path))
    reset_store(None)
    _ready_env(monkeypatch)  # the env file says email mode; the row says off
    try:
        _problems, notes = _preflight().check()
    finally:
        reset_store(None)
    assert any("approval mode: email" in n for n in notes), notes
