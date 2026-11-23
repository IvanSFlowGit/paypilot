"""POST /inbound/read: the public, stateless demo of the reply reader."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import api as api_module

client = TestClient(api_module.app)


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    api_module._rate_hits.clear()
    api_module._global_hits.clear()
    yield
    api_module._rate_hits.clear()
    api_module._global_hits.clear()


def post(**kw):
    body = {"message": "I have already paid this invoice."}
    body.update(kw)
    return client.post("/inbound/read", json=body)


def test_paid_claim_with_a_simulated_booked_credit_is_confirmed_as_a_draft():
    r = post(bank_feed="booked", customer_first_name="Maria")
    assert r.status_code == 200
    j = r.json()
    assert j["reading"]["intent"] == "already_paid" and j["payment_check"] == "found_full_booked"
    assert j["rule_fired"] == "paid_found_booked" and j["draft_only"] is True
    assert j["reply"]["body"].startswith("Hello Maria,") and "money_in_flight" in j["actions"]


def test_pending_credit_is_not_called_received():
    j = post(bank_feed="pending").json()
    assert j["payment_check"] == "found_full_pending" and "still pending" in j["reply"]["body"]


def test_no_feed_connected_routes_a_paid_claim_to_a_person():
    j = post().json()
    assert j["payment_check"] == "unchecked" and j["person_must_act"] is True


def test_empty_feed_asks_for_a_reference():
    j = post(bank_feed="none").json()
    assert j["rule_fired"] == "paid_not_found" and "bank reference" in j["reply"]["body"]


def test_partial_and_unreachable_feeds():
    assert post(bank_feed="partial").json()["rule_fired"] == "paid_found_part"
    assert post(bank_feed="unreachable").json()["payment_check"] == "feed_unreachable"


def test_opt_out_in_a_quoted_footer_is_ignored_over_http():
    j = post(message="Already paid.\n> reply STOP and I will remove you\n> please stop emailing me").json()
    assert j["reading"]["intent"] == "already_paid"


def test_bad_inputs_are_refused_not_guessed():
    assert post(amount="12,50").status_code == 422
    assert post(invoice_no="bad id").status_code == 422
    assert post(message="").status_code == 422
    assert post(message="x" * 4001).status_code == 422
    assert post(bank_feed="settled").status_code == 422
    assert post(amount="0").status_code == 422


def test_the_endpoint_is_in_the_published_schema_and_stores_nothing():
    spec = client.get("/openapi.json").json()
    assert "/inbound/read" in spec["paths"]


def test_the_public_reply_demo_refuses_big_bodies_before_reading_and_counts_invalid_requests():
    from app import api as api_mod

    api_mod._rate_hits.clear()
    api_mod._global_hits.clear()
    c = TestClient(api_mod.app)
    big = c.post("/inbound/read", content=b'{"message":"' + b"x" * 20_000 + b'"}', headers={"content-type": "application/json"})
    assert big.status_code == 413
    no_length = c.post("/inbound/read", content=iter([b'{"message":"hi"}']), headers={"content-type": "application/json"})
    assert no_length.status_code == 411
    before = sum(len(v) for v in api_mod._rate_hits.values())
    bad = c.post("/inbound/read", json={"message": ""})
    assert bad.status_code == 422
    assert sum(len(v) for v in api_mod._rate_hits.values()) > before


def test_a_hostile_business_name_with_unicode_line_breaks_is_refused_over_http():
    c = TestClient(__import__("app.api", fromlist=["app"]).app)
    for code in (0x2028, 0x202E, 0x0085):
        r = c.post("/inbound/read", json={"message": "I have already paid this", "business_name": "A" + chr(code) + "B"})
        assert r.status_code == 422, hex(code)
