"""Tests for the closed recovery loop: Stripe events to recorded outcomes.

The expensive failures here are attribution failures, not crashes: counting an
invoice that never failed as a recovery, churning an invoice that was already
paid, or letting a redelivered event move the ledger twice. Those are what
these cover.

Offline throughout - mock LLM, no network. The autouse fixture in conftest.py
gives each test its own database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import loop
from app.store import (
    STATE_CHURNED,
    STATE_FAILED,
    STATE_MESSAGED,
    STATE_RECOVERED,
)


@pytest.fixture
def no_key(monkeypatch):
    """Force the keyless demo path so run_recovery works offline."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    return monkeypatch


def _failed_event(
    invoice_id="in_1",
    customer="cust_001",
    stripe_customer="cus_XYZ",
    subscription="sub_XYZ",
    amount_due=4900,
    attempt=1,
    event_id="evt_failed_1",
):
    return {
        "id": event_id,
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "object": "invoice",
                "id": invoice_id,
                "customer": stripe_customer,
                "subscription": subscription,
                "amount_due": amount_due,
                "currency": "eur",
                "attempt_count": attempt,
                "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
                "metadata": {"paypilot_customer_id": customer},
            }
        },
    }


def _paid_event(
    invoice_id="in_1",
    amount_paid=4900,
    etype="invoice.paid",
    event_id="evt_paid_1",
    stripe_customer="cus_XYZ",
    subscription="sub_XYZ",
):
    return {
        "id": event_id,
        "type": etype,
        "data": {
            "object": {
                "object": "invoice",
                "id": invoice_id,
                "customer": stripe_customer,
                "subscription": subscription,
                "amount_paid": amount_paid,
                "amount_due": 4900,
                "currency": "eur",
            }
        },
    }


def _churn_event(subscription="sub_XYZ", stripe_customer="cus_XYZ", event_id="evt_churn_1"):
    return {
        "id": event_id,
        "type": "customer.subscription.deleted",
        "data": {"object": {"object": "subscription", "id": subscription,
                            "customer": stripe_customer}},
    }


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    signed = f"{ts}".encode() + b"." + payload
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


# ---------------------------------------------------------------------------
# Opening the loop
# ---------------------------------------------------------------------------

def test_payment_failed_persists_the_invoice(no_key, isolated_store):
    result = loop.handle_payment_failed(_failed_event(), isolated_store)
    assert result["handled"] is True

    row = isolated_store.get_failure("in_1")
    assert row["state"] == STATE_FAILED
    assert row["amount_minor"] == 4900          # minor units, not 49.0
    assert row["currency"] == "eur"
    assert row["stripe_customer_id"] == "cus_XYZ"
    assert row["subscription_id"] == "sub_XYZ"
    assert row["customer_id"] == "cust_001"     # metadata hint kept for the graph


def test_payment_failed_still_returns_a_recovery_draft(no_key, isolated_store):
    result = loop.handle_payment_failed(_failed_event(), isolated_store)
    assert set(result["recovery"]) == {
        "diagnosis", "risk", "strategy", "schedule", "message", "impact", "fallback_used"
    }


def test_payment_failed_without_an_invoice_id_is_refused(no_key, isolated_store):
    """A failure with no invoice id could never be closed, so it would sit in
    the denominator of every recovery rate forever."""
    event = _failed_event(invoice_id="")
    result = loop.handle_payment_failed(event, isolated_store)
    assert result["handled"] is False
    assert result["reason"] == "missing_invoice_id"
    assert isolated_store.list_failures() == []


def test_repeat_failure_of_one_invoice_stays_one_row(no_key, isolated_store):
    loop.handle_payment_failed(_failed_event(attempt=1), isolated_store)
    loop.handle_payment_failed(_failed_event(attempt=2), isolated_store)
    assert len(isolated_store.list_failures()) == 1
    assert isolated_store.get_failure("in_1")["attempt_count"] == 2


# ---------------------------------------------------------------------------
# Closing as recovered
# ---------------------------------------------------------------------------

def test_invoice_paid_closes_a_tracked_failure(no_key, isolated_store):
    loop.handle_payment_failed(_failed_event(), isolated_store)
    result = loop.handle_recovery(_paid_event(), isolated_store)

    assert result["handled"] is True and result["changed"] is True
    row = isolated_store.get_failure("in_1")
    assert row["state"] == STATE_RECOVERED
    assert row["recovered_amount_minor"] == 4900
    assert row["recovered_at"]


def test_payment_succeeded_closes_it_too(no_key, isolated_store):
    loop.handle_payment_failed(_failed_event(), isolated_store)
    result = loop.handle_recovery(
        _paid_event(etype="invoice.payment_succeeded"), isolated_store
    )
    assert result["handled"] is True
    assert isolated_store.get_failure("in_1")["state"] == STATE_RECOVERED


def test_paid_invoice_we_never_saw_fail_is_not_a_recovery(no_key, isolated_store):
    """Most invoices in an account are paid without ever failing. Counting them
    would inflate the one number the product is judged on."""
    result = loop.handle_recovery(_paid_event(invoice_id="in_never_failed"), isolated_store)
    assert result["handled"] is False
    assert result["reason"] == "no_matching_failure"
    assert isolated_store.list_failures() == []


def test_partial_payment_records_what_arrived_not_what_was_billed(no_key, isolated_store):
    loop.handle_payment_failed(_failed_event(amount_due=4900), isolated_store)
    loop.handle_recovery(_paid_event(amount_paid=2000), isolated_store)
    assert isolated_store.get_failure("in_1")["recovered_amount_minor"] == 2000


def test_redelivered_paid_event_does_not_recover_twice(no_key, isolated_store):
    loop.handle_payment_failed(_failed_event(), isolated_store)
    first = loop.handle_recovery(_paid_event(), isolated_store)
    second = loop.handle_recovery(_paid_event(), isolated_store)
    assert first["changed"] is True
    assert second["changed"] is False  # idempotent, not a second recovery


# ---------------------------------------------------------------------------
# Closing as churn
# ---------------------------------------------------------------------------

def test_subscription_deleted_churns_open_invoices(no_key, isolated_store):
    loop.handle_payment_failed(_failed_event(invoice_id="in_1"), isolated_store)
    loop.handle_payment_failed(
        _failed_event(invoice_id="in_2", event_id="evt_failed_2"), isolated_store
    )

    result = loop.handle_churn(_churn_event(), isolated_store)
    assert result["handled"] is True
    assert sorted(result["churned_invoice_ids"]) == ["in_1", "in_2"]
    assert isolated_store.get_failure("in_1")["state"] == STATE_CHURNED
    assert isolated_store.get_failure("in_2")["state"] == STATE_CHURNED


def test_churn_leaves_an_already_recovered_invoice_alone(no_key, isolated_store):
    """Paid in March, cancelled in June: the recovery still happened, and
    rewriting it as churn would delete a real win."""
    loop.handle_payment_failed(_failed_event(), isolated_store)
    loop.handle_recovery(_paid_event(), isolated_store)

    result = loop.handle_churn(_churn_event(), isolated_store)
    assert result["handled"] is False
    assert result["reason"] == "no_open_failures"
    assert isolated_store.get_failure("in_1")["state"] == STATE_RECOVERED


def test_churn_is_scoped_to_the_cancelled_subscription(no_key, isolated_store):
    """A customer with two subscriptions: cancelling one must not churn the
    invoices belonging to the other."""
    loop.handle_payment_failed(
        _failed_event(invoice_id="in_a", subscription="sub_A"), isolated_store
    )
    loop.handle_payment_failed(
        _failed_event(invoice_id="in_b", subscription="sub_B", event_id="evt_failed_2"),
        isolated_store,
    )

    loop.handle_churn(_churn_event(subscription="sub_A"), isolated_store)
    assert isolated_store.get_failure("in_a")["state"] == STATE_CHURNED
    assert isolated_store.get_failure("in_b")["state"] == STATE_FAILED


def test_churn_falls_back_to_the_customer_when_no_subscription_matches(no_key, isolated_store):
    """Stripe does not always expand ``subscription`` on the invoice."""
    loop.handle_payment_failed(_failed_event(subscription=None), isolated_store)
    result = loop.handle_churn(_churn_event(subscription="sub_unknown"), isolated_store)
    assert result["handled"] is True
    assert isolated_store.get_failure("in_1")["state"] == STATE_CHURNED


def test_churn_with_no_open_invoices_is_acknowledged(no_key, isolated_store):
    result = loop.handle_churn(_churn_event(), isolated_store)
    assert result["handled"] is False
    assert result["reason"] == "no_open_failures"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_dispatch_routes_each_handled_type(no_key, isolated_store):
    assert loop.handle_event(_failed_event(), isolated_store)["handled"] is True
    assert loop.handle_event(_paid_event(), isolated_store)["handled"] is True


def test_dispatch_acknowledges_an_unhandled_type(no_key, isolated_store):
    result = loop.handle_event({"type": "customer.created"}, isolated_store)
    assert result["handled"] is False
    assert result["reason"] == "unhandled_event_type"


def test_handled_event_types_covers_the_four_loop_events():
    """The exact set to subscribe a Stripe destination to."""
    assert loop.HANDLED_EVENT_TYPES == {
        "invoice.payment_failed",
        "invoice.paid",
        "invoice.payment_succeeded",
        "customer.subscription.deleted",
    }


# ---------------------------------------------------------------------------
# HTTP surface: signatures fail closed
# ---------------------------------------------------------------------------

def test_invalid_signature_is_rejected_and_audited(no_key, monkeypatch, caplog):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    client = TestClient(api_module.app)
    payload = json.dumps(_failed_event()).encode()

    with caplog.at_level("ERROR"):
        r = client.post(
            "/webhooks/stripe", content=payload,
            headers={"stripe-signature": "t=1,v1=deadbeef"},
        )
    assert r.status_code == 400
    assert any("webhook_signature_rejected" in m for m in caplog.messages)


def test_unsigned_webhooks_are_rejected_by_default(no_key, monkeypatch, caplog):
    """The default must fail closed.

    This was opt-in: verification was skipped unless a secret was set or
    PAYPILOT_ENV happened to read exactly "production". So the documented
    default let an unauthenticated POST forge a recovery, choose the amount and
    pick the recipient. An opt-in security control is not a security control.
    """
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    client = TestClient(api_module.app)

    with caplog.at_level("ERROR"):
        r = client.post("/webhooks/stripe", json=_failed_event())
    assert r.status_code == 400
    assert any("webhook_secret_missing" in m for m in caplog.messages)


def test_a_forged_recovery_cannot_move_the_ledger(no_key, monkeypatch, isolated_store):
    """The attack the new default blocks: fabricated recovered revenue."""
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    client = TestClient(api_module.app)

    assert client.post("/webhooks/stripe", json=_failed_event()).status_code == 400
    assert client.post("/webhooks/stripe", json=_paid_event()).status_code == 400
    assert isolated_store.list_failures() == []


def test_unsigned_events_need_an_explicit_opt_out(no_key, monkeypatch):
    """The public demo path: deliberate, single-purpose, written down."""
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "1")
    client = TestClient(api_module.app)
    assert client.post("/webhooks/stripe", json=_failed_event()).status_code == 200


# ---------------------------------------------------------------------------
# HTTP surface: the full loop
# ---------------------------------------------------------------------------

def test_full_loop_over_http(no_key, monkeypatch, isolated_store):
    """failed -> paid, end to end through the endpoint."""
    from collections import OrderedDict

    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "1")
    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())
    client = TestClient(api_module.app)

    opened = client.post("/webhooks/stripe", json=_failed_event())
    assert opened.status_code == 200 and opened.json()["handled"] is True
    assert isolated_store.get_failure("in_1")["state"] == STATE_FAILED

    closed = client.post("/webhooks/stripe", json=_paid_event())
    assert closed.status_code == 200 and closed.json()["handled"] is True
    row = isolated_store.get_failure("in_1")
    assert row["state"] == STATE_RECOVERED
    assert row["recovered_amount_minor"] == 4900


def test_durable_dedupe_survives_a_lost_in_process_cache(no_key, monkeypatch, isolated_store):
    """The restart case. The in-memory replay cache is gone, but the events
    table remembers, so a redelivery cannot move the ledger a second time."""
    from collections import OrderedDict

    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "1")
    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())
    client = TestClient(api_module.app)

    client.post("/webhooks/stripe", json=_failed_event())
    isolated_store.transition("in_1", STATE_MESSAGED, reason="test")

    # Simulate the restart: the process cache is empty, the database is not.
    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())
    replay = client.post("/webhooks/stripe", json=_failed_event())

    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    # State untouched by the replay.
    assert isolated_store.get_failure("in_1")["state"] == STATE_MESSAGED
