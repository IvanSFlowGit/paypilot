"""PayPilot on Chargebee failed payments, pinned in both directions.

The two facts that make this adapter different from Stripe and Recharge are
the two these tests exist to hold down.

* CHARGEBEE SIGNS NOTHING, so the only check is a shared secret the receiver
  chose. It is compared in constant time, it FAILS CLOSED when unset, and it
  is read only from the two carriers Chargebee can actually send: the
  basic-auth password and a key in the URL.
* CHARGEBEE PUBLISHES NO ERROR VOCABULARY. ``transaction.error_code`` belongs
  to the gateway, so it is mapped on Stripe and NOT mapped anywhere else. The
  gateway cases below use a code that IS in Stripe's table against a non-Stripe
  gateway, because that is the only input where the two readings diverge: map
  it and the answer is ``card_expired``, refuse to and it is
  ``generic_decline``. A test using an unmapped code would pass either way.

Chargebee runs its own dunning and emails the customer during it, so an invoice
still in dunning returns the diagnosis and the schedule with the message HELD.

All offline: no network, no Chargebee site, no live payload. Every fixture here
is synthetic and written from the documentation cited in app/chargebee_map.py.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import chargebee_map as cm

SECRET = "cb-test-shared-secret"


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setenv("CHARGEBEE_WEBHOOK_SECRET", SECRET)
    api_module._idem_store.clear()
    return monkeypatch


def _event(
    *,
    event_type="payment_failed",
    status="failure",
    gateway="stripe",
    error_code="expired_card",
    dunning_status="exhausted",
    next_retry_at=None,
    event_id="ev_cb_001",
    amount_due=2900,
    with_transaction=True,
):
    """A synthetic Chargebee event envelope. Keys from the published attribute
    names; the resource set inside ``content`` is the part not confirmed against
    a live delivery, which is why every reader in the module is defensive."""
    content: dict = {
        "invoice": {
            "id": "inv_7781",
            "status": "payment_due",
            "dunning_status": dunning_status,
            "next_retry_at": next_retry_at,
            "amount_due": amount_due,
            "attempt_count": 2,
            "currency_code": "USD",
            "customer_id": "cust_42",
            "subscription_id": "sub_9",
            "resource_version": 1739999999000,
        },
        "customer": {
            "id": "cust_42",
            "first_name": "Dana",
            "last_name": "Fox",
            "email": "dana@example.test",
        },
        "subscription": {"id": "sub_9", "plan_id": "coffee-club-monthly"},
    }
    if with_transaction:
        content["transaction"] = {
            "id": "txn_3",
            "status": status,
            "error_code": error_code,
            "error_text": "Your card has expired.",
            "gateway": gateway,
            "amount": 2900,
            "currency_code": "USD",
            "customer_id": "cust_42",
            "subscription_id": "sub_9",
            "payment_method": "card",
        }
    return {
        "id": event_id,
        "occurred_at": 1739990000,
        "event_type": event_type,
        "api_version": "v2",
        "source": "scheduled_job",
        "content": content,
    }


# --- the shared secret, which is all Chargebee gives us --------------------

def test_the_right_secret_is_accepted():
    assert cm.verify_chargebee_shared_secret(SECRET, SECRET) is True


def test_a_wrong_secret_is_refused():
    assert cm.verify_chargebee_shared_secret("nope", SECRET) is False


def test_an_unset_expected_secret_fails_closed():
    """An empty secret compared against an empty header would authenticate
    everybody, which is the one outcome this function must never produce."""
    assert cm.verify_chargebee_shared_secret("", "") is False
    assert cm.verify_chargebee_shared_secret(None, None) is False
    assert cm.verify_chargebee_shared_secret("anything", "") is False
    assert cm.verify_chargebee_shared_secret("anything", None) is False


def test_a_missing_provided_secret_is_refused():
    assert cm.verify_chargebee_shared_secret(None, SECRET) is False
    assert cm.verify_chargebee_shared_secret("   ", SECRET) is False


def test_a_non_ascii_secret_compares_rather_than_raising():
    """compare_digest refuses non-ASCII str, so both sides are encoded first."""
    assert cm.verify_chargebee_shared_secret("paselle-naive", "paselle-naive") is True
    assert cm.verify_chargebee_shared_secret("x", "paselle-naive") is False


# --- classification keys on the closed enums, never on a borrowed namespace -

def test_every_published_transaction_status_has_a_decision():
    """The closed-class assert: a status Chargebee publishes and this table
    does not carry would classify UNMAPPED and silently stop recovery."""
    for status in cm.PUBLISHED_TRANSACTION_STATUSES:
        got = cm.classify_chargebee_failure({"status": status}, {})
        assert got, status
        assert got != cm.UNMAPPED, status


def test_an_unknown_status_is_unmapped_rather_than_guessed():
    assert cm.classify_chargebee_failure({"status": "exploded"}, {}) == cm.UNMAPPED
    assert cm.classify_chargebee_failure({}, {}) == cm.UNMAPPED
    assert cm.classify_chargebee_failure({"status": ""}, {}) == cm.UNMAPPED


@pytest.mark.parametrize("status", ["timeout", "needs_attention"])
def test_a_gateway_connection_failure_never_reaches_the_customer(status):
    """Chargebee's own words: these two are the gateway not accepting or
    dropping the connection. Nothing about the card is implicated, so emailing
    the customer would be asking them to fix somebody else's outage."""
    assert cm.classify_chargebee_failure({"status": status}, {}) == cm.NOT_CUSTOMER


def test_a_late_failure_is_the_customers_to_act_on():
    """A payment that succeeded and was later reversed is the case a retry
    schedule is least likely to cover."""
    got = cm.classify_chargebee_failure(
        {"status": "late_failure", "gateway": "stripe", "error_code": "insufficient_funds"},
        {"dunning_status": "stopped"},
    )
    assert got == "insufficient_funds"


def test_a_stripe_gateway_code_is_read_with_stripes_own_table():
    got = cm.classify_chargebee_failure(
        {"status": "failure", "gateway": "stripe", "error_code": "expired_card"},
        {"dunning_status": "stopped"},
    )
    assert got == "card_expired"


def test_another_gateways_code_is_not_read_with_stripes_table():
    """THE DISCRIMINATING CASE. ``expired_card`` is in Stripe's table, so a
    reader that ignored ``gateway`` would answer ``card_expired`` here. Adyen
    does not reserve Stripe's namespace and neither does PayPilot, so the
    answer falls back to what Chargebee's own enum supports."""
    got = cm.classify_chargebee_failure(
        {"status": "failure", "gateway": "adyen", "error_code": "expired_card"},
        {"dunning_status": "stopped"},
    )
    assert got == "generic_decline"


def test_an_unmapped_stripe_code_falls_back_rather_than_failing():
    got = cm.classify_chargebee_failure(
        {"status": "failure", "gateway": "stripe", "error_code": "lost_card"},
        {"dunning_status": "stopped"},
    )
    assert got == "generic_decline"


def test_exhausted_dunning_outranks_the_gateway_code():
    """Chargebee has stopped: "Maximum number of attempts have been made."
    Nobody else is going to collect this, so the customer needs a new method
    whatever the last decline said. Uses a code that maps elsewhere, so the
    two readings diverge."""
    got = cm.classify_chargebee_failure(
        {"status": "failure", "gateway": "stripe", "error_code": "insufficient_funds"},
        {"dunning_status": "exhausted"},
    )
    assert got == "retries_exhausted"


def test_a_missing_invoice_does_not_raise():
    assert cm.classify_chargebee_failure({"status": "failure"}, None) == "generic_decline"


# --- Chargebee owns the schedule while dunning is running ------------------

def test_the_retry_date_is_reported_while_dunning_is_in_progress():
    ts = 1740000000
    got = cm.chargebee_next_retry({"dunning_status": "in_progress", "next_retry_at": ts})
    assert got == datetime.fromtimestamp(ts, tz=UTC).isoformat()


def test_no_retry_is_promised_once_dunning_is_exhausted():
    """A stale next_retry_at left on an exhausted invoice would otherwise read
    as a promise nobody is going to keep."""
    assert cm.chargebee_next_retry(
        {"dunning_status": "exhausted", "next_retry_at": 1740000000}
    ) is None


def test_no_retry_without_a_timestamp():
    assert cm.chargebee_next_retry({"dunning_status": "in_progress"}) is None
    assert cm.chargebee_next_retry({"dunning_status": "in_progress", "next_retry_at": "soon"}) is None
    assert cm.chargebee_next_retry(None) is None


# --- the translation -------------------------------------------------------

def test_the_internal_event_carries_the_duplicate_and_ordering_keys():
    """Chargebee says to expect the same delivery more than once and out of
    order, and names ``id`` and ``resource_version`` as the defences, so both
    leave this function rather than being re-derived by every caller."""
    out = cm.chargebee_event_to_internal(_event())
    assert out["event_id"] == "ev_cb_001"
    assert out["resource_version"] == 1739999999000


def test_the_amount_is_the_debt_rather_than_the_attempt():
    """amount_due is what the customer owes; the transaction amount is what one
    attempt tried to take. A customer-facing message is about the debt."""
    out = cm.chargebee_event_to_internal(_event(amount_due=1250))
    assert out["event"]["amount"] == 12.5


def test_the_transaction_amount_is_the_fallback():
    ev = _event(amount_due=None)
    out = cm.chargebee_event_to_internal(ev)
    assert out["event"]["amount"] == 29.0


def test_the_attempt_count_comes_from_the_invoice():
    out = cm.chargebee_event_to_internal(_event())
    assert out["event"]["attempt"] == 2


def test_an_event_with_no_transaction_is_unmapped():
    """The resource set inside content is the one thing not confirmed against a
    live delivery, so its absence is reported rather than guessed around."""
    out = cm.chargebee_event_to_internal(_event(with_transaction=False))
    assert out["classification"] == cm.UNMAPPED


def test_the_customer_and_plan_survive_the_translation():
    out = cm.chargebee_event_to_internal(_event())["event"]
    assert out["customer_id"] == "cust_42"
    assert out["customer_email"] == "dana@example.test"
    assert out["customer_name"] == "Dana Fox"
    assert out["plan"] == "coffee-club-monthly"
    assert out["processor"] == "chargebee"


# --- the route -------------------------------------------------------------

def _post(client, event, *, key=None, basic=None, headers=None):
    url = "/webhooks/chargebee" + (f"?key={key}" if key else "")
    hdrs = dict(headers or {})
    if basic is not None:
        token = base64.b64encode(f"cb:{basic}".encode()).decode()
        hdrs["Authorization"] = f"Basic {token}"
    return client.post(url, content=json.dumps(event), headers=hdrs)


def test_the_url_key_authenticates(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(), key=SECRET)
    assert r.status_code == 200, r.text
    assert r.json()["handled"] is True


def test_the_basic_auth_password_authenticates(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(event_id="ev_basic"), basic=SECRET)
    assert r.status_code == 200, r.text
    assert r.json()["handled"] is True


def test_a_wrong_secret_is_rejected(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(), key="wrong")
    assert r.status_code == 400


def test_no_credentials_at_all_is_rejected(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event())
    assert r.status_code == 400


def test_a_malformed_basic_header_is_a_rejection_not_a_crash(no_key):
    """A header that is not valid base64 must not reach the decoder as a 500,
    because an unauthenticated caller chooses that string."""
    with TestClient(api_module.app) as client:
        r = _post(client, _event(), headers={"Authorization": "Basic !!!not-base64!!!"})
    assert r.status_code == 400


def test_an_unset_secret_rejects_when_authentication_is_required(no_key):
    no_key.delenv("CHARGEBEE_WEBHOOK_SECRET", raising=False)
    no_key.delenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    with TestClient(api_module.app) as client:
        r = _post(client, _event())
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_a_successful_payment_is_not_a_failure(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(event_type="payment_succeeded"), key=SECRET)
    body = r.json()
    assert r.status_code == 200
    assert body["handled"] is False
    assert body["reason"] == "not_a_failed_payment"


def test_a_gateway_timeout_is_acknowledged_and_not_run(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(status="timeout"), key=SECRET)
    body = r.json()
    assert body["handled"] is False
    assert body["reason"] == cm.NOT_CUSTOMER


def test_the_message_is_held_while_chargebee_is_still_dunning(no_key):
    """Chargebee emails the customer itself during dunning, so a PayPilot
    message now would duplicate one they are already receiving."""
    ev = _event(dunning_status="in_progress", next_retry_at=1740000000, event_id="ev_dun")
    with TestClient(api_module.app) as client:
        r = _post(client, ev, key=SECRET)
    rec = r.json()["recovery"]
    assert rec["message"] is None
    assert rec["message_suppressed"] == "chargebee_dunning_in_progress"


def test_the_message_is_written_once_dunning_is_exhausted(no_key):
    with TestClient(api_module.app) as client:
        r = _post(client, _event(event_id="ev_exh"), key=SECRET)
    rec = r.json()["recovery"]
    assert rec.get("message_suppressed") is None


def test_the_same_event_id_is_served_from_the_idempotency_cache(no_key):
    ev = _event(event_id="ev_dupe")
    with TestClient(api_module.app) as client:
        first = _post(client, ev, key=SECRET).json()
        second = _post(client, ev, key=SECRET).json()
    assert first.get("idempotent") is None
    assert second["idempotent"] is True


def test_a_non_dict_payload_is_a_400(no_key):
    with TestClient(api_module.app) as client:
        r = client.post(f"/webhooks/chargebee?key={SECRET}", content="[]")
    assert r.status_code == 400


def test_invalid_json_is_a_400(no_key):
    with TestClient(api_module.app) as client:
        r = client.post(f"/webhooks/chargebee?key={SECRET}", content="{not json")
    assert r.status_code == 400


def test_the_route_and_the_module_agree_on_the_failure_event_set():
    """Two lists of event types would drift. The route reads the module's."""
    import app.api as a

    src = a.chargebee_webhook.__doc__ or ""
    assert "payment_failed" in src
    assert set(cm.PUBLISHED_FAILURE_EVENTS) == {"payment_failed"}


def test_the_module_says_it_has_never_seen_a_live_payload():
    """The honesty claim is load bearing: it is the difference between an
    adapter written from documentation and one proven against a delivery."""
    assert "NEVER SEEN A LIVE CHARGEBEE PAYLOAD" in (cm.__doc__ or "")
