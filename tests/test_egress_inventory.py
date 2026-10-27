"""What PayPilot can reach, pinned.

A client's question is not "what does it do" but "what can it touch". This file answers it from the
code, and fails when the answer changes without somebody deciding it should.

* Every call that can leave the process (HTTP, IMAP, the Stripe and OpenAI SDKs) is listed by file.
  A new one in a new file fails until it is reviewed and added here.
* The Gmail surface is pinned to the exact methods and paths it uses. None of them sends mail.
* The IMAP surface is pinned to the verbs it uses. Nothing here sends mail either.
* The Stripe surface is one call, and it mints a link; it moves no money.
* With default settings a recovery run opens no socket at all.

Read docs/what-paypilot-touches.md for the client-facing version.
"""

from __future__ import annotations

import ast
import json
import socket
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

_PREFIXES = (
    "httpx.", "urllib.request.", "imaplib.", "stripe.", "smtplib.", "requests.", "socket.",
    "http.client.", "aiohttp.", "ftplib.", "paramiko.", "boto3.", "telnetlib.",
)
_BARE = {"ChatOpenAI", "OpenAI", "AsyncOpenAI", "Anthropic"}

#: file -> primitive -> count. Reviewed 2026-10-27 against the code at that date.
EXPECTED = {
    "app/bank/http.py": {"httpx.Client": 1, "httpx.URL": 1},  # URL is local parsing
    "app/gmail_drafts.py": {"httpx.Client": 1},
    "app/imap_drafts.py": {
        "imaplib.IMAP4": 1, "imaplib.IMAP4_SSL": 1,
        "imaplib.Internaldate2tuple": 1, "imaplib.Time2Internaldate": 1,  # local date parsing
    },
    "app/mailer.py": {"httpx.post": 1},
    "app/mercury_client.py": {"urllib.request.Request": 1, "urllib.request.urlopen": 1},
    "app/nodes.py": {"ChatOpenAI": 1},
    "app/stripe_client.py": {"stripe.billing_portal.Session.create": 1},
}

#: Hosts written as constants in app/. Four are egress; the rest are pages, fixtures or CSP entries.
EGRESS_HOSTS = {"api.resend.com", "oauth2.googleapis.com", "gmail.googleapis.com", "api.mercury.com"}
NON_EGRESS_HOSTS = {
    "aspsp.invalid", "bank.invalid",            # test doubles, reserved TLD
    "cdn.jsdelivr.net", "fastapi.tiangolo.com", # the docs page's own assets
    "paypilot.fly.dev",                         # the demo's public link
}
#: Reached through an SDK or a per-tenant setting, so the host is not a constant here.
IMPLICIT = {
    "api.stripe.com": "stripe SDK, one call: billing_portal.Session.create",
    "api.openai.com": "LangChain ChatOpenAI, only when LLM drafting is switched on",
    "<tenant imap host>": "IMAP APPEND/SEARCH/FETCH/STORE of PayPilot's own drafts",
    "<aspsp host>": "bank seam, read-only, off unless a provider is configured",
}


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _primitives(path: Path) -> Counter:
    found: Counter = Counter()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name and (name.startswith(_PREFIXES) or name in _BARE):
                found[name] += 1
    return found


def _scan() -> dict[str, dict[str, int]]:
    out = {}
    for path in sorted(APP.rglob("*.py")):
        found = _primitives(path)
        if found:
            out[path.relative_to(ROOT).as_posix()] = dict(found)
    return out


def test_every_outbound_call_site_in_app_is_the_reviewed_list():
    assert _scan() == EXPECTED, (
        "an outbound call site was added, removed or moved. Review what it can reach, update "
        "EXPECTED and docs/what-paypilot-touches.md together."
    )


def test_no_mail_library_or_raw_socket_is_imported_in_app():
    banned = {"smtplib", "socket", "ftplib", "telnetlib", "paramiko", "boto3", "requests", "aiohttp"}
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            assert not banned & set(names), (path.name, names)


def test_the_hosts_written_in_app_are_exactly_the_reviewed_set():
    import re

    seen: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(getattr(n.body[0], "value", None), ast.Constant)
        }
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
                seen |= set(re.findall(r"https?://([A-Za-z0-9.\-]+)", n.value))
    assert seen == EGRESS_HOSTS | NON_EGRESS_HOSTS, sorted(seen ^ (EGRESS_HOSTS | NON_EGRESS_HOSTS))


# -- Gmail: drafts only ------------------------------------------------------


def _gmail_calls() -> set[tuple[str, str]]:
    calls = set()
    tree = ast.parse((APP / "gmail_drafts.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted(node.func) == "self._call":
            method = node.args[0].value
            path = node.args[1]
            text = path.value if isinstance(path, ast.Constant) else ast.unparse(path).strip("f'\"")
            calls.add((method, text))
    return calls


def test_the_gmail_surface_is_these_five_calls_and_none_of_them_sends():
    calls = _gmail_calls()
    assert calls == {
        ("POST", "/drafts"),
        ("GET", "/drafts/{draft_id}"),
        ("DELETE", "/drafts/{draft_id}"),
        ("GET", "/messages"),
        ("GET", "/messages/{item['id']}"),
    }
    assert not any("send" in path.lower() for _, path in calls)
    src = (APP / "gmail_drafts.py").read_text(encoding="utf-8")
    assert "messages/send" not in src and "drafts/send" not in src


def test_the_gmail_scope_is_requested_nowhere_in_app():
    # The refresh token's scope is the client's to grant; the code must not ask for a send scope.
    for path in APP.rglob("*.py"):
        assert "gmail.send" not in path.read_text(encoding="utf-8"), path.name


# -- IMAP: the draft folder only --------------------------------------------


def test_the_imap_surface_is_a_small_fixed_set_of_verbs():
    tree = ast.parse((APP / "imap_drafts.py").read_text(encoding="utf-8"))
    verbs, uid_verbs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = _dotted(node.func.value)
            if base == "conn":
                verbs.add(node.func.attr)
                if node.func.attr == "uid" and isinstance(node.args[0], ast.Constant):
                    uid_verbs.add(node.args[0].value)
    assert verbs == {"append", "expunge", "login", "logout", "select", "starttls", "uid"}
    assert uid_verbs == {"FETCH", "SEARCH", "STORE"}


# -- Stripe: one call, no money ---------------------------------------------


def test_the_only_stripe_attribute_chain_in_app_is_the_billing_portal_session():
    chains = set()
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                name = _dotted(node)
                if name and name.split(".")[0] == "stripe" and name.count(".") >= 1:
                    chains.add(name)
    writes = {c for c in chains if c.endswith(("create", "modify", "delete", "pay", "refund", "capture"))}
    assert writes == {"stripe.billing_portal.Session.create"}, sorted(writes)
    assert not {c for c in chains if any(w in c.lower() for w in ("charge", "refund", "payout", "transfer", "paymentintent"))}


# -- default settings: no socket --------------------------------------------


def test_a_default_recovery_run_opens_no_network_connection(monkeypatch, isolated_store):
    """No keys, sending off: a signed Stripe event is processed without touching the network."""
    import hashlib
    import hmac
    import time

    from app.api import app

    def _refuse(*a, **k):
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr("httpx.post", _refuse)
    monkeypatch.delenv("PAYPILOT_SEND_EMAIL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_egress")

    event = {
        "id": "evt_egress", "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": "in_egress", "customer": "cus_X", "subscription": "sub_X",
            "customer_email": "owner@acme.test", "amount_due": 4900, "currency": "eur",
            "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }
    raw = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(b"whsec_egress", f"{ts}".encode() + b"." + raw, hashlib.sha256).hexdigest()
    r = TestClient(app).post("/webhooks/stripe", content=raw,
                             headers={"stripe-signature": f"t={ts},v1={sig}"})
    assert r.status_code == 200, r.text
    assert isolated_store.get_failure("in_egress") is not None


@pytest.mark.parametrize("name", ["PAYPILOT_SEND_EMAIL"])
def test_sending_is_off_unless_switched_on(monkeypatch, name):
    from app.mailer import sending_enabled
    from app.tenant_config import platform_config

    monkeypatch.delenv(name, raising=False)
    assert sending_enabled(platform_config()) is False


# -- the client sheet cannot drift from the inventory ------------------------


def test_the_client_sheet_names_every_egress_host_and_the_safe_claims():
    sheet = (ROOT / "docs" / "what-paypilot-touches.md").read_text(encoding="utf-8")
    for host in EGRESS_HOSTS | {"api.stripe.com", "api.openai.com"}:
        assert host in sheet, host
    for claim in ("sends nothing", "No send call exists", "Moves no money", "Off by default"):
        assert claim in sheet, claim
    assert chr(0x2014) not in sheet and chr(0x2013) not in sheet
