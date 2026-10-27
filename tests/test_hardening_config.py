"""Sender identity and the card-update link: no silent substitution, no blank link.

Reviewer findings 4, 5 and 9 on M3. A blank value must never quietly become somebody else's
default: a blank From refuses the send, a rejected stored From falls back to the deployment's own
sender, and a blank stored card-update URL is refused on save and ignored if one is already stored.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app import mailer
from app.store import Database
from app.tenant_config import (
    DEFAULT_FROM,
    TENANT_KEY_ENV,
    TenantConfigError,
    config_for,
    save_tenant_config,
    set_platform_fields,
)

OWN_FROM = "Acme <billing@acme.test>"


class _Resp:
    status_code = 200
    content = b"{}"

    @staticmethod
    def json():
        return {"id": "rs_ok"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this file may reach a real provider; a test that wants a post patches it."""

    def _refuse(*args, **kwargs):
        raise AssertionError("a test reached httpx.post without patching it")

    monkeypatch.setattr("httpx.post", _refuse)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    for name in ("PAYPILOT_FROM_EMAIL", "PAYPILOT_UPDATE_URL", "PAYPILOT_SEND_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    return Database(tmp_path / "h.db", owner_tenant="default")


# -- 4: a blank From refuses the send; it never becomes another sender ----------------------


def _live(monkeypatch, sent):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "me@mine.test")
    monkeypatch.setattr("httpx.post", lambda *a, **k: sent.append(k.get("json")) or _Resp())


@pytest.mark.parametrize("blank", ["  ", "\t", " \n "])
def test_a_blank_from_address_suppresses_the_send_and_posts_nothing(
    isolated_store, monkeypatch, blank
):
    sent: list = []
    _live(monkeypatch, sent)
    monkeypatch.setenv("PAYPILOT_FROM_EMAIL", blank)

    result = mailer.send_dunning_email(
        invoice_id="in_1", to="me@mine.test", subject="s", body="b", store=isolated_store
    )

    assert result["status"] == mailer.STATUS_SUPPRESSED
    assert result["error"] == "missing_from_address"
    assert sent == [], "nothing may reach the provider under a sender nobody chose"


def test_an_unset_from_address_still_uses_the_default_sender(isolated_store, monkeypatch):
    """Only a SET-but-blank value is a refusal; unset keeps the deployment default."""
    monkeypatch.delenv("PAYPILOT_FROM_EMAIL", raising=False)
    assert config_for("default").from_address == DEFAULT_FROM


def test_the_transport_refuses_a_blank_sender_whoever_the_caller_is():
    sent, _id, error = mailer._post_to_resend(
        {"from": "   ", "to": ["a@b.test"], "subject": "s", "text": "b"}, "re_key",
        config=config_for("default"),
    )
    assert (sent, error) == (False, "missing_from_address")


# -- 5: a rejected stored From falls back to the deployment's own sender --------------------


def test_a_rejected_stored_from_falls_back_to_the_base_sender_not_the_platform_default(
    db, monkeypatch
):
    monkeypatch.setenv("PAYPILOT_FROM_EMAIL", OWN_FROM)
    save_tenant_config(db, "default", {"from_address": "x@elsewhere.test"}, {})
    set_platform_fields(
        db, "default", send_enabled=False, allowed_recipients=[],
        allowed_from_domains=["alpha.test"],
    )

    assert config_for("default", db).from_address == OWN_FROM


def test_an_empty_domain_in_the_allowed_list_never_matches_an_addressless_from(db, monkeypatch):
    monkeypatch.setenv("PAYPILOT_FROM_EMAIL", OWN_FROM)
    save_tenant_config(db, "default", {"from_address": "Billing <x@>"}, {})
    set_platform_fields(
        db, "default", send_enabled=False, allowed_recipients=[],
        allowed_from_domains=["alpha.test", ""],
    )

    cfg = config_for("default", db)
    assert cfg.allowed_from_domains == ("alpha.test",)
    assert cfg.from_address == OWN_FROM


def test_a_stored_from_inside_an_allowed_domain_is_honoured(db, monkeypatch):
    monkeypatch.setenv("PAYPILOT_FROM_EMAIL", OWN_FROM)
    save_tenant_config(db, "default", {"from_address": "Pay <pay@alpha.test>"}, {})
    set_platform_fields(
        db, "default", send_enabled=False, allowed_recipients=[],
        allowed_from_domains=["alpha.test"],
    )

    assert config_for("default", db).from_address == "Pay <pay@alpha.test>"


# -- 9: a blank card-update URL is never a value in a stored row ----------------------------


def test_saving_a_blank_update_url_is_refused(db):
    for blank in ("", "   "):
        with pytest.raises(TenantConfigError):
            save_tenant_config(db, "default", {"update_url": blank}, {})


def test_a_blank_update_url_already_in_a_row_falls_back_to_the_base(db, monkeypatch):
    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "https://pay.acme.test/card")
    with db.lock:  # a row written before the refusal existed, or by hand
        db.conn.execute(
            "INSERT INTO tenant_config (tenant_id, behaviour_json, secrets_enc, updated_at) "
            "VALUES ('default', '{\"update_url\": \"\"}', NULL, '2026-10-27T00:00:00')"
        )
        db.conn.commit()

    assert config_for("default", db).update_url == "https://pay.acme.test/card"


def test_a_set_but_empty_environment_update_url_keeps_the_old_behaviour(db, monkeypatch):
    """Main treats a set-but-empty PAYPILOT_UPDATE_URL as empty, so every link is refused."""
    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "")
    assert config_for("default").update_url == ""


# -- each layer on its own: the two defences against an empty domain cover for each other ----


def test_the_list_parser_drops_empty_entries_on_its_own():
    from app.tenant_config import _coerce

    assert _coerce("allowed_from_domains", ["alpha.test", "", "  "]) == ("alpha.test",)
    assert _coerce("allowed_recipients", ["a@b.test", ""]) == ("a@b.test",)


def test_the_domain_check_rejects_an_addressless_from_even_if_the_list_held_an_empty_string():
    from app.tenant_config import TenantConfig, _overlay

    base = TenantConfig(
        tenant_id="default", from_address=OWN_FROM, allowed_from_domains=("alpha.test", "")
    )
    cfg = _overlay(base, {"from_address": "Billing <x@>"}, source="row")

    assert cfg.from_address == OWN_FROM
