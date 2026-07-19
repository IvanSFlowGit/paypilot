"""Tests for the end-to-end demo script.

The script itself needs a real Stripe test key, so CI cannot run it for real.
What CI can do - and what matters most - is prove the two things that would be
expensive to get wrong:

* it refuses to run against a live key, because it creates subscriptions and
  charges customers, and
* the orchestration actually drives an invoice from failed to recovered,
  verified against a fake Stripe that returns Stripe-shaped objects.

The fake is deliberately thin. It is not trying to be Stripe; it is trying to
prove the script calls the right things in the right order and feeds the right
events into the loop.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "demo_loop.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("demo_loop", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo():
    return _load_script()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("STRIPE_API_KEY", "STRIPE_SECRET_KEY", "PAYPILOT_SEND_EMAIL",
                "PAYPILOT_ALLOWED_RECIPIENTS", "RESEND_API_KEY", "OPENAI_API_KEY",
                "PAYPILOT_HOLDOUT_PCT"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# The live-key guard
# ---------------------------------------------------------------------------

def test_refuses_a_live_key(demo, monkeypatch):
    """The guard that stops this from billing real people."""
    monkeypatch.setenv("STRIPE_API_KEY", "sk_live_pretend")
    with pytest.raises(SystemExit) as exc:
        demo._require_test_mode()
    assert exc.value.code == 1


def test_refuses_a_live_restricted_key(demo, monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "rk_live_pretend")
    with pytest.raises(SystemExit):
        demo._require_test_mode()


def test_refuses_a_missing_key(demo):
    with pytest.raises(SystemExit):
        demo._require_test_mode()


def test_accepts_a_test_key(demo, monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_pretend")
    stripe = demo._require_test_mode()
    assert stripe.api_key == "sk_test_pretend"


def test_guard_checks_the_key_not_an_env_name(demo, monkeypatch):
    """A key prefix is verifiable; an env var called "staging" is a promise."""
    monkeypatch.setenv("PAYPILOT_ENV", "staging")
    monkeypatch.setenv("STRIPE_API_KEY", "sk_live_pretend")
    with pytest.raises(SystemExit):
        demo._require_test_mode()


# ---------------------------------------------------------------------------
# The orchestration, against a fake Stripe
# ---------------------------------------------------------------------------

class _Obj(dict):
    """Dict that also allows attribute access, like Stripe's objects."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _Listing:
    def __init__(self, items):
        self._items = items

    def auto_paging_iter(self):
        return iter(self._items)


def _fake_stripe(state):
    """Minimal Stripe stand-in covering exactly what the script touches."""
    invoice_id = "in_demo_1"
    customer_id = "cus_demo_1"

    def _invoice(status, amount_paid=0):
        return _Obj(
            object="invoice", id=invoice_id, customer=customer_id,
            subscription="sub_demo_1", customer_email="demo@mine.test",
            amount_due=4900, amount_paid=amount_paid, currency="eur",
            attempt_count=1, status=status,
            hosted_invoice_url="https://invoice.stripe.com/i/acct_1/demo",
            payment_intent={"last_payment_error": {"decline_code": "expired_card"}},
            metadata={"paypilot_customer_id": "cust_001"},
        )

    def _event(event_type, invoice):
        return {"id": f"evt_{event_type}", "type": event_type,
                "data": {"object": dict(invoice)}}

    stripe = types.SimpleNamespace()
    stripe.api_key = None

    clock = _Obj(id="clock_1", status="ready")
    stripe.test_helpers = types.SimpleNamespace(
        TestClock=types.SimpleNamespace(
            create=lambda **kw: clock,
            advance=lambda cid, **kw: state.setdefault("advanced", True),
            retrieve=lambda cid: clock,
        )
    )
    stripe.Customer = types.SimpleNamespace(
        create=lambda **kw: _Obj(id=customer_id, **kw),
        modify=lambda cid, **kw: state.setdefault("card_swapped", True),
    )
    stripe.Price = types.SimpleNamespace(create=lambda **kw: _Obj(id="price_1"))
    stripe.Subscription = types.SimpleNamespace(
        create=lambda **kw: _Obj(id="sub_demo_1", status="past_due")
    )
    stripe.PaymentMethod = types.SimpleNamespace(
        attach=lambda pm, **kw: state.setdefault("attached", pm)
    )
    stripe.Invoice = types.SimpleNamespace(
        list=lambda **kw: _Listing([_invoice("open")]),
        pay=lambda iid: _invoice("paid", amount_paid=4900),
    )

    def _event_list(type, limit=25):
        invoice = _invoice("paid", 4900) if type == "invoice.paid" else _invoice("open")
        return _Listing([_event(type, invoice)])

    stripe.Event = types.SimpleNamespace(list=_event_list)
    return stripe


def test_full_cycle_drives_an_invoice_to_recovered(demo, tmp_path, monkeypatch, capsys):
    state: dict = {}
    monkeypatch.setattr(demo, "_require_test_mode", lambda: _fake_stripe(state))
    monkeypatch.setattr(sys, "argv", [
        "demo_loop.py", "--email", "demo@mine.test",
        "--db", str(tmp_path / "demo.db"),
    ])

    exit_code = demo.main()
    out = capsys.readouterr().out

    assert exit_code == 0, "the cycle must end with a recovery"
    assert state.get("advanced") is True, "the clock must actually be advanced"
    assert state.get("attached") == demo.CARD_SUCCEEDS, "the card must be swapped"
    assert "invoices recovered this run: 1" in out
    assert "EUR recovered: 49.00" in out


def test_the_cycle_writes_to_a_separate_ledger(demo, tmp_path, monkeypatch):
    """A demo run must never mix its invoices into the real recovery numbers."""
    db = tmp_path / "demo.db"
    monkeypatch.setattr(demo, "_require_test_mode", lambda: _fake_stripe({}))
    monkeypatch.setattr(sys, "argv", [
        "demo_loop.py", "--email", "demo@mine.test", "--db", str(db),
    ])
    demo.main()

    from app.store import Store

    store = Store(db)
    try:
        rows = store.list_failures()
        assert len(rows) == 1 and rows[0]["state"] == "recovered"
        assert rows[0]["recovered_amount_minor"] == 4900
    finally:
        store.close()


def test_missing_email_is_refused(demo, monkeypatch):
    monkeypatch.setattr(demo, "_require_test_mode", lambda: _fake_stripe({}))
    monkeypatch.setattr(sys, "argv", ["demo_loop.py"])
    with pytest.raises(SystemExit):
        demo.main()


def test_it_uses_payment_method_tokens_not_card_numbers():
    """The product never handles card data; a demo that did would contradict it.

    Checked against the parsed AST, not the raw text: naming a test card in a
    docstring is documentation, whereas putting one in a string the code
    actually evaluates would mean the script is handling a PAN.
    """
    import ast

    source = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Docstrings are documentation, so exclude them from the executable set.
    # clean=False: the default runs cleandoc(), which reindents the text so it
    # no longer matches the raw constant it came from.
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    live_strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]

    assert any("pm_card_" in s for s in live_strings), "must use PaymentMethod tokens"
    for pan in ("4000000000000341", "4242424242424242"):
        offenders = [s for s in live_strings if pan in s]
        assert not offenders, f"raw card number {pan} in evaluated code: {offenders}"
