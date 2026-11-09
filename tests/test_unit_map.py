"""Unit's return reasons become ACH codes, and a delivery that cannot be decided is held. Synthetic data only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from app import ach_return_map, unit_map
from app.unit_map import (
    UnitPayloadError,
    nacha_code_for,
    parse_unit_delivery,
    verify_unit_signature,
)

SECRET = "test-secret-not-a-real-one"


def event(etype="payment.returned", reason="InsufficientFunds", eid="e1", pid="p1", key="reason"):
    attrs = {"createdAt": "2026-11-01T10:00:00.000Z", "amount": 2500}
    if reason is not None:
        attrs[key] = reason
    return {"id": eid, "type": etype, "attributes": attrs,
            "relationships": {"payment": {"data": {"type": "payment", "id": pid}}}}


def body(*events):
    return json.dumps({"data": list(events)}).encode()


def sign(raw, secret=SECRET):
    return base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha1).digest()).decode()


# --- the map ---------------------------------------------------------------

def test_the_table_has_41_unique_names_and_unique_ach_codes():
    assert len(unit_map.UNIT_REASONS) == 41
    codes = list(unit_map.UNIT_REASONS.values())
    assert len(set(codes)) == 41
    assert all(c.startswith("R") and len(c) == 3 for c in codes)


def test_every_code_in_the_ach_decision_table_has_a_unit_name():
    table = {c for c in ach_return_map._TABLE if c != "DEFAULT"}
    assert table <= set(unit_map.UNIT_REASONS.values())


@pytest.mark.parametrize("name,code", [
    ("InsufficientFunds", "R01"), ("UncollectedFunds", "R09"), ("ConsumerPaymentUnauthorized", "R10"),
    ("AccountFrozen", "R16"), ("InvalidIndividualOrCompanyName", "R45"),
    ("IncorrectlyCodedOutboundInternationalPayment", "R85"),
])
def test_known_names_map_to_their_codes(name, code):
    assert nacha_code_for(name) == code


@pytest.mark.parametrize("value", ["insufficientfunds", "INSUFFICIENT_FUNDS", " Insufficient Funds ", "insufficient-funds"])
def test_case_and_punctuation_are_ignored(value):
    assert nacha_code_for(value) == "R01"


@pytest.mark.parametrize("value", ["R01", "r09", " R10 "])
def test_a_nacha_code_passes_through(value):
    assert nacha_code_for(value) == value.strip().upper()


@pytest.mark.parametrize("value", ["NotAReason", "", "R1", "R001", "Insufficient", None, 7, ["R01"]])
def test_an_unknown_or_malformed_reason_is_none_never_a_guess(value):
    assert nacha_code_for(value) is None


# --- the signature ---------------------------------------------------------

def test_a_correct_signature_verifies():
    raw = body(event())
    assert verify_unit_signature(raw, sign(raw), SECRET) is True


def test_one_changed_byte_fails():
    raw = body(event())
    assert verify_unit_signature(raw + b" ", sign(raw), SECRET) is False


@pytest.mark.parametrize("header,secret", [(None, SECRET), ("", SECRET), ("x", SECRET), ("anything", None), ("anything", "")])
def test_a_missing_or_wrong_header_or_secret_fails_closed(header, secret):
    assert verify_unit_signature(body(event()), header, secret) is False


def test_a_signature_under_another_secret_fails():
    raw = body(event())
    assert verify_unit_signature(raw, sign(raw, "other"), SECRET) is False


def test_surrounding_whitespace_on_the_header_is_tolerated():
    raw = body(event())
    assert verify_unit_signature(raw, " " + sign(raw) + "\n", SECRET) is True


def test_the_comparison_is_constant_time():
    import inspect
    assert "compare_digest" in inspect.getsource(verify_unit_signature)


# --- the delivery ----------------------------------------------------------

def test_a_returned_payment_with_a_reason_is_decided_from_the_ach_table():
    r = parse_unit_delivery(body(event(reason="InsufficientFunds")))[0]
    assert (r.status, r.code, r.decision_row, r.person_required, r.payment_id) == ("decided", "R01", 1, False, "p1")


def test_an_unauthorised_return_goes_to_a_person():
    r = parse_unit_delivery(body(event(reason="ConsumerPaymentUnauthorized")))[0]
    assert (r.status, r.code, r.decision_row, r.person_required) == ("decided", "R10", 5, True)


def test_a_valid_unit_name_the_ach_table_has_no_row_for_falls_to_the_conservative_row():
    r = parse_unit_delivery(body(event(reason="ReturnedPerOdfiRequest")))[0]
    assert (r.status, r.code, r.matched, r.person_required) == ("decided", "R06", False, True)


def test_an_event_with_no_reason_is_held_and_never_looked_up():
    r = parse_unit_delivery(body(event(reason=None)))[0]
    assert (r.status, r.code, r.person_required) == ("held", None, True)
    assert r.why.startswith("reason_not_in_event")


def test_an_unknown_reason_name_is_held():
    r = parse_unit_delivery(body(event(reason="SomethingNew")))[0]
    assert (r.status, r.code, r.person_required) == ("held", None, True)
    assert r.why.startswith("unknown_reason_name")


def test_returnReason_is_read_when_reason_is_absent():
    r = parse_unit_delivery(body(event(reason="AccountClosed", key="returnReason")))[0]
    assert (r.status, r.code) == ("decided", "R02")


def test_a_rejection_is_not_a_network_return_and_is_not_mapped():
    r = parse_unit_delivery(body(event(etype="payment.rejected", reason="InsufficientFunds")))[0]
    assert (r.status, r.code, r.decision_row) == ("not_a_return", None, None)


def test_other_event_types_are_not_decided():
    r = parse_unit_delivery(body(event(etype="customer.created")))[0]
    assert r.status == "not_a_return"


def test_a_repeated_event_id_is_decided_once():
    out = parse_unit_delivery(body(event(eid="dup"), event(eid="dup"), event(eid="other")))
    assert [r.event_id for r in out] == ["dup", "other"]


def test_a_bare_list_and_a_single_object_are_accepted():
    assert len(parse_unit_delivery(json.dumps([event()]))) == 1
    assert len(parse_unit_delivery(json.dumps({"data": event()}))) == 1


@pytest.mark.parametrize("raw", [b"not json", b"{}", b"[]", b'{"data": []}', b'{"data": [1]}', b'{"data": "x"}', b"\xff\xfe", b"null", b'"x"'])
def test_a_malformed_delivery_raises_and_decides_nothing(raw):
    with pytest.raises(UnitPayloadError):
        parse_unit_delivery(raw)


def test_a_non_string_non_bytes_delivery_raises():
    with pytest.raises(UnitPayloadError):
        parse_unit_delivery(123)


def test_the_cap_on_events_is_enforced():
    with pytest.raises(UnitPayloadError):
        parse_unit_delivery(json.dumps({"data": [event(eid=f"e{i}") for i in range(unit_map.MAX_EVENTS + 1)]}))


def test_the_module_has_no_network_or_model_path():
    import inspect
    src = inspect.getsource(unit_map)
    for banned in ("import requests", "import urllib", "import socket", "http.client", "anthropic", "openai", "open("):
        assert banned not in src
