"""PayPilot on Recharge failed charges, pinned in both directions.

* Every error type Recharge publishes gets a decision; an unknown one does not
  get emailed about.
* Non-payment errors (inventory, shipping, test mode) never reach a customer.
* Recharge owns the retry schedule: its ``retry_date`` is reported with
  ``source="recharge"`` when it will retry, and no retry is promised when it
  will not.
* The signature is checked exactly as Recharge documents it.

All offline: mock mode, no network.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import recharge_map as rm

SECRET = "test-client-secret"


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setenv("RECHARGE_CLIENT_SECRET", SECRET)
    api_module._idem_store.clear()
    return monkeypatch


def _charge(error_type="CARD_DECLINED", retry_date=None, tries=2, charge_id=5501,
            nested_customer=False):
    charge = {
        "id": charge_id,
        "error_type": error_type,
        "error": "Card was declined",
        "status": "ERROR",
        "number_times_tried": tries,
        "retry_date": retry_date,
        "total_price": "29.00",
        "currency": "USD",
        "first_name": "Dana",
        "last_name": "Fox",
        "line_items": [{"title": "Coffee Club Monthly"}],
    }
    if nested_customer:
        charge["customer"] = {"id": 777, "email": "dana@example.test"}
    else:
        charge["customer_id"] = 777
        charge["email"] = "dana@example.test"
    return {"charge": charge}


def _post(body, secret=SECRET, sign=True):
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if sign:
        headers["X-Recharge-Hmac-Sha256"] = hashlib.sha256(secret.encode() + raw).hexdigest()
    return TestClient(api_module.app).post("/webhooks/recharge", content=raw, headers=headers)


# ---- the table ---------------------------------------------------------------

def test_every_published_error_type_has_a_decision():
    from app import templates

    codes = set(templates.failure_codes())
    assert len(rm.PUBLISHED_ERROR_TYPES) == 109
    for error_type in rm.PUBLISHED_ERROR_TYPES:
        decision = rm.classify_error_type(error_type)
        assert decision == rm.NOT_CUSTOMER or decision in codes, (error_type, decision)


def test_unknown_error_type_is_unmapped_not_a_guess():
    assert rm.classify_error_type("SOMETHING_RECHARGE_ADDS_LATER") == rm.UNMAPPED
    assert rm.classify_error_type(None) == rm.UNMAPPED
    assert rm.classify_error_type("") == rm.UNMAPPED


@pytest.mark.parametrize("error_type, expected", [
    ("CARD_EXPIRED", "card_expired"),
    ("INSUFFICIENT_FUNDS", "insufficient_funds"),
    ("INCORRECT_ZIP", "card_details_invalid"),
    ("DO_NOT_HONOR", "issuer_do_not_retry"),
    ("CUSTOMER_NEEDS_TO_UPDATE_CARD", "issuer_do_not_retry"),
    ("CLOSED_MAX_RETRIES_REACHED", "retries_exhausted"),
    ("CARD_DECLINED", "generic_decline"),
    ("VARIANT_DOES_NOT_EXIST", rm.NOT_CUSTOMER),
    ("CARD_UPDATED_NOW_PENDING_NEXT_ATTEMPT", rm.NOT_CUSTOMER),
    ("TEST_MODE", rm.NOT_CUSTOMER),
])
def test_classification(error_type, expected):
    assert rm.classify_error_type(error_type) == expected
    assert rm.classify_error_type(error_type.lower()) == expected


# ---- signature ---------------------------------------------------------------

def test_signature_is_sha256_of_secret_then_body():
    body = b'{"charge": {"id": 1}}'
    good = hashlib.sha256(SECRET.encode() + body).hexdigest()
    assert rm.verify_recharge_signature(body, good, SECRET)
    assert rm.verify_recharge_signature(body, good.upper(), SECRET)
    # The documented order matters: body-then-secret must not validate.
    wrong_order = hashlib.sha256(body + SECRET.encode()).hexdigest()
    assert not rm.verify_recharge_signature(body, wrong_order, SECRET)
    assert not rm.verify_recharge_signature(body + b" ", good, SECRET)
    assert not rm.verify_recharge_signature(body, "", SECRET)
    assert not rm.verify_recharge_signature(body, good, "")


# ---- translation -------------------------------------------------------------

@pytest.mark.parametrize("nested", [False, True])
def test_both_customer_shapes_translate(nested):
    out = rm.recharge_charge_to_internal(_charge(nested_customer=nested))["event"]
    assert out["customer_id"] == "777"
    assert out["customer_email"] == "dana@example.test"
    assert out["customer_name"] == "Dana Fox"
    assert out["plan"] == "Coffee Club Monthly"
    assert out["amount"] == 29.0
    assert out["currency"] == "usd"
    assert out["attempt"] == 2


def test_retry_date_only_kept_when_recharge_will_retry():
    when = "2026-10-05T00:00:00"
    retried = rm.recharge_charge_to_internal(_charge("CARD_DECLINED", retry_date=when))
    assert retried["event"]["recharge_retry_date"] == "2026-10-05T00:00:00+00:00"
    hard = rm.recharge_charge_to_internal(_charge("DO_NOT_HONOR", retry_date=when))
    assert hard["event"]["recharge_retry_date"] is None


# ---- the endpoint ------------------------------------------------------------

def test_retried_decline_reports_recharges_schedule(no_key):
    when = (datetime.now(UTC) + timedelta(days=4)).replace(microsecond=0)
    response = _post(_charge("CARD_DECLINED", retry_date=when.strftime("%Y-%m-%dT%H:%M:%S")))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["handled"] is True
    schedule = body["recovery"]["schedule"]
    assert schedule["source"] == "recharge"
    assert schedule["next_retry_at"] == when.isoformat()


def test_control_without_retry_date_is_a_labelled_suggestion(no_key):
    # Control for the test above: the recharge source must not be the default.
    body = _post(_charge("CARD_DECLINED", retry_date=None)).json()
    assert body["recovery"]["schedule"]["source"] == "paypilot_suggestion"


def test_max_retries_asks_for_a_new_method_and_promises_nothing(no_key):
    body = _post(_charge("CLOSED_MAX_RETRIES_REACHED", tries=8)).json()
    recovery = body["recovery"]
    assert recovery["strategy"]["action"] == "request_new_payment_method"
    assert recovery["schedule"]["source"] == "none"
    assert "every scheduled retry" in recovery["diagnosis"].lower()
    assert "{" not in recovery["message"], recovery["message"]


def test_hard_decline_copy_does_not_name_stripe(no_key):
    # Recharge often charges through Shopify Payments; "told Stripe" would be false.
    recovery = _post(_charge("DO_NOT_HONOR")).json()["recovery"]
    assert "stripe" not in recovery["diagnosis"].lower()
    assert "stripe" not in recovery["strategy"]["offer"].lower()


def test_non_payment_error_is_acknowledged_and_not_run(no_key, monkeypatch):
    calls = []
    monkeypatch.setattr(api_module, "run_recovery", lambda e: calls.append(e) or {})
    body = _post(_charge("VARIANT_DOES_NOT_EXIST")).json()
    assert body == {"received": True, "handled": False, "reason": rm.NOT_CUSTOMER,
                    "error_type": "VARIANT_DOES_NOT_EXIST"}
    assert calls == []


def test_unknown_error_type_is_acknowledged_and_not_run(no_key, monkeypatch):
    calls = []
    monkeypatch.setattr(api_module, "run_recovery", lambda e: calls.append(e) or {})
    body = _post(_charge("SOMETHING_NEW")).json()
    assert body["handled"] is False and body["reason"] == rm.UNMAPPED
    assert calls == []


def test_control_a_payment_error_does_run(no_key, monkeypatch):
    # Control for the two tests above: the stub must be reachable.
    calls = []
    monkeypatch.setattr(api_module, "run_recovery", lambda e: calls.append(e) or {})
    _post(_charge("CARD_DECLINED"))
    assert len(calls) == 1


def test_charge_without_error_is_not_ours(no_key):
    body = _charge()
    body["charge"]["error_type"] = None
    assert _post(body).json()["reason"] == "not_a_failed_charge"


def test_redelivery_replays_and_a_new_attempt_does_not(no_key):
    first = _post(_charge("CARD_DECLINED", tries=2)).json()
    again = _post(_charge("CARD_DECLINED", tries=2)).json()
    assert "idempotent" not in first
    assert again["idempotent"] is True
    later = _post(_charge("CARD_DECLINED", tries=3)).json()
    assert "idempotent" not in later


def test_bad_signature_is_rejected(no_key):
    assert _post(_charge(), secret="wrong-secret").status_code == 400
    assert _post(_charge(), sign=False).status_code == 400


def test_non_charge_payload_is_rejected(no_key):
    assert _post({"subscription": {"id": 1}}).status_code == 400


# ---- silence while Recharge retries --------------------------------------------

def test_scheduled_recharge_retry_sends_no_customer_message(no_key):
    when = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    recovery = _post(_charge("CARD_DECLINED", retry_date=when)).json()["recovery"]
    assert recovery["message"] is None
    assert recovery["message_suppressed"] == "recharge_retry_scheduled"
    assert recovery["schedule"]["source"] == "recharge"
    assert recovery["diagnosis"]


def test_control_no_retry_date_still_drafts_a_message(no_key):
    # Control: suppression must key on Recharge's schedule, not on the error type.
    recovery = _post(_charge("CARD_DECLINED", retry_date=None)).json()["recovery"]
    assert recovery["message"]
    assert "message_suppressed" not in recovery


def test_customer_must_act_still_gets_a_message(no_key):
    when = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    recovery = _post(_charge("CARD_EXPIRED", retry_date=when)).json()["recovery"]
    assert recovery["message"]
    assert "message_suppressed" not in recovery
