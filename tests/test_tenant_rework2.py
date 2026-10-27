"""M3 rework cycle 2: each blocker and major has a test that fails without its fix.

Offline. Fake transports; no network. The transport test patches ``httpx.post`` so the real
output guard runs (a fake at ``_post_to_resend`` would hide it).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import approval, gmail_drafts, mailer, nodes
from app import tenant_config as tc
from app.meter import CostMeter, get_cost_meter, reset_meters
from app.store import Database, Store, get_store
from app.tenant_config import (
    DEFAULT_FROM,
    DEFAULT_HOLDOUT_SEED,
    TENANT_KEY_ENV,
    TenantConfig,
    UnknownTenant,
    config_for,
    save_tenant_config,
    set_platform_fields,
)

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SENTINEL_"


# --- helpers -------------------------------------------------------------------------------

def _strings(obj, out=None):
    """Every string reachable from ``obj``, reading dataclass fields directly (asdict-style)."""
    out = [] if out is None else out
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            _strings(getattr(obj, f.name), out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _strings(k, out)
            _strings(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _strings(v, out)
    else:
        out.append(str(obj))
    return out


def _sentinel_config(tenant_id: str = "tn_secret") -> TenantConfig:
    return TenantConfig(
        tenant_id=tenant_id,
        business_name="Merchant Ltd",
        update_url="https://pay.merchant.example/card",
        stripe_api_key=SENTINEL + "stripe_api_key",
        stripe_webhook_secret=SENTINEL + "stripe_webhook",
        recharge_client_secret=SENTINEL + "recharge",
        chargebee_webhook_secret=SENTINEL + "chargebee",
        resend_api_key=SENTINEL + "resend",
        pii_salt=SENTINEL + "pii_salt",
        gmail_client_id=SENTINEL + "gmail_id",
        gmail_client_secret=SENTINEL + "gmail_secret",
        gmail_refresh_token=SENTINEL + "gmail_refresh",
        imap_password=SENTINEL + "imap",
    )


_EVENT = {"customer_id": "cust_001", "amount": 49.0, "currency": "eur",
          "failure_code": "expired_card", "attempt": 1}


class _Resp:
    status_code = 200
    content = b'{"id": "msg_test"}'

    def json(self):
        return {"id": "msg_test"}


def _record_httpx(monkeypatch, sent: list) -> None:
    """Patch the transport, not the guard: the real output guard runs before this."""
    def _post(url, json=None, headers=None, timeout=None):  # noqa: A002 - httpx signature
        sent.append({"payload": json, "key": (headers or {}).get("Authorization", "")})
        return _Resp()

    monkeypatch.setattr(mailer.httpx, "post", _post)


@pytest.fixture
def multi(tmp_path, monkeypatch):
    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    for name in ("PAYPILOT_UPDATE_URL", "PAYPILOT_MAX_TOUCHES", "PAYPILOT_SEND_COOLDOWN_HOURS",
                 "RESEND_API_KEY", "PAYPILOT_FROM_EMAIL", "PAYPILOT_APPROVER_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    db = Database(tmp_path / "multi.db", owner_tenant="tn_alpha")
    yield db


# --- B3: the send switch keeps the exact old rule ---------------------------------------------

def _old_send_switch(raw: str | None) -> bool:
    """The rule as it stands on main (app/mailer.py sending_enabled)."""
    return (raw or "").strip() in ("1", "true", "yes")


@pytest.mark.parametrize("raw", ["True", "TRUE", "Yes", "YES", " 1 ", "1", "true", "yes", "on", "0", ""])
def test_b3_the_send_switch_matches_the_old_rule_on_every_spelling(monkeypatch, raw):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", raw)
    assert config_for("default").send_enabled is _old_send_switch(raw), raw


@pytest.mark.parametrize("raw", ["True", "TRUE", "Yes", "YES", "on"])
def test_b3_a_capitalised_switch_value_does_not_send_live(monkeypatch, raw):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", raw)
    assert config_for("default").send_enabled is False


@pytest.mark.parametrize("raw", ["True", "TRUE", "Yes", "1", "true", "0"])
def test_b3_the_llm_switch_keeps_the_old_rule_too(monkeypatch, raw):
    monkeypatch.setenv("PAYPILOT_LLM_DRAFT", raw)
    assert config_for("default").llm_draft is _old_send_switch(raw), raw


# --- B1: secrets never enter graph state ------------------------------------------------------

def test_b1_no_secret_appears_in_any_state_the_graph_yields(monkeypatch):
    """Runs the real graph with a config full of sentinel secrets and reads EVERY state it yields,
    walking dataclass fields directly. A secret anywhere in any state fails."""
    from app.graph import graph, initial_state
    from app.tenant_config import running_config
    from app.tracing import trace_config

    cfg = _sentinel_config()
    event = dict(_EVENT)
    initial = initial_state(event, cfg)  # the production construction, not a copy of it
    states = []
    with running_config(cfg):
        for state in graph.stream(initial, config=trace_config(event, cfg), stream_mode="values"):
            states.append(state)
    assert states, "the graph yielded no state"
    for state in [initial, *states]:
        leaked = [s for s in _strings(state) if SENTINEL in s]
        assert leaked == [], f"a secret reached graph state: {leaked[:3]}"
        assert SENTINEL not in json.dumps(state, default=repr)


def test_b1_run_recovery_puts_only_the_behaviour_view_in_state(monkeypatch):
    from app import graph as graph_module

    seen = {}
    real = graph_module.graph.invoke

    def _spy(state, config=None, **kw):
        seen["state"] = state
        return real(state, config=config, **kw)

    monkeypatch.setattr(graph_module.graph, "invoke", _spy)
    graph_module.run_recovery(dict(_EVENT), tenant_config=_sentinel_config())
    leaked = [s for s in _strings(seen["state"]) if SENTINEL in s]
    assert leaked == [], leaked[:3]
    assert seen["state"]["tenant_config"].business_name == "Merchant Ltd"  # non-secret kept


def test_b1_the_langfuse_event_serializer_sees_no_secret(monkeypatch):
    """The serializer Langfuse uses for chain inputs. Positive control first: the serializer
    really does export a raw config's secrets, so the negative result below means something."""
    try:
        from langfuse._utils.serializer import EventSerializer
    except ImportError as exc:  # pragma: no cover - reported, not hidden
        pytest.skip(f"langfuse EventSerializer not importable in this venv: {exc}")

    cfg = _sentinel_config()
    raw = json.dumps(cfg, cls=EventSerializer)
    assert SENTINEL in raw, "positive control: the serializer no longer exports raw secrets"
    view = json.dumps(cfg.behaviour_view(), cls=EventSerializer)
    assert SENTINEL not in view


def test_b1_mask_value_redacts_a_dataclass_instead_of_passing_it_through():
    """Planted control: the raw dataclass, passed unchanged, carries the secret; the masker must
    convert it and redact every credential-named field."""
    from app.tracing import _mask_value

    cfg = _sentinel_config()
    assert SENTINEL in json.dumps(cfg, default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else str(o)), \
        "control: the raw object does hold the secret"
    masked = _mask_value({"cfg": cfg})
    assert SENTINEL not in json.dumps(masked, default=str)
    assert masked["cfg"]["stripe_api_key"] == "***"
    assert masked["cfg"]["business_name"] == "Merchant Ltd"


# --- B2: the transport guard reads the BOUND tenant's link ------------------------------------

def test_b2_single_mode_custom_update_url_is_not_suppressed_by_the_guard(monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "cust@mail.test")
    monkeypatch.setenv("RESEND_API_KEY", "re_single")
    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "https://pay.merchant.example/card")
    sent: list = []
    _record_httpx(monkeypatch, sent)
    result = mailer.send_dunning_email(
        invoice_id="in_b2_single", to="cust@mail.test", subject="Payment failed",
        body="Update your card at https://pay.merchant.example/card", store=get_store(),
    )
    assert result["status"] == mailer.STATUS_SENT, result
    assert len(sent) == 1, "the merchant's own link was suppressed by the guard"


def test_b2_single_mode_a_foreign_link_is_still_refused(monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "cust@mail.test")
    monkeypatch.setenv("RESEND_API_KEY", "re_single")
    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "https://pay.merchant.example/card")
    sent: list = []
    _record_httpx(monkeypatch, sent)
    result = mailer.send_dunning_email(
        invoice_id="in_b2_foreign", to="cust@mail.test", subject="Payment failed",
        body="Pay at https://evil.attacker.test/steal", store=get_store(),
    )
    assert result["status"] == mailer.STATUS_SUPPRESSED, result
    assert sent == []


def test_b2_multi_mode_each_tenant_sends_its_own_link_and_not_the_others(multi, monkeypatch):
    db = multi
    for tid, url in (("tn_alpha", "https://alpha.example.test/update"),
                     ("tn_beta", "https://beta.example.test/update")):
        save_tenant_config(db, tid, {"approval_mode": "off", "update_url": url,
                                     "from_address": f"{tid} <bill@{tid}.test>"},
                           {"resend_api_key": f"re_{tid}"})
        set_platform_fields(db, tid, send_enabled=True, allowed_recipients=["cust@mail.test"])
    sent: list = []
    _record_httpx(monkeypatch, sent)
    for tid, url in (("tn_alpha", "https://alpha.example.test/update"),
                     ("tn_beta", "https://beta.example.test/update")):
        store = Store(db, tenant_id=tid)
        ok = mailer.send_dunning_email(invoice_id=f"in_{tid}", to="cust@mail.test", subject="s",
                                       body=f"Card: {url}", store=store)
        assert ok["status"] == mailer.STATUS_SENT, (tid, ok)
        store.close()
    assert len(sent) == 2
    assert sent[0]["key"] == "Bearer re_tn_alpha" and sent[1]["key"] == "Bearer re_tn_beta"
    # alpha's body quoting beta's link is refused by alpha's guard
    store = Store(db, tenant_id="tn_alpha")
    foreign = mailer.send_dunning_email(invoice_id="in_x", to="cust@mail.test", subject="s",
                                        body="Card: https://beta.example.test/update", store=store)
    store.close()
    assert foreign["status"] == mailer.STATUS_SUPPRESSED
    assert len(sent) == 2


# --- M1: startup hooks never stop the process booting -----------------------------------------

@pytest.mark.parametrize("error_name", ["LegacyLedgerError", "TenantMismatchError"])
def test_m1_single_mode_boots_and_health_reports_only_the_error_class(monkeypatch, error_name):
    import app.api as api_module
    import app.store as store_module

    error_cls = type(error_name, (RuntimeError,), {})

    def _broken(*_a, **_k):
        raise error_cls("the ledger file is not owned by this tenant: /secret/path.db")

    monkeypatch.delenv("PAYPILOT_TENANCY", raising=False)
    monkeypatch.setattr(store_module, "get_store", _broken)
    monkeypatch.setattr(api_module, "get_store", _broken)
    with TestClient(api_module.app) as client:  # startup runs here; it must not raise
        response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["reason"] == error_name
    assert "/secret/path.db" not in response.text and "Traceback" not in response.text


def test_m1_multi_mode_boots_and_health_is_ok(monkeypatch):
    import app.api as api_module

    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    with TestClient(api_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 200, response.text


# --- M2: an invalid LLM budget is a zero ceiling, never no ceiling ----------------------------

@pytest.mark.parametrize(("raw", "expected"), [("5O", 0.0), ("-1", 0.0), ("nan", 0.0), ("", None), ("5", 5.0)])
def test_m2_invalid_budget_is_zero_not_absent(monkeypatch, raw, expected):
    monkeypatch.setenv("PAYPILOT_LLM_BUDGET_USD", raw)
    assert config_for("default").llm_budget_usd == expected


def test_m2_a_zero_ceiling_refuses_spend():
    meter = CostMeter(budget_usd=0.0)
    state, _reason = meter.verdict()
    assert state != "no_budget", "a zero ceiling must not read as no ceiling"


# --- M3: a failure after the claim never leaves an approval approved --------------------------

def _pending(store, token: str) -> int:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    aid = store.create_approval(
        invoice_id="in_m3", channel="email", recipient="cust@mail.test", subject="Payment failed",
        body="Pay up", token_hash=approval.hash_token(token), expires_at=expires,
    )
    assert aid is not None
    return aid


def test_m3_a_send_exception_after_the_claim_resolves_the_approval_failed(monkeypatch):
    store = get_store()
    token = "tok_m3_send"
    aid = _pending(store, token)

    def _boom(**_k):
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(approval, "send_dunning_email", _boom)
    with pytest.raises(RuntimeError):
        approval.decide(aid, token, "approve", store)
    assert store.get_approval(aid)["status"] == approval.A_FAILED


def test_m3_multi_mode_unknown_tenant_mid_decision_is_not_left_approved(multi, monkeypatch):
    db = multi
    save_tenant_config(db, "tn_alpha", {"approval_mode": "email"}, {})
    store = Store(db, tenant_id="tn_alpha")
    token = "tok_m3_multi"
    aid = _pending(store, token)

    def _no_tenant(*_a, **_k):
        raise UnknownTenant("no ambient tenant")

    monkeypatch.setattr(approval, "_recheck_before_sending", _no_tenant)
    with pytest.raises(UnknownTenant):
        approval.decide(aid, token, "approve", store)
    status = store.get_approval(aid)["status"]
    assert status == approval.A_FAILED, status
    store.close()


# --- M4: the fixture refusal is logged once, and the demo tenant reads its fixtures -----------

def test_m4_fixtures_refused_outside_the_demo_tenant_are_logged_once(monkeypatch, caplog):
    monkeypatch.delenv("PAYPILOT_TENANT", raising=False)
    nodes._FIXTURE_REFUSAL_LOGGED["done"] = False
    with caplog.at_level(logging.WARNING, logger="paypilot"):
        assert nodes.load_customer_records() == []
        assert nodes.load_customer_records() == []
    notes = [r for r in caplog.records if "customer fixtures not read" in r.getMessage()]
    assert len(notes) == 1, [r.getMessage() for r in notes]


def test_m4_the_demo_tenant_reads_its_fixtures(monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    assert nodes.load_customer_records()


# --- M5: platform defaults where the row is silent; FROM stays tenant-level -------------------

def test_m5_platform_defaults_apply_when_the_row_is_silent_and_from_does_not(multi, monkeypatch):
    db = multi
    monkeypatch.setenv("RESEND_API_KEY", "re_platform")
    monkeypatch.setenv("PAYPILOT_MAX_TOUCHES", "5")
    monkeypatch.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "7")
    monkeypatch.setenv("PAYPILOT_FROM_EMAIL", "env@platform.test")
    save_tenant_config(db, "tn_m5", {"approval_mode": "off"}, {})
    cfg = config_for("tn_m5", db)
    assert cfg.resend_api_key == "re_platform"
    assert cfg.max_touches == 5 and cfg.send_cooldown_hours == 7.0
    assert cfg.from_address == DEFAULT_FROM, "the platform FROM leaked into a tenant"
    save_tenant_config(db, "tn_m5", {"max_touches": 2, "send_cooldown_hours": 1},
                       {"resend_api_key": "re_tenant"})
    over = config_for("tn_m5", db)
    assert over.resend_api_key == "re_tenant"
    assert over.max_touches == 2 and over.send_cooldown_hours == 1.0


# --- m1: one Gmail client per tenant and credential; superseded clients are closed ------------

def test_m1_gmail_clients_are_cached_per_tenant_and_credential_and_closed_when_superseded():
    gmail_drafts.set_client(None)
    gmail_drafts._clients.clear()
    cfg = TenantConfig(tenant_id="tn_g1", gmail_client_id="id1", gmail_client_secret="s1",
                       gmail_refresh_token="r1")
    first = gmail_drafts.get_client(config=cfg)
    assert gmail_drafts.get_client(config=cfg) is first, "a new client per call leaks sockets"
    rotated = dataclasses.replace(cfg, gmail_refresh_token="r2")
    second = gmail_drafts.get_client(config=rotated)
    assert second is not first
    assert first._http.is_closed, "the superseded client's HTTP session was not closed"
    other = TenantConfig(tenant_id="tn_g2", gmail_client_id="id1", gmail_client_secret="s1",
                         gmail_refresh_token="r1")
    assert gmail_drafts.get_client(config=other) is not second
    assert not second._http.is_closed, "another tenant's rotation closed this tenant's client"
    gmail_drafts._clients.clear()


# --- m2, m3, m4, m5 -----------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    ("993", 993), ("0", 0), ("+993", None), ("-1", None), ("5_0", None), ("", None),
    ("²", None),  # isdigit() True, but not an ASCII port: refused, never a ValueError
])
def test_m2_the_port_accepts_exactly_what_main_accepted(raw, expected):
    assert tc._port(raw) == expected


def test_m2_a_non_ascii_digit_port_does_not_raise(monkeypatch):
    monkeypatch.setenv("PAYPILOT_IMAP_PORT", "²")  # superscript two: isdigit() True, int() ValueError
    assert tc._port("²") is None
    assert config_for("default").imap_port is None


def test_m3_empty_and_null_row_values_fall_back_to_the_default_not_to_nothing(multi):
    db = multi
    save_tenant_config(db, "tn_m3", {"max_touches": None, "from_address": "",
                                     "holdout_seed": "   ", "send_cooldown_hours": "5"}, {})
    cfg = config_for("tn_m3", db)
    assert cfg.max_touches == 3
    assert cfg.from_address == DEFAULT_FROM
    assert cfg.holdout_seed == DEFAULT_HOLDOUT_SEED
    assert cfg.send_cooldown_hours == 5.0


def test_m4_saves_merge_and_platform_fields_survive_a_tenant_save(multi, monkeypatch):
    db = multi
    save_tenant_config(db, "tn_m4", {"max_touches": 2}, {"resend_api_key": "k1"})
    save_tenant_config(db, "tn_m4", {"from_address": "New <n@x.test>"}, {})
    set_platform_fields(db, "tn_m4", send_enabled=True, allowed_recipients=["a@x.test"])
    save_tenant_config(db, "tn_m4", {"business_name": "Biz"}, {})
    cfg = config_for("tn_m4", db)
    assert cfg.max_touches == 2, "a later save dropped an earlier field"
    assert cfg.resend_api_key == "k1", "a save without secrets dropped the stored secret"
    assert cfg.send_enabled is True and cfg.allowed_recipients == ("a@x.test",)


def test_m4_single_mode_row_lays_over_the_environment_field_by_field(tmp_path, monkeypatch):
    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_APPROVER_EMAILS", "env@x.test")
    monkeypatch.setenv("PAYPILOT_MAX_TOUCHES", "9")
    db = Database(tmp_path / "single.db", owner_tenant="default")
    save_tenant_config(db, "default", {"max_touches": 2}, {})
    cfg = config_for("default", db)
    assert cfg.max_touches == 2, "the row did not win over the environment"
    assert cfg.approver_emails == ("env@x.test",), "a field the row does not set lost its environment value"


def test_m5_the_meter_reads_the_ceiling_from_config_on_every_call():
    reset_meters()
    first = get_cost_meter(TenantConfig(tenant_id="tn_meter_m5", llm_budget_usd=1.0))
    second = get_cost_meter(TenantConfig(tenant_id="tn_meter_m5", llm_budget_usd=0.0))
    assert second is first
    assert second._budget == 0.0, "the meter kept the budget it was created with"
    reset_meters()


# --- counted guard: the remaining current_config() fallbacks are pinned, not open-ended -------

#: Every ``current_config()`` call that remains in app/, by (module, function). Each is an
#: optional ``config`` parameter whose tenant path passes the config explicitly; the count is
#: pinned, so a new fallback fails until someone decides it. The set may only shrink.
KNOWN_CURRENT_CONFIG_FALLBACKS = Counter({
    # The one explicit resolver: a run's held config, else the state view, else the single-mode
    # config (which raises in multi mode). Every node calls it; no guard falls back by itself.
    ("app/tenant_config.py", "run_config"): 1,
    # Single-mode only, by design: each reports the deployment's own state, and fails (503, or
    # "unavailable") in multi mode rather than resolving an ambient tenant.
    ("app/api.py", "health"): 1,
    ("app/api.py", "_inference_snapshot"): 1,
    ("app/meter.py", "restore_cost_from_audit"): 1,
    ("app/api.py", "_warn_default_update_url"): 1,
    ("app/approval.py", "approval_mode"): 1,
    ("app/approval.py", "approver_emails"): 1,
    ("app/approval.py", "ttl_hours"): 1,
    ("app/attribution.py", "holdout_pct"): 1,
    ("app/attribution.py", "holdout_seed"): 1,
    ("app/gmail_drafts.py", "_access_token"): 1,
    ("app/gmail_drafts.py", "configured"): 1,
    ("app/gmail_drafts.py", "get_client"): 1,
    ("app/imap_drafts.py", "_cfg"): 1,
    ("app/imap_drafts.py", "configured"): 1,
    ("app/loop.py", "ai_disclosure"): 1,
    ("app/loop.py", "max_touches"): 1,
    ("app/loop.py", "send_cooldown_hours"): 1,
    ("app/mailbox_approval.py", "grace_minutes"): 1,
    ("app/mailer.py", "allowed_recipients"): 1,
    ("app/mailer.py", "send_operator_alert"): 1,
    ("app/mailer.py", "sender"): 1,
    ("app/mailer.py", "sending_enabled"): 1,
    ("app/meter.py", "get_cost_meter"): 1,
    ("app/nodes.py", "_high_value_threshold"): 1,
    ("app/nodes.py", "llm_drafting_enabled"): 1,
    ("app/pii.py", "_pii_salt"): 1,
    ("app/safety.py", "allowed_link_hosts"): 1,
    ("app/safety.py", "payment_update_url"): 1,
    ("app/stripe_client.py", "_return_url"): 1,
    ("app/stripe_client.py", "api_key"): 1,
    ("app/tenant_config.py", "config_of"): 1,
})


def _current_config_sites(root: Path) -> Counter:
    from tests._ast_guards import current_config_sites_in

    counts: Counter = Counter()
    for path in sorted((root / "app").rglob("*.py")):
        counts += current_config_sites_in(
            path.read_text(encoding="utf-8"), path.relative_to(root).as_posix()
        )
    return counts


def test_the_current_config_fallbacks_are_exactly_the_pinned_set():
    observed = _current_config_sites(ROOT)
    added = {k: observed[k] - KNOWN_CURRENT_CONFIG_FALLBACKS[k]
             for k in observed if observed[k] > KNOWN_CURRENT_CONFIG_FALLBACKS[k]}
    gone = {k: KNOWN_CURRENT_CONFIG_FALLBACKS[k] - observed[k]
            for k in KNOWN_CURRENT_CONFIG_FALLBACKS if KNOWN_CURRENT_CONFIG_FALLBACKS[k] > observed[k]}
    assert added == {}, f"a new current_config() fallback: pass the config explicitly or pin it: {added}"
    assert gone == {}, f"a pinned fallback is gone: lower the pin: {gone}"


def test_the_current_config_guard_catches_a_planted_fallback(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "planted.py").write_text(
        "from app.tenant_config import current_config\n"
        "def tenant_path(store):\n"
        "    return current_config().resend_api_key\n",
        encoding="utf-8",
    )
    assert _current_config_sites(tmp_path) == Counter({("app/planted.py", "tenant_path"): 1})
