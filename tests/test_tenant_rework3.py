"""M3 rework cycle 3. Each finding has a test that fails on the code before its fix.

Offline. Fake transports; no network. Realistic events: a payment_failed that carries the
customer's name and a plan line item, as a live Stripe invoice does.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import approval, loop, mailer
from app.store import Database, Store, get_store
from app.tenant_config import TENANT_KEY_ENV, save_tenant_config, set_platform_fields


def _realistic_event(invoice_id: str, customer: str = "cus_shared") -> dict:
    """A live-shaped invoice.payment_failed: a customer name and a plan line item."""
    return {
        "id": f"evt_{invoice_id}",
        "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": customer,
            "subscription": "sub_shared", "customer_email": "cust@mail.test",
            "customer_name": "Acme Trading Ltd",
            "amount_due": 4900, "currency": "eur", "attempt_count": 1,
            "lines": {"data": [{"description": "Pro plan (annual)"}]},
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


class _Resp:
    status_code = 200
    content = b'{"id": "msg_f1"}'

    def json(self):
        return {"id": "msg_f1"}


def _multi_two_tenants(tmp_path, monkeypatch):
    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    for name in ("PAYPILOT_UPDATE_URL", "RESEND_API_KEY", "PAYPILOT_SEND_EMAIL",
                 "PAYPILOT_ALLOWED_RECIPIENTS", "PAYPILOT_BUSINESS_NAME"):
        monkeypatch.delenv(name, raising=False)
    sent: list = []

    def _post(url, json=None, headers=None, timeout=None):  # noqa: A002
        sent.append({"to": (json or {}).get("to"), "key": (headers or {}).get("Authorization")})
        return _Resp()

    monkeypatch.setattr(mailer.httpx, "post", _post)
    db = Database(tmp_path / "f1.db", owner_tenant="tn_alpha")
    tenants = {
        "tn_alpha": ("https://alpha.example.test/update", "re_alpha"),
        "tn_beta": ("https://beta.example.test/update", "re_beta"),
    }
    for tid, (url, key) in tenants.items():
        save_tenant_config(db, tid, {"approval_mode": "off", "update_url": url,
                                     "business_name": f"{tid} billing"}, {"resend_api_key": key})
        set_platform_fields(db, tid, send_enabled=True, allowed_recipients=["cust@mail.test"])
    return db, sent


def test_f1_a_realistic_payment_failed_does_not_crash_in_multi_mode_for_either_tenant(tmp_path, monkeypatch):
    """Red on the pre-fix code: the name and plan reach the output guard with no config, which
    resolves the ambient tenant, which raises UnknownTenant in multi mode (a webhook 500)."""
    db, sent = _multi_two_tenants(tmp_path, monkeypatch)
    for tid in ("tn_alpha", "tn_beta"):
        store = Store(db, tenant_id=tid)
        result = loop.handle_payment_failed(_realistic_event(f"in_f1_{tid}"), store)
        store.close()
        assert result["handled"] is True, (tid, result)
    assert {m["key"] for m in sent} == {"Bearer re_alpha", "Bearer re_beta"}, sent


# --- F2: the model path meters against the tenant's own budget ---------------------------------

class FakeChat:
    """A stand-in for the real chat model. Records every construction and every prompt."""

    constructed: list = []
    prompts: list = []

    def __init__(self, model=None, temperature=None, **_kw):
        FakeChat.constructed.append(model)

    def invoke(self, message):
        FakeChat.prompts.append(message)

        class _Out:
            content = "Your card did not go through. Please update your payment details."

        return _Out()


@pytest.fixture
def fake_chat(monkeypatch):
    from app import nodes

    FakeChat.constructed.clear()
    FakeChat.prompts.clear()
    monkeypatch.setattr(nodes, "ChatOpenAI", FakeChat)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    return FakeChat


def test_f2_a_multi_mode_tenant_with_drafting_on_reaches_the_model_on_its_own_budget(
        tmp_path, monkeypatch, fake_chat):
    """Red on the pre-fix code: get_llm called get_cost_meter() with no config, which resolves the
    ambient tenant and raises UnknownTenant in multi mode, so the webhook failed."""
    db, _sent = _multi_two_tenants(tmp_path, monkeypatch)
    set_platform_fields(db, "tn_alpha", send_enabled=True, allowed_recipients=["cust@mail.test"],
                        llm_draft=True, llm_budget_usd=5.0)
    store = Store(db, tenant_id="tn_alpha")
    result = loop.handle_payment_failed(_realistic_event("in_f2_alpha"), store)
    store.close()
    assert result["handled"] is True, result
    assert fake_chat.constructed, "the tenant's drafting switch was on, but no model was used"


# --- F3: a zero ceiling refuses inference, priced or not ---------------------------------------

@pytest.mark.parametrize("raw", ["5O", "-1", "nan", "0"])
def test_f3_a_zero_ceiling_refuses_the_model_even_for_an_unpriced_call(tmp_path, monkeypatch, fake_chat, raw):
    from app.meter import CostMeter

    meter = CostMeter(budget_usd=0.0)
    meter.record("model-without-a-sourced-price", 10, 10)  # unpriced: cost cannot be measured
    assert meter.allows_inference() is False, meter.verdict()

    db, _sent = _multi_two_tenants(tmp_path, monkeypatch)
    set_platform_fields(db, "tn_alpha", send_enabled=True, allowed_recipients=["cust@mail.test"],
                        llm_draft=True, llm_budget_usd=raw)
    store = Store(db, tenant_id="tn_alpha")
    result = loop.handle_payment_failed(_realistic_event(f"in_f3_{abs(hash(raw))}"), store)
    store.close()
    assert result["handled"] is True, result
    assert fake_chat.constructed == [] and fake_chat.prompts == [], "a model was called on a zero ceiling"


# --- F4: /health reports a config that cannot be built --------------------------------------

def test_f4_health_is_503_when_the_single_mode_config_cannot_be_built(monkeypatch):
    import app.api as api_module

    monkeypatch.delenv("PAYPILOT_TENANCY", raising=False)
    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    db = get_store().database
    save_tenant_config(db, "default", {"approval_mode": "off"}, {"resend_api_key": "re_row_secret"})
    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))  # rotated, not the key that wrote it
    with TestClient(api_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 503, response.text
    assert response.json()["reason"] == "TenantSecretError", response.text


# --- F5: the demo deployment never sends and creates no approvals ---------------------------

def test_f5_a_demo_deployment_never_sends_and_creates_no_approvals(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")
    monkeypatch.setenv("RESEND_API_KEY", "re_demo_would_send")
    posted: list = []
    monkeypatch.setattr(mailer.httpx, "post", lambda *a, **k: posted.append(a) or _Resp())
    store = Store(Database(tmp_path / "demo.db", owner_tenant="demo"), tenant_id="demo")
    loop.handle_payment_failed(_realistic_event("in_f5_demo"), store)
    assert posted == [], "the demo deployment sent mail"
    assert store.approvals_for("in_f5_demo") == [], "the demo deployment created an approval"


# --- F7: a tenant save cannot undo a concurrent operator change ----------------------------

def test_f7_a_tenant_save_cannot_undo_a_concurrent_kill_switch(tmp_path, monkeypatch):
    import threading

    from app import tenant_config as tc
    from app.tenant_config import config_for, set_platform_fields

    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    db = Database(tmp_path / "f7.db", owner_tenant="tn_race")
    save_tenant_config(db, "tn_race", {"business_name": "Before"}, {})
    set_platform_fields(db, "tn_race", send_enabled=True, allowed_recipients=["a@x.test"])

    paused, gate = threading.Event(), threading.Event()
    real_load = tc._load_row

    def _pausing_load(d, tid):
        row = real_load(d, tid)
        if threading.current_thread().name == "tenant-saver":
            paused.set()  # the save has read its copy of the row and is about to write
            gate.wait(5)
        return row

    monkeypatch.setattr(tc, "_load_row", _pausing_load)
    saver = threading.Thread(target=save_tenant_config,
                             args=(db, "tn_race", {"business_name": "After"}, {}), name="tenant-saver")
    saver.start()
    paused.wait(2)
    set_platform_fields(db, "tn_race", send_enabled=False, allowed_recipients=["a@x.test"])  # the kill switch
    gate.set()
    saver.join(5)
    assert config_for("tn_race", db).send_enabled is False, "a tenant save undid the operator's kill switch"


# --- F8: a failure after a real send never marks the approval failed ----------------------

def _pending(store, token: str) -> int:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    aid = store.create_approval(
        invoice_id="in_f8", channel="email", recipient="cust@mail.test", subject="Payment failed",
        body="Pay up", token_hash=approval.hash_token(token), expires_at=expires,
    )
    assert aid is not None
    return aid


def test_f8_a_failure_after_a_real_send_does_not_mark_the_approval_failed(monkeypatch):
    from app import approval

    store = get_store()
    token = "tok_f8"
    aid = _pending(store, token)
    monkeypatch.setattr(approval, "send_dunning_email", lambda **_k: {
        "status": mailer.STATUS_SENT, "provider_message_id": "m_f8", "error": None, "message_id": 1})

    def _transition_fails(*_a, **_k):
        raise RuntimeError("transition failed after the mail left")

    monkeypatch.setattr(store, "transition", _transition_fails)
    with pytest.raises(RuntimeError):
        approval.decide(aid, token, "approve", store)
    assert store.get_approval(aid)["status"] == approval.A_SENT, "a delivered mail was recorded as failed"


# --- F9: the row and the environment read a value the same way ---------------------------

def test_f9_a_stored_imap_security_is_lowercased_as_the_environment_is(tmp_path, monkeypatch):
    from app.tenant_config import config_for

    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    db = Database(tmp_path / "f9.db", owner_tenant="tn_f9")
    save_tenant_config(db, "tn_f9", {"imap_security": "SSL"}, {})
    assert config_for("tn_f9", db).imap_security == "ssl"


def test_f9_a_link_host_list_of_only_separators_allows_no_host_as_main_does(monkeypatch):
    from app import safety
    from app.tenant_config import config_for

    monkeypatch.setenv("PAYPILOT_ALLOWED_LINK_HOSTS", ", ,")
    assert safety.allowed_link_hosts(config_for("default")) == ()


def test_f9_a_set_but_empty_update_url_stays_empty_as_main_does(monkeypatch):
    from app.tenant_config import config_for

    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "")
    assert config_for("default").update_url == ""


# --- F10: LLM spend and sender identity are platform decisions ---------------------------

def test_f10_a_tenant_cannot_switch_on_model_drafting_or_set_its_budget(tmp_path, monkeypatch):
    from app.tenant_config import TenantConfigError

    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    db = Database(tmp_path / "f10.db", owner_tenant="tn_f10")
    with pytest.raises(TenantConfigError):
        save_tenant_config(db, "tn_f10", {"llm_draft": True}, {})
    with pytest.raises(TenantConfigError):
        save_tenant_config(db, "tn_f10", {"llm_budget_usd": 1000}, {})


def test_f10_a_from_address_is_honoured_only_inside_the_platform_allowed_domains(tmp_path, monkeypatch):
    from app.tenant_config import DEFAULT_FROM, config_for, set_platform_fields

    monkeypatch.setenv(TENANT_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    db = Database(tmp_path / "f10b.db", owner_tenant="tn_a")
    save_tenant_config(db, "tn_a", {}, {})
    save_tenant_config(db, "tn_b", {}, {})
    set_platform_fields(db, "tn_a", send_enabled=True, allowed_recipients=[], allowed_from_domains=["alpha.test"])
    set_platform_fields(db, "tn_b", send_enabled=True, allowed_recipients=[], allowed_from_domains=["beta.test"])

    save_tenant_config(db, "tn_a", {"from_address": "Alpha <bill@alpha.test>"}, {})
    assert config_for("tn_a", db).from_address == "Alpha <bill@alpha.test>"

    save_tenant_config(db, "tn_a", {"from_address": "Impostor <bill@beta.test>"}, {})
    assert config_for("tn_a", db).from_address == DEFAULT_FROM, "tenant A sent as tenant B's domain"


# --- F12: a normal multi-mode boot is quiet ----------------------------------------------

def test_f12_a_normal_multi_mode_boot_logs_no_startup_error(monkeypatch, caplog):
    import app.api as api_module

    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    with caplog.at_level(logging.ERROR):
        with TestClient(api_module.app) as client:
            client.get("/health")
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "startup_step_failed" in r.getMessage()]
    assert errors == [], [r.getMessage() for r in errors]


# --- F12: a draft in flight survives a credential rotation -----------------------------------

class _R:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_f12_an_in_flight_draft_survives_a_credential_rotation():
    import dataclasses
    import threading

    from app import gmail_drafts
    from app.tenant_config import TenantConfig

    gmail_drafts.set_client(None)
    gmail_drafts._clients.clear()
    cfg = TenantConfig(tenant_id="tn_f12", gmail_client_id="id", gmail_client_secret="s",
                       gmail_refresh_token="r1")
    started, release = threading.Event(), threading.Event()

    class _Http:
        is_closed = False

        def post(self, url, data=None, **_kw):
            return _R(200, {"access_token": "tok", "expires_in": 3600})

        def request(self, method, url, headers=None, **_kw):
            started.set()
            release.wait(5)
            if self.is_closed:
                raise RuntimeError("client closed while the request was in flight")
            return _R(200, {"id": "draft_f12"})

        def close(self):
            self.is_closed = True

    client = gmail_drafts.GmailClient(http=_Http(), config=cfg)
    gmail_drafts._clients[("tn_f12", gmail_drafts._fingerprint(cfg))] = client
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(id=client.create_draft("raw")))
    worker.start()
    started.wait(2)
    gmail_drafts.get_client(config=dataclasses.replace(cfg, gmail_refresh_token="r2"))  # rotation
    release.set()
    worker.join(5)
    assert result.get("id") == "draft_f12", result
    gmail_drafts._clients.clear()


# --- F6: no output guard, and no config lookup, runs without the bound tenant's config ------

import ast  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: The guard functions, and where their config sits: (keyword name, positional index or None).
#: A call must pass the config by keyword, or positionally at that index, and never as a literal
#: None or as a raw ``state.get(...)`` lookup, which is None on a run with no tenant in state.
GUARD_SIGNATURES = {
    "message_violations": (None,),
    "find_foreign_urls": (None,),
    "name_is_safe": (None,),
    "remask_text": (None,),
    "mask_structured_pii": (1,),
    "payment_update_url": (0,),
    "allowed_link_hosts": (0,),
    "get_cost_meter": (0,),
}


def _callee(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _config_violations(tree: ast.AST, where: str) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _callee(node) in GUARD_SIGNATURES):
            continue
        (position,) = GUARD_SIGNATURES[_callee(node)]
        value = next((k.value for k in node.keywords if k.arg == "config"), None)
        if value is None and position is not None and len(node.args) > position:
            value = node.args[position]
        name = f"{where}:{node.lineno} {_callee(node)}"
        if value is None:
            found.append(f"{name} passes no config")
        elif isinstance(value, ast.Constant) and value.value is None:
            found.append(f"{name} passes a literal None")
        elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
              and value.func.attr == "get"):
            found.append(f"{name} passes a raw state lookup; resolve it with run_config()")
    return found


def test_f6_every_guard_call_in_the_codebase_names_its_config():
    found = []
    for folder in ("app", "scripts", "evals"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            found += _config_violations(ast.parse(path.read_text(encoding="utf-8")), rel)
    assert found == [], found


def test_f6_the_guard_flags_each_planted_form():
    planted = ast.parse(
        "message_violations(x)\n"                                  # no config
        "find_foreign_urls(x, config=None)\n"                      # literal None
        "payment_update_url()\n"                                   # no config
        "payment_update_url(state.get('tenant_config'))\n"         # raw state lookup
        "get_cost_meter()\n"                                       # no config
        "name_is_safe(x, config=cfg)\n"                            # fine
        "payment_update_url(cfg)\n"                                # fine
        "mask_structured_pii(fields, cfg)\n"                       # fine, positional
    )
    flagged = _config_violations(planted, "planted.py")
    assert len(flagged) == 5, flagged
    assert not any(":8 " in f for f in flagged), "a compliant call was flagged"


# --- F11: the canary sends without a ledger; the eval guard honours the env link ---------

def test_f11_the_canary_alert_does_not_open_the_ledger(monkeypatch):
    """A legacy or foreign ledger must not stop an operator alert that supplies its own key and
    sender. The canary needs only the environment."""
    from app import mailer as mailer_module
    from app import tenant_config as tc_module

    def _ledger_refused():
        raise RuntimeError("ledger is not a PayPilot ledger for this tenant")

    monkeypatch.setattr(tc_module, "current_config", _ledger_refused)
    monkeypatch.setattr(mailer_module, "current_config", _ledger_refused)  # the name the mailer imported
    sent: list = []
    monkeypatch.setattr(mailer_module.httpx, "post", lambda url, json=None, headers=None, timeout=None:
                        sent.append(json) or _Resp())
    result = mailer_module.send_operator_alert(
        to="ops@canary.test", subject="canary", body="No links here.",
        api_key="re_canary", sender_address="Canary <alerts@canary.test>")
    assert result["status"] == mailer_module.STATUS_SENT, result
    assert len(sent) == 1


def test_f11_the_eval_guard_accepts_the_deployments_own_card_update_link(monkeypatch):
    """The eval must judge the link the deployment actually mints (PAYPILOT_UPDATE_URL), not the
    built-in constant: otherwise the eval rejects the operator's own link."""
    from evals.evalkit.guardrails import no_foreign_urls

    monkeypatch.setenv("PAYPILOT_UPDATE_URL", "https://pay.env.example/card")
    _name, check = no_foreign_urls()
    assert check("Update here: https://pay.env.example/card", {}) is None
    assert check("Visit https://evil.test/steal", {}) is not None
