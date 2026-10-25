"""The normalised invoice record: strict, deterministic, no personal data."""

from __future__ import annotations

from datetime import date

import pytest

from app import invoice_source as inv

SALT = "test-salt-not-a-secret"
TODAY = date(2026, 10, 25)


def rec(**over):
    base = dict(
        source="csv", source_id="r1", invoice_no="INV-1",
        customer_ref=inv.make_customer_ref("Acme Ltd", SALT),
        amount_minor=12_500, currency="usd", status="open",
        issued=date(2026, 9, 1), due=date(2026, 10, 1),
    )
    base.update(over)
    return inv.InvoiceRecord(**base)


def test_the_customer_ref_is_stable_salted_and_not_the_name():
    a = inv.make_customer_ref("Acme Ltd", SALT)
    assert a == inv.make_customer_ref("  ACME LTD ", SALT)
    assert a != inv.make_customer_ref("Acme Ltd", SALT + "x")
    assert "acme" not in a.lower() and len(a) == 16


@pytest.mark.parametrize("salt", ["", "short", None])
def test_a_missing_or_weak_salt_is_refused(salt):
    with pytest.raises(inv.InvoiceError):
        inv.make_customer_ref("Acme", salt)


@pytest.mark.parametrize(
    "text,cur,expected",
    [("125.00", "usd", 12_500), ("125", "usd", 12_500), ("0.01", "eur", 1),
     ("5000", "jpy", 5000), ("1.234", "kwd", 1234)],
)
def test_amounts_parse_to_exact_minor_units(text, cur, expected):
    assert inv.parse_minor(text, cur) == expected


@pytest.mark.parametrize(
    "bad",
    [12.5, True, 1250, "12,50", "1,250.00", "-5.00", "0", "0.00", "abc", "",
     "1e3", "NaN", "Infinity", "12.505", " ", "1 000", None],
)
def test_ambiguous_or_malformed_amounts_are_refused(bad):
    with pytest.raises(inv.InvoiceError):
        inv.parse_minor(bad, "usd")


def test_a_yen_amount_with_decimals_is_refused():
    with pytest.raises(inv.InvoiceError):
        inv.parse_minor("100.50", "jpy")


def test_an_absurd_amount_is_a_unit_error_not_a_big_invoice():
    with pytest.raises(inv.InvoiceError):
        inv.parse_minor("9" * 20, "usd")


def test_errors_name_the_field_and_never_echo_the_value():
    secret = "SECRET-VALUE-9f3"
    for call in (
        lambda: inv.parse_minor(secret, "usd"),
        lambda: inv.parse_date(secret),
        lambda: rec(invoice_no=secret + " bad space"),
        lambda: rec(currency=secret),
    ):
        with pytest.raises(inv.InvoiceError) as exc:
            call()
        assert secret not in str(exc.value)


@pytest.mark.parametrize(
    "field,value",
    [("source", "CSV"), ("source_id", ""), ("invoice_no", 'x"y'), ("customer_ref", "Acme Ltd"),
     ("amount_minor", 12.5), ("amount_minor", True), ("amount_minor", 0), ("amount_minor", -1),
     ("currency", "US"), ("currency", "USDX"), ("status", "Unpaid"), ("due", "2026-10-01")],
)
def test_each_field_is_validated(field, value):
    with pytest.raises(inv.InvoiceError):
        rec(**{field: value})


def test_dates_are_iso_only_and_empty_is_none():
    assert inv.parse_date("2026-10-01") == date(2026, 10, 1)
    assert inv.parse_date("") is None and inv.parse_date(None) is None
    for bad in ("01/10/2026", "2026-13-01", "2026-02-30", "2026-1-1", 20261001):
        with pytest.raises(inv.InvoiceError):
            inv.parse_date(bad)


@pytest.mark.parametrize(
    "due,status,expected",
    [(date(2026, 10, 25), "open", "not_due"), (date(2026, 10, 26), "open", "not_due"),
     (date(2026, 10, 24), "open", "overdue_1_7"), (date(2026, 10, 18), "open", "overdue_1_7"),
     (date(2026, 10, 17), "open", "overdue_8_30"), (date(2026, 9, 25), "open", "overdue_8_30"),
     (date(2026, 9, 24), "open", "overdue_31_60"), (date(2026, 8, 26), "open", "overdue_31_60"),
     (date(2026, 8, 25), "open", "overdue_61_plus"),
     (date(2026, 1, 1), "paid", "closed"), (date(2026, 1, 1), "cancelled", "closed"),
     (date(2026, 1, 1), "processing", "processing"), (None, "open", "no_due_date"),
     (None, "unknown", "no_due_date")],
)
def test_ageing_buckets_at_every_boundary(due, status, expected):
    assert inv.bucket(rec(due=due, status=status), TODAY) == expected


def test_a_missing_due_date_is_never_zero_days_overdue():
    r = rec(due=None)
    assert inv.days_overdue(r, TODAY) is None
    assert inv.bucket(r, TODAY) == "no_due_date"


def test_every_bucket_the_function_returns_is_declared():
    seen = {inv.bucket(rec(due=d, status=s), TODAY)
            for d in (None, date(2026, 12, 1), date(2026, 10, 24), date(2026, 10, 10),
                      date(2026, 9, 10), date(2026, 1, 1))
            for s in ("open", "paid", "processing")}
    assert seen <= set(inv.BUCKETS)
    assert set(inv.BUCKETS) == seen, "a declared bucket nothing can produce is dead data"


def test_the_record_is_immutable():
    with pytest.raises(Exception):
        rec().status = "paid"
