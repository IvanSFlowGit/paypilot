"""The Mercury mapper: signature scheme, status table, refusal to guess field names."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date

import pytest

from app import mercury_map as mm
from app.invoice_source import InvoiceRecord

SECRET = "whsec_test_not_real"
SALT = "test-salt-not-a-secret"
NOW = 1_800_000_000
BODY = b'{"id":"evt_1","resourceType":"transaction","resourceId":"tx_9","operationType":"create"}'

# SYNTHETIC key names standing in for the three the documentation does not state.
KEYS = {"amount": "totalAmount", "due": "dueDate", "currency": "currency"}


def sign(body=BODY, secret=SECRET, ts=NOW):
    mac = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(header, body=BODY, secret=SECRET, now=NOW, **kw):
    return mm.verify_mercury_signature(secret, header, body, now=now, **kw)


def test_a_correctly_signed_fresh_delivery_verifies():
    assert verify(sign()) is True


def test_wrong_secret_fails():
    assert verify(sign(secret="other")) is False


def test_altered_body_fails():
    assert verify(sign(), body=BODY + b" ") is False


def test_a_reserialised_body_fails_because_the_raw_bytes_are_what_was_signed():
    reserialised = json.dumps(json.loads(BODY)).encode()
    assert reserialised != BODY and verify(sign(), body=reserialised) is False


@pytest.mark.parametrize("delta", [-301, 301, -10_000, 10_000])
def test_outside_the_five_minute_window_fails_in_both_directions(delta):
    assert verify(sign(ts=NOW + delta)) is False


@pytest.mark.parametrize("delta", [-300, 0, 300])
def test_the_window_edges_pass(delta):
    assert verify(sign(ts=NOW + delta)) is True


@pytest.mark.parametrize(
    "header",
    ["", "garbage", "t=1800000000", "v1=" + "a" * 64, f"t={NOW},v1=short",
     f"t={NOW},v1=" + "z" * 64, "t=abc,v1=" + "a" * 64, f"t={NOW};v1=" + "a" * 64],
)
def test_malformed_headers_fail_without_raising(header):
    assert verify(header) is False


def test_uppercase_hex_is_accepted_because_case_is_not_part_of_the_signature():
    ts, mac = sign().split(",v1=")
    assert verify(f"{ts},v1={mac.upper()}") is True


@pytest.mark.parametrize("secret", ["", None])
def test_an_empty_secret_never_verifies(secret):
    header = sign(secret="")
    assert mm.verify_mercury_signature(secret, header, BODY, now=NOW) is False


def test_a_non_bytes_body_or_header_fails_without_raising():
    assert mm.verify_mercury_signature(SECRET, sign(), "text", now=NOW) is False
    assert mm.verify_mercury_signature(SECRET, None, BODY, now=NOW) is False


def test_the_custom_tolerance_is_honoured():
    assert verify(sign(ts=NOW - 50), tolerance=10) is False
    assert verify(sign(ts=NOW - 5), tolerance=10) is True


# ---- invoices ------------------------------------------------------------

def invoice(**over):
    base = {"id": "inv_1", "invoiceNumber": "INV-7", "customerId": "cus_1", "status": "Unpaid",
            "totalAmount": "125.00", "dueDate": "2026-10-01", "currency": "USD"}
    base.update(over)
    return base


def test_every_published_status_has_a_decision_in_the_closed_set():
    assert set(mm.PUBLISHED_MERCURY_STATUSES) == {"Unpaid", "Processing", "Paid", "Cancelled"}
    for status, mapped in mm.PUBLISHED_MERCURY_STATUSES.items():
        r = mm.mercury_invoice_to_record(invoice(status=status), keys=KEYS, salt=SALT)
        assert isinstance(r, InvoiceRecord) and r.status == mapped


def test_a_good_invoice_translates():
    r = mm.mercury_invoice_to_record(invoice(), keys=KEYS, salt=SALT)
    assert (r.source, r.source_id, r.invoice_no) == ("mercury", "inv_1", "INV-7")
    assert (r.amount_minor, r.currency, r.due) == (12_500, "usd", date(2026, 10, 1))
    assert "cus_1" not in repr(r)


@pytest.mark.parametrize("missing", ["amount", "due", "currency"])
def test_unstated_keys_must_be_supplied_or_the_invoice_is_unmapped(missing):
    keys = {k: v for k, v in KEYS.items() if k != missing}
    r = mm.mercury_invoice_to_record(invoice(), keys=keys, salt=SALT)
    assert isinstance(r, mm.Unmapped) and missing in r.reason


def test_a_plausible_but_unsupplied_key_is_not_read():
    payload = invoice()
    payload["amount"] = "125.00"
    keys = {"amount": "", "due": "dueDate", "currency": "currency"}
    assert isinstance(mm.mercury_invoice_to_record(payload, keys=keys, salt=SALT), mm.Unmapped)


@pytest.mark.parametrize("status", ["Draft", "unpaid", "", None, 3])
def test_an_unpublished_status_is_unmapped_not_unknown(status):
    r = mm.mercury_invoice_to_record(invoice(status=status), keys=KEYS, salt=SALT)
    assert isinstance(r, mm.Unmapped)


@pytest.mark.parametrize("key", ["invoiceNumber", "customerId", "totalAmount", "currency"])
def test_a_missing_required_key_is_unmapped_and_names_the_key(key):
    payload = invoice()
    del payload[key]
    r = mm.mercury_invoice_to_record(payload, keys=KEYS, salt=SALT)
    assert isinstance(r, mm.Unmapped) and key in r.reason


def test_a_missing_due_date_translates_to_none_not_a_refusal():
    payload = invoice()
    del payload["dueDate"]
    r = mm.mercury_invoice_to_record(payload, keys=KEYS, salt=SALT)
    assert isinstance(r, InvoiceRecord) and r.due is None


@pytest.mark.parametrize("amount", [125.5, 12500, "12,50", "-1", "0"])
def test_ambiguous_amounts_are_unmapped_never_coerced(amount):
    r = mm.mercury_invoice_to_record(invoice(totalAmount=amount), keys=KEYS, salt=SALT)
    assert isinstance(r, mm.Unmapped)


def test_a_non_object_payload_is_unmapped():
    for bad in (None, [], "x", 3):
        assert isinstance(mm.mercury_invoice_to_record(bad, keys=KEYS, salt=SALT), mm.Unmapped)


def test_unmapped_reasons_never_echo_a_value():
    secret = "SECRET-VALUE-9f3"
    r = mm.mercury_invoice_to_record(invoice(totalAmount=secret, customerId=secret), keys=KEYS, salt=SALT)
    assert isinstance(r, mm.Unmapped) and secret not in r.reason


# ---- payment hint --------------------------------------------------------

MATCH = {"resourceType": "transaction", "operationType": "create"}


def test_a_matching_event_yields_a_hint_and_nothing_more():
    h = mm.mercury_event_to_hint(BODY, match=MATCH)
    assert h == mm.PaymentHint(event_id="evt_1", resource_id="tx_9")
    assert not hasattr(h, "amount_minor") and not hasattr(h, "paid")


def test_an_empty_match_matches_nothing():
    assert mm.mercury_event_to_hint(BODY, match={}) is None


def test_a_balance_update_is_not_a_payment_hint():
    body = b'{"id":"e2","resourceType":"checkingAccount","resourceId":"a1","operationType":"update"}'
    assert mm.mercury_event_to_hint(body, match=MATCH) is None


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b"null", b'{"resourceType":"transaction","operationType":"create"}',
     b'{"id":1,"resourceId":"x","resourceType":"transaction","operationType":"create"}'],
)
def test_malformed_events_are_none_not_exceptions(body):
    assert mm.mercury_event_to_hint(body, match=MATCH) is None
