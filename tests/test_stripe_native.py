"""PayPilot works on what Stripe's native recovery hands back, not against it.

Two behaviours, each pinned in both directions:

* Stripe's per-charge ``advice_code`` outranks the decline-code map. When the
  issuer says ``do_not_try_again`` the failure becomes ``issuer_do_not_retry``,
  the strategy asks for a new payment method, and the customer copy never
  promises a retry on that card.
* Retry timing belongs to Stripe. When the event carries Stripe's own
  ``next_payment_attempt``, the schedule reports it unchanged with
  ``source="stripe"``; the rules-table cadence is only ever returned labelled
  as a suggestion.

All offline: mock mode, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import decision, stripe_map
from app import nodes as nodes_module


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    return monkeypatch


def _invoice(decline="insufficient_funds", advice=None, where="payment_intent",
             next_attempt=None):
    err = {"decline_code": decline}
    obj = {
        "object": "invoice",
        "id": "in_native_1",
        "customer": "cus_N",
        "subscription": "sub_N",
        "amount_due": 4900,
        "currency": "eur",
        "attempt_count": 1,
        "payment_intent": {"last_payment_error": err},
        "metadata": {"paypilot_customer_id": "cust_001"},
    }
    if advice and where == "payment_intent":
        err["advice_code"] = advice
    if advice and where == "charge":
        obj["charge"] = {"outcome": {"advice_code": advice}}
    if next_attempt is not None:
        obj["next_payment_attempt"] = next_attempt
    return {"id": "evt_native", "type": "invoice.payment_failed", "data": {"object": obj}}


# ---- advice_code -------------------------------------------------------------

@pytest.mark.parametrize("where", ["payment_intent", "charge"])
def test_do_not_try_again_overrides_the_decline_code(where):
    event = _invoice(decline="insufficient_funds", advice="do_not_try_again", where=where)
    assert stripe_map.stripe_event_to_internal(event)["failure_code"] == "issuer_do_not_retry"
    assert stripe_map.stripe_event_to_failure(event)["failure_code"] == "issuer_do_not_retry"


def test_no_advice_code_leaves_the_decline_map_in_charge():
    # Control: the override must not fire on an ordinary decline.
    event = _invoice(decline="insufficient_funds")
    internal = stripe_map.stripe_event_to_internal(event)
    assert internal["failure_code"] == "insufficient_funds"
    assert internal["advice_code"] is None


def test_try_again_later_advice_does_not_override():
    event = _invoice(decline="expired_card", advice="try_again_later")
    internal = stripe_map.stripe_event_to_internal(event)
    assert internal["failure_code"] == "card_expired"
    assert internal["advice_code"] == "try_again_later"


def test_do_not_retry_strategy_schedules_no_retry_even_when_escalated():
    for escalate in (False, True):
        strategy = decision.strategy_for("issuer_do_not_retry", escalate=escalate)
        assert strategy["action"] == "request_new_payment_method"
        # The generic escalation floors retry_in_days at 1; here that would
        # invent a retry the issuer has refused.
        assert strategy["retry_in_days"] == 0
        assert strategy["escalated"] is escalate


# ---- next_payment_attempt ----------------------------------------------------

def test_stripe_next_attempt_parses_unix_seconds():
    ts = 1_790_000_000
    assert stripe_map.stripe_next_attempt({"next_payment_attempt": ts}) == (
        datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")
    )


@pytest.mark.parametrize("value", [None, 0, -5, True, "1790000000"])
def test_stripe_next_attempt_absent_or_malformed_is_none(value):
    assert stripe_map.stripe_next_attempt({"next_payment_attempt": value}) is None


def test_schedule_reports_stripes_time_unchanged():
    when = (datetime.now(UTC) + timedelta(days=4, hours=3)).replace(microsecond=0)
    state = {
        "event": {"stripe_next_payment_attempt": when.isoformat(timespec="seconds")},
        "strategy": {"action": "wait_and_retry", "retry_in_days": 3},
    }
    schedule = nodes_module.schedule_retry(state)["schedule"]
    assert schedule["source"] == "stripe"
    assert datetime.fromisoformat(schedule["next_retry_at"]) == when
    # The rules table said 3 days; Stripe said 4 and a bit. Stripe wins.
    assert schedule["retry_in_days"] == 5


def test_schedule_without_stripe_time_is_labelled_a_suggestion():
    state = {"event": {}, "strategy": {"action": "wait_and_retry", "retry_in_days": 3}}
    schedule = nodes_module.schedule_retry(state)["schedule"]
    assert schedule["source"] == "paypilot_suggestion"
    assert schedule["retry_in_days"] == 3


def test_do_not_retry_schedule_is_empty_even_if_stripe_sent_a_time():
    later = (datetime.now(UTC) + timedelta(days=2)).isoformat(timespec="seconds")
    state = {
        "event": {"stripe_next_payment_attempt": later},
        "strategy": {"action": "request_new_payment_method", "retry_in_days": 0},
    }
    schedule = nodes_module.schedule_retry(state)["schedule"]
    assert schedule == {
        "retry_in_days": 0, "next_retry_at": None, "retry_on": None,
        "timezone": "UTC", "source": "none",
    }


# ---- end to end --------------------------------------------------------------

_RETRY_PROMISES = ("we'll retry", "we will retry", "retry in a few", "automatically retry")


def test_webhook_event_with_do_not_try_again_never_promises_a_retry(no_key):
    from app.graph import run_recovery

    event = stripe_map.stripe_event_to_internal(
        _invoice(decline="insufficient_funds", advice="do_not_try_again",
                 next_attempt=int(datetime.now(UTC).timestamp()) + 86400)
    )
    out = run_recovery(event)
    assert out["strategy"]["action"] == "request_new_payment_method"
    assert out["schedule"]["source"] == "none"
    lowered = out["message"].lower()
    assert "different card" in lowered
    assert not any(p in lowered for p in _RETRY_PROMISES), out["message"]


def test_control_insufficient_funds_copy_does_promise_a_retry(no_key):
    # Control for the test above: the phrase check must be able to fire.
    from app.graph import run_recovery

    out = run_recovery(stripe_map.stripe_event_to_internal(_invoice()))
    assert any(p in out["message"].lower() for p in _RETRY_PROMISES), out["message"]


def test_endpoint_serialises_a_null_retry_time(no_key):
    response = TestClient(api_module.app).post(
        "/payment-failed",
        json={"customer_id": "cust_001", "amount": 49.0, "currency": "eur",
              "failure_code": "issuer_do_not_retry", "attempt": 1},
    )
    assert response.status_code == 200, response.text
    schedule = response.json()["schedule"]
    assert schedule["source"] == "none"
    assert schedule["next_retry_at"] is None
