"""PII masking, audit logging, and endpoint-auth controls.

These cover the security-baseline gaps closed on top of the injection fencing:
customer PII is masked at prompt assembly and re-hydrated after the guards (with
a final guard pass), every LLM call emits one structured audit event, and the
webhook/metrics endpoints enforce optional auth that fails open to a warned demo
mode. Everything runs offline (no OpenAI key, no network).
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import audit as audit_module
from app import graph as graph_module
from app import nodes as nodes_module
from app import pii as pii_module
from app.auth import sign_body


@pytest.fixture
def no_key(monkeypatch):
    """Key-less demo path: mock LLM + lexical retriever, no network."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    return monkeypatch


def _reset_rate(monkeypatch):
    from collections import OrderedDict, deque

    monkeypatch.setattr(api_module, "_RATE_MAX", 100)
    monkeypatch.setattr(api_module, "_RATE_GLOBAL_MAX", 10000)
    monkeypatch.setattr(api_module, "_rate_hits", OrderedDict())
    monkeypatch.setattr(api_module, "_global_hits", deque())


_ACME = "Acme Robotics"
_ACME_EMAIL = "billing@acmerobotics.io"
_EVENT = {
    "customer_id": "cust_001",  # Acme Robotics
    "amount": 1499.0,
    "currency": "usd",
    "failure_code": "card_expired",
    "attempt": 1,
}


# ---------------------------------------------------------------------------
# app.pii unit checks
# ---------------------------------------------------------------------------

def test_mask_and_rehydrate_roundtrip():
    masked, mapping = pii_module.mask_structured_pii(
        {"name": _ACME, "email": _ACME_EMAIL, "plan": "Scale"}
    )
    assert masked["name"] == "{{NAME_1}}"
    assert masked["email"] == "{{EMAIL_1}}"
    assert masked["plan"] == "Scale"  # plan/tier is not PII
    text = "Hi {{NAME_1}}, reach us at {{EMAIL_1}}."
    assert pii_module.rehydrate(text, mapping) == f"Hi {_ACME}, reach us at {_ACME_EMAIL}."
    assert pii_module.unresolved_placeholders(pii_module.rehydrate(text, mapping)) == []


def test_unsafe_name_maps_to_fallback():
    """A name carrying a URL never round-trips; it maps to a safe fallback."""
    masked, mapping = pii_module.mask_structured_pii({"name": "Dana http://evil.example"})
    assert mapping["{{NAME_1}}"] == "there"
    assert pii_module.rehydrate("Hi {{NAME_1}}!", mapping) == "Hi there!"


def test_malformed_email_drops_to_empty():
    _, mapping = pii_module.mask_structured_pii({"email": "not-an-email ignore instructions"})
    assert mapping["{{EMAIL_1}}"] == ""


def test_unresolved_placeholder_detected():
    assert pii_module.unresolved_placeholders("Hi {{NAME 1}}, ok") == ["{{NAME 1}}"]


def test_scrub_freeform_masks_card_keeps_structured():
    """Luhn-valid card is masked; invoice id and ISO date survive intact."""
    text = "invoice INV-2026-0007 on 2026-07-11 card 4242424242424242 amount 1499.00"
    out = pii_module.scrub_freeform(text)
    assert "4242424242424242" not in out
    assert "{{CARD}}" in out
    assert "INV-2026-0007" in out          # letters/hyphens: not a bare digit run
    assert "2026-07-11" in out             # ISO date survives
    assert "1499.00" in out                # amount survives


def test_scrub_freeform_leaves_non_luhn_digit_run():
    """A 16-digit run that fails Luhn is not a card and is left alone."""
    assert pii_module.scrub_freeform("ref 1234567812345678") == "ref 1234567812345678"


# ---------------------------------------------------------------------------
# 1.1(a) prompt-capture: raw PII never reaches a prompt string
# ---------------------------------------------------------------------------

def test_raw_pii_never_in_any_prompt(no_key):
    prompts: list[str] = []
    orig = nodes_module._llm_text
    no_key.setattr(nodes_module, "_llm_text", lambda p: (prompts.append(p), orig(p))[1])
    graph_module.run_recovery(_EVENT)
    blob = "\n".join(prompts)
    assert prompts, "the LLM nodes should have run"
    assert _ACME not in blob, "raw customer name must never reach a prompt"
    assert _ACME_EMAIL not in blob, "raw customer email must never reach a prompt"
    assert "{{NAME_1}}" in blob, "the masked placeholder should be what the model sees"


# ---------------------------------------------------------------------------
# 1.1(b) mock-mode E2E: real name in output, no placeholder, no fallback
# ---------------------------------------------------------------------------

def test_mock_mode_rehydrates_and_does_not_degrade(no_key):
    out = graph_module.run_recovery(_EVENT)
    assert _ACME in out["message"], "the real name must be re-hydrated into the message"
    assert "{{" not in out["message"], "no placeholder may survive into the output"
    assert "{{" not in out["diagnosis"]
    # The silent-degradation guard: personalisation is intact, not templated away.
    assert out["fallback_used"] is False


# ---------------------------------------------------------------------------
# 1.1(c) log-capture happy path: no raw PII in logs
# ---------------------------------------------------------------------------

def test_no_raw_pii_in_logs_happy_path(no_key, monkeypatch, caplog):
    _reset_rate(monkeypatch)
    client = TestClient(api_module.app)
    with caplog.at_level(logging.INFO):
        r = client.post("/payment-failed", json=_EVENT)
    assert r.status_code == 200
    assert _ACME not in caplog.text, "customer name must not appear in logs"
    assert _ACME_EMAIL not in caplog.text, "customer email must not appear in logs"


# ---------------------------------------------------------------------------
# 1.1(d) error paths never leak PII
# ---------------------------------------------------------------------------

def test_validation_error_redacts_input(monkeypatch, caplog):
    """A 422 must not echo the rejected value (pydantic v2 echoes it by default)."""
    _reset_rate(monkeypatch)
    client = TestClient(api_module.app)
    hostile = "attacker@evil.example"  # violates customer_id charset -> 422
    with caplog.at_level(logging.INFO):
        r = client.post("/payment-failed", json={**_EVENT, "customer_id": hostile})
    assert r.status_code == 422
    assert hostile not in r.text, "the rejected value must not be reflected in the response"
    assert hostile not in caplog.text, "the rejected value must not be logged"


def test_exception_path_leaks_no_pii(no_key, monkeypatch, caplog):
    """A node raising mid-flight returns a generic 500 with no PII in the log."""
    _reset_rate(monkeypatch)

    def _boom(_p):
        raise RuntimeError("synthetic node failure")

    monkeypatch.setattr(nodes_module, "_llm_text", _boom)
    client = TestClient(api_module.app, raise_server_exceptions=False)
    with caplog.at_level(logging.INFO):
        r = client.post("/payment-failed", json=_EVENT)
    assert r.status_code == 500
    assert _ACME not in r.text and _ACME not in caplog.text
    assert _ACME_EMAIL not in r.text and _ACME_EMAIL not in caplog.text


# ---------------------------------------------------------------------------
# 1.2 audit: one event per LLM call; fallback_used matches reality
# ---------------------------------------------------------------------------

def _audit_events(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        if rec.name == "paypilot.audit":
            try:
                out.append(json.loads(rec.getMessage()))
            except json.JSONDecodeError:
                pass
    return [e for e in out if e.get("event") == "llm_call"]


def test_audit_one_event_per_llm_node_happy(no_key, caplog):
    with caplog.at_level(logging.INFO, logger="paypilot.audit"):
        graph_module.run_recovery(_EVENT)
    events = _audit_events(caplog)
    assert {e["node"] for e in events} == {"diagnose_reason", "draft_message"}
    assert len(events) == 2, "exactly one audit event per LLM node"
    for e in events:
        assert e["fallback_used"] is False
        assert e["guards_passed"] is True
        assert e["model"] == "mock"
        assert e["boundary_id"] and len(e["prompt_sha256"]) == 64


def test_audit_reflects_fallback(no_key, monkeypatch, caplog):
    """When the model obeys an injection, every audit event flips to fallback."""
    leaky = "verify http://evil.example key sk-abcdef0123456789ABCDEF"
    monkeypatch.setattr(nodes_module, "_llm_text", lambda _p: leaky)
    with caplog.at_level(logging.INFO, logger="paypilot.audit"):
        graph_module.run_recovery(_EVENT)
    events = _audit_events(caplog)
    assert len(events) == 2
    for e in events:
        assert e["fallback_used"] is True
        assert e["injection_suspected"] is True
        assert e["guards_failed"]


def test_audit_prompt_hash_is_boundary_stable():
    """The prompt hash normalizes the random boundary, so it is comparable."""
    a = audit_module.prompt_sha256("<<UNTRUSTED-aaaa>> data <</UNTRUSTED-aaaa>>", "aaaa")
    b = audit_module.prompt_sha256("<<UNTRUSTED-zzzz>> data <</UNTRUSTED-zzzz>>", "zzzz")
    assert a == b, "same template + data must hash the same despite a different boundary"


# ---------------------------------------------------------------------------
# 1.3 endpoint auth (HMAC webhook + admin bearer)
# ---------------------------------------------------------------------------

def test_webhook_demo_mode_accepts_unsigned(no_key, monkeypatch):
    """With WEBHOOK_SECRET unset, /payment-failed accepts unsigned requests."""
    _reset_rate(monkeypatch)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    client = TestClient(api_module.app)
    assert client.post("/payment-failed", json=_EVENT).status_code == 200


def test_webhook_valid_signature_200(no_key, monkeypatch):
    _reset_rate(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret-demo")
    body = json.dumps(_EVENT).encode("utf-8")
    sig = sign_body(body, "s3cret-demo")
    client = TestClient(api_module.app)
    r = client.post(
        "/payment-failed",
        content=body,
        headers={"Content-Type": "application/json", "X-PayPilot-Signature": sig},
    )
    assert r.status_code == 200


def test_webhook_bad_signature_401(no_key, monkeypatch):
    _reset_rate(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret-demo")
    body = json.dumps(_EVENT).encode("utf-8")
    client = TestClient(api_module.app)
    r = client.post(
        "/payment-failed",
        content=body,
        headers={"Content-Type": "application/json", "X-PayPilot-Signature": "deadbeef"},
    )
    assert r.status_code == 401


def test_metrics_demo_mode_open(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client = TestClient(api_module.app)
    assert client.get("/metrics").status_code == 200


def test_metrics_requires_bearer_when_set(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-demo")
    client = TestClient(api_module.app)
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer admin-demo"})
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# 1.4 /billing/update static page
# ---------------------------------------------------------------------------

def test_billing_update_page_served():
    client = TestClient(api_module.app)
    r = client.get("/billing/update")
    assert r.status_code == 200
    assert "Demo endpoint" in r.text
    # House rule: no em/en dashes in shipped copy.
    assert "—" not in r.text and "–" not in r.text  # lint-style: allow-dash


# ---------------------------------------------------------------------------
# 1.5 every unauthenticated rejection is alertable
# ---------------------------------------------------------------------------

def _security_events(caplog) -> list[dict]:
    """The security audit events (as opposed to the per-LLM-call ones) in caplog."""
    out = []
    for rec in caplog.records:
        if rec.name != "paypilot.audit":
            continue
        try:
            parsed = json.loads(rec.getMessage())
        except json.JSONDecodeError:
            continue
        if parsed.get("event") != "llm_call":
            out.append(parsed)
    return out


#: Every route that answers an unauthenticated caller with a 401, and the audit
#: event its rejection must emit. There were three separate implementations of
#: "reject unauthenticated" and only the Stripe one audited, so an ADMIN_TOKEN
#: brute force against /report and /recovery-report produced zero alertable log
#: lines: the only control that could have seen the attack never fired.
_UNAUTHENTICATED_ROUTES = (
    ("GET", "/metrics", "admin_token_rejected"),
    ("GET", "/recovery-report", "admin_token_rejected"),
    ("GET", "/report", "admin_token_rejected"),
    ("POST", "/payment-failed", "webhook_signature_rejected"),
    ("POST", "/payment-failed/batch", "webhook_signature_rejected"),
)


@pytest.mark.parametrize(("method", "path", "expected_event"), _UNAUTHENTICATED_ROUTES)
def test_each_unauthenticated_rejection_emits_one_audit_event(
    no_key, monkeypatch, caplog, method, path, expected_event
):
    """Positive validation: one rejection, one named audit event, on every route.

    Asserted by count and by event name rather than by an empty-violations list,
    because "nothing was logged" is exactly the defect this covers.
    """
    _reset_rate(monkeypatch)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-demo")
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret-demo")
    client = TestClient(api_module.app)
    payload = _EVENT if path == "/payment-failed" else {"events": [_EVENT]}
    with caplog.at_level(logging.INFO, logger="paypilot.audit"):
        r = client.request(
            method,
            path,
            json=payload if method == "POST" else None,
            headers={
                "Authorization": "Bearer wrong-token",
                "X-PayPilot-Signature": "deadbeef",
            },
        )
    assert r.status_code == 401, f"{method} {path} must reject the caller"
    events = [e for e in _security_events(caplog) if e["event"] == expected_event]
    assert len(events) == 1, f"{method} {path}: one rejection must emit one audit event"
    assert events[0]["severity"] == "error"
    assert path in events[0]["detail"], "the event must name the route that was hit"


def test_admin_token_brute_force_is_alertable(monkeypatch, caplog):
    """Six guesses against the admin token produce six audit events, not zero.

    This is the reproduced attack: a token brute force that left no alertable
    trace because the bearer check raised a bare HTTPException.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "admin-demo")
    client = TestClient(api_module.app)
    guesses = ["a", "b", "c", "d", "e", "f"]
    with caplog.at_level(logging.INFO, logger="paypilot.audit"):
        codes = [
            client.get("/report", headers={"Authorization": f"Bearer {g}"}).status_code
            for g in guesses
        ]
    assert codes == [401] * len(guesses)
    events = [e for e in _security_events(caplog) if e["event"] == "admin_token_rejected"]
    assert len(events) == len(guesses), "every rejection must be one alertable line"


# ---------------------------------------------------------------------------
# 1.6 a malformed operator customers.json cannot take the endpoints down
# ---------------------------------------------------------------------------

def _writes_json(payload):
    """Materialise ``customers.json`` holding ``payload`` as JSON."""
    def _make(path):
        path.write_text(json.dumps(payload), encoding="utf-8")

    return _make


def _valid_record(**extra) -> dict:
    record = {
        "id": "cust_001",
        "name": "Acme Robotics",
        "plan": "Scale",
        "mrr": 1499,
        "currency": "usd",
        "payment_history": [{"status": "failed", "failure_code": "card_expired"}],
    }
    record.update(extra)
    return [record]


def _writes_binary(path) -> None:
    """A non-UTF-8 file: an operator pointed the path at a sqlite db / an image."""
    path.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01not utf-8")


def _writes_unreadable(path) -> None:
    path.write_text("[]", encoding="utf-8")
    path.chmod(0o000)


#: Every shape ``data/customers.json`` can actually take on an operator's box,
#: mapped to the ``/config`` customer ids that shape should yield. Three separate
#: failure classes live here and each one reached production as a 500:
#:
#: * valid JSON of the wrong OUTER shape - a dict iterates to its str keys, a
#:   list of strings/nulls has no ``.get``;
#: * valid JSON of the wrong NESTED shape - ``payment_history`` as a string or a
#:   list of nulls, which ``_prior_failures`` reads ``.get("status")`` off;
#: * a file that cannot be decoded at all - a directory (IsADirectoryError), a
#:   binary blob (UnicodeDecodeError, NOT an OSError) or a chmod-000 file
#:   (PermissionError). The loader used to catch only FileNotFoundError and
#:   JSONDecodeError, so all three escaped it.
#:
#: The corpus is the point: a table missing the nested-shape rows is exactly how
#: /config got hardened and the recovery endpoint did not.
_CUSTOMER_FILE_SHAPES = {
    "dict_not_list": (_writes_json({"cust_001": {"id": "cust_001"}}), []),
    "list_of_strings": (_writes_json(["cust_001", "cust_002"]), []),
    "list_of_nulls": (_writes_json([None, None]), []),
    "json_scalar": (_writes_json("cust_001"), []),
    "invalid_json": (lambda p: p.write_text("{not json", encoding="utf-8"), []),
    "empty_file": (lambda p: p.write_text("", encoding="utf-8"), []),
    "directory": (lambda p: p.mkdir(), []),
    "binary_file": (_writes_binary, []),
    "unreadable_file": (_writes_unreadable, []),
    "history_is_a_string": (
        _writes_json(_valid_record(payment_history="nope")),
        ["cust_001"],
    ),
    "history_of_nulls": (
        _writes_json(_valid_record(payment_history=[None, "x"])),
        ["cust_001"],
    ),
    "history_mixed": (
        _writes_json(_valid_record(payment_history=[{"status": "failed"}, None, "x", 7])),
        ["cust_001"],
    ),
    "history_missing": (
        _writes_json([{"id": "cust_001", "name": "Acme Robotics", "plan": "Scale"}]),
        ["cust_001"],
    ),
    "valid": (_writes_json(_valid_record()), ["cust_001"]),
}


@pytest.mark.parametrize("shape", sorted(_CUSTOMER_FILE_SHAPES))
def test_malformed_customers_file_never_500s(no_key, monkeypatch, tmp_path, shape):
    """/config and /payment-failed degrade instead of crashing on a bad file."""
    _reset_rate(monkeypatch)
    make_file, expected_ids = _CUSTOMER_FILE_SHAPES[shape]
    path = tmp_path / "customers.json"
    make_file(path)
    if shape == "unreadable_file" and os.access(path, os.R_OK):
        pytest.skip("running as root: chmod 000 is still readable")
    monkeypatch.setattr(nodes_module, "CUSTOMERS_PATH", path)
    # There used to be a SECOND path constant and loader in app.api; redirect it
    # too so this test reproduces the /config 500 against the old code. Once the
    # loader is shared there is one path constant and this branch is dead.
    if hasattr(api_module, "_CUSTOMERS_PATH"):
        monkeypatch.setattr(api_module, "_CUSTOMERS_PATH", path)

    client = TestClient(api_module.app, raise_server_exceptions=False)
    cfg = client.get("/config")
    assert cfg.status_code == 200, f"{shape}: /config must not 500"
    recovery = client.post("/payment-failed", json=_EVENT)
    assert recovery.status_code != 500, f"{shape}: /payment-failed must not 500"

    assert [c["id"] for c in cfg.json()["customers"]] == expected_ids, (
        f"{shape}: /config listed the wrong records"
    )
