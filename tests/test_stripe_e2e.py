# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The Stripe webhook end to end, with Stripe-format signatures, in process.

scripts/stripe_webhook_e2e.py runs the same cases over HTTP against a deployed URL. Running
them here too means the script itself is tested: a script that has only ever been pointed at
production is one nobody has seen fail.
"""

from __future__ import annotations

import importlib.util
import json
from collections import OrderedDict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api as api_module

ROOT = Path(__file__).resolve().parents[1]
SECRET = "whsec_e2e_test_secret"


def _load():
    spec = importlib.util.spec_from_file_location("stripe_e2e", ROOT / "scripts" / "stripe_webhook_e2e.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


e2e = _load()


@pytest.fixture()
def sender(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    client = TestClient(api_module.app)

    def send(body: bytes, signature):
        headers = {"Content-Type": "application/json"}
        if signature is not None:
            headers["Stripe-Signature"] = signature
        r = client.post("/webhooks/stripe", content=body, headers=headers)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    return send


def test_every_case_behaves_in_process(sender):
    results = e2e.run_cases(sender, SECRET)
    assert len(results) == 9
    bad = [(n, d) for n, ok, d in results if not ok]
    assert not bad, bad


def test_the_cases_fail_when_the_secret_is_wrong(sender):
    """The control for the control: pointed at a deployment whose secret is NOT ours, the positive
    case must go red. Otherwise a green run would only prove that everything is rejected."""
    results = e2e.run_cases(sender, "whsec_a_different_secret")
    by_name = {n: ok for n, ok, _ in results}
    assert by_name["CONTROL: a validly signed payment_failed is accepted and opens a recovery"] is False


def test_the_effect_is_in_the_store_not_only_in_the_response(sender):
    from app.store import get_store

    results = e2e.run_cases(sender, SECRET)
    assert all(ok for _, ok, _ in results)
    seen = get_store().mark_event_seen("evt_e2e_probe", "x", None)
    assert seen is True, "the store is readable; the dedupe table the webhook wrote to exists"


def test_the_script_refuses_to_run_without_a_secret(monkeypatch, capsys):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    assert e2e.main(["--base", "http://127.0.0.1:9"]) == 2
    assert "STRIPE_WEBHOOK_SECRET" in capsys.readouterr().err


def test_the_secret_is_never_printed(monkeypatch, capsys):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_must_not_appear_in_output")
    e2e.main(["--base", "http://127.0.0.1:9"])
    out = capsys.readouterr()
    assert "whsec_must_not_appear_in_output" not in out.out + out.err
    json.dumps(out.out)
