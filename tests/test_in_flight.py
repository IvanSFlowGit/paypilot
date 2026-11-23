"""In-flight hold: synthetic bank lines, offline, no key. Every hold expires; weak evidence never holds."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app import in_flight as f
from app import invoice_rules as rules
from app import invoice_source as inv
from app.bank.provider import Transaction

SALT = "test-salt-not-a-secret"
TODAY = date(2026, 11, 9)  # a Monday


def record(**kw):
    base = dict(
        source="csv", source_id="1", invoice_no="INV-1001",
        customer_ref=inv.make_customer_ref("Synthetic Co", SALT),
        amount_minor=250_000, currency="eur", status="open", due=date(2026, 10, 1),
    )
    base.update(kw)
    return inv.InvoiceRecord(**base)


def ob(desc, amount=250_000, ccy="EUR", state="pending", seen=TODAY, booked=None):
    t = Transaction(
        transaction_id="t1", amount_minor=amount, currency=ccy,
        booked_at=booked or datetime(seen.year, seen.month, seen.day, tzinfo=UTC),
        description=desc,
    )
    return f.Observation(t, state, seen)


def run(obs, today=TODAY, alive=True, rec=None):
    return f.assess(rec or record(), obs, today, feed_alive=alive)


def test_pending_credit_with_reference_holds_until_three_business_days():
    v = run([ob("SEPA CREDIT INV-1001 ACME")])
    assert (v.label, v.rule_id) == (f.HOLD, "credit_in_flight")
    assert v.until == date(2026, 11, 12)
    assert v.matched == 1


def test_hold_expires_so_a_payment_that_never_lands_is_chased_again():
    v = run([ob("INV-1001", seen=date(2026, 11, 2))], today=date(2026, 11, 9))
    assert (v.label, v.rule_id) == (f.RELEASE, "hold_expired")
    assert v.until == date(2026, 11, 5)


def test_hold_is_live_on_the_expiry_day_itself():
    v = run([ob("INV-1001", seen=date(2026, 11, 4))], today=date(2026, 11, 9))
    assert v.label == f.HOLD and v.until == date(2026, 11, 9)


def test_business_days_skip_the_weekend():
    assert f.add_business_days(date(2026, 11, 6), 3) == date(2026, 11, 11)  # Fri + 3 = Wed


def test_booked_credit_holds_five_business_days_from_booking():
    booked = datetime(2026, 11, 6, tzinfo=UTC)
    v = run([ob("INV-1001", state="booked", booked=booked)])
    assert v.label == f.HOLD and v.until == date(2026, 11, 13)


def test_amount_only_match_is_reported_and_never_holds():
    v = run([ob("PAYMENT FROM SOMEONE")])
    assert (v.label, v.rule_id) == (f.RELEASE, "nothing_in_flight")
    assert v.possible == 1 and v.matched == 0


def test_reference_must_be_a_whole_token():
    assert run([ob("INV-10011")]).label == f.RELEASE
    assert run([ob("xINV-1001")]).label == f.RELEASE
    assert run([ob("ref inv-1001.")]).label == f.HOLD  # case-insensitive, punctuation ok


def test_debit_with_the_reference_is_ignored():
    assert run([ob("INV-1001", amount=-250_000)]).label == f.RELEASE


def test_a_credit_below_the_invoice_amount_never_holds_the_chase():
    v = run([ob("INV-1001", amount=100_000)])
    assert (v.label, v.rule_id, v.matched, v.matched_minor) == (f.UNKNOWN, "partial_credit", 1, 100_000)
    assert run([ob("INV-1001", amount=1)]).label == f.UNKNOWN
    assert run([ob("INV-1001", amount=250_000)]).label == f.HOLD
    assert run([ob("INV-1001", amount=300_000)]).label == f.HOLD


def test_another_documents_reference_does_not_hold_this_invoice():
    for desc in ("INV-1001-2", "INV-1001/A", "INV-1001_B", "ref INV-1001.2", "INV-1001#3"):
        assert run([ob(desc)]).label == f.RELEASE, desc
    assert run([ob("paid INV-1001, thanks")]).label == f.HOLD
    assert run([ob("INV-1001.")]).label == f.HOLD


def test_a_short_or_numeric_id_needs_the_amount_as_well_as_the_mention():
    short = inv.InvoiceRecord("t", "1", "7", "0123456789abcdef", 250_000, "eur", "open", due=date(2026, 10, 1))
    wrong_amount = f.assess(short, [ob("card 7 shop", amount=999)], date(2026, 11, 9), feed_alive=True)
    right_amount = f.assess(short, [ob("card 7 shop", amount=250_000)], date(2026, 11, 9), feed_alive=True)
    assert wrong_amount.label == f.RELEASE and right_amount.label == f.HOLD


def test_currency_mismatch_is_unknown_never_a_guess():
    v = run([ob("INV-1001", ccy="USD")])
    assert (v.label, v.rule_id) == (f.UNKNOWN, "currency_mismatch")


def test_dead_feed_is_unreachable_not_nothing_pending():
    v = run([], alive=False)
    assert (v.label, v.rule_id) == (f.UNKNOWN, "feed_unreachable")


def test_empty_live_feed_releases():
    v = run([])
    assert (v.label, v.rule_id, v.scanned) == (f.RELEASE, "nothing_in_flight", 0)


def test_scanned_counts_every_line_read():
    v = run([ob("a"), ob("b", amount=-5), ob("INV-1001")])
    assert v.scanned == 3


def test_bad_observation_state_refuses():
    with pytest.raises(ValueError):
        ob("x", state="settled")


# --- the rules table reads the hold, and only the hold ---

def facts(**kw):
    return rules.InvoiceFacts(**kw)


def test_money_in_flight_stops_a_chase_the_ageing_would_make():
    rec = record(due=date(2026, 9, 1))  # 69 days overdue
    assert rules.classify(rec, facts(), TODAY).label == "overdue_60"
    v = rules.classify(rec, facts(money_in_flight=True), TODAY)
    assert (v.label, v.rule_id) == ("UNKNOWN", "money_in_flight")


def test_money_in_flight_outranks_a_lapsed_promise():
    rec = record()
    v = rules.classify(rec, facts(promised_by=date(2026, 11, 1), money_in_flight=True), TODAY)
    assert v.rule_id == "money_in_flight"


def test_a_dispute_still_surfaces_to_a_person_under_a_hold():
    v = rules.classify(record(), facts(disputed=True, money_in_flight=True), TODAY)
    assert v.label == "disputed"


def test_money_in_flight_must_be_a_bool():
    with pytest.raises(ValueError):
        facts(money_in_flight="yes")


# --- the bank feed to the hold, end to end, offline ---

import json  # noqa: E402

from app import in_flight  # noqa: E402,F401
from app.bank.adapters import berlin_group as bg  # noqa: E402
from app.bank.provider import ProviderError  # noqa: E402


def _body(booked=None, pending=None):
    env = {}
    if booked is not None:
        env["booked"] = booked
    if pending is not None:
        env["pending"] = pending
    return json.dumps({"transactions": env}).encode()


def _row(desc, amount="2500.00", ccy="EUR", date_="2026-11-09", tid="x"):
    return {
        "transactionId": tid, "transactionAmount": {"amount": amount, "currency": ccy},
        "bookingDate": date_, "remittanceInformationUnstructured": desc,
    }


def test_pending_lines_survive_when_booked_lines_also_exist():
    body = _body(booked=[_row("old", tid="b1")], pending=[_row("INV-1001 ACME", tid="p1")])
    txns = bg.BerlinGroupAis().parse_transactions(body=body)
    assert sorted((t.transaction_id, t.status) for t in txns) == [("b1", "booked"), ("p1", "pending")]


def test_feed_to_hold_pending_credit_stops_the_chase():
    body = _body(booked=[_row("rent", amount="-10.00", tid="b1")], pending=[_row("INV-1001", tid="p1")])
    obs = f.observe(bg.BerlinGroupAis().parse_transactions(body=body), {}, TODAY)
    v = f.assess(record(), obs, TODAY, feed_alive=True)
    assert (v.label, v.matched, v.scanned) == (f.HOLD, 1, 2)


def test_first_seen_is_kept_across_polls_so_the_hold_cannot_be_renewed_forever():
    body = _body(pending=[_row("INV-1001", tid="p1")])
    txns = bg.BerlinGroupAis().parse_transactions(body=body)
    obs = f.observe(txns, {"p1": date(2026, 11, 2)}, TODAY)
    assert f.assess(record(), obs, TODAY, feed_alive=True).rule_id == "hold_expired"


def test_a_malformed_line_refuses_the_read_rather_than_reading_as_nothing_pending():
    with pytest.raises(ProviderError):
        bg.BerlinGroupAis().parse_transactions(body=_body(pending=["not an object"]))
    with pytest.raises(ProviderError):
        bg.BerlinGroupAis().parse_transactions(body=b"{}")
    with pytest.raises(ProviderError):
        bg.BerlinGroupAis().parse_transactions(body=_body(pending=[_row("x", amount="")]))


def test_a_decimal_string_amount_is_exact():
    t = bg.BerlinGroupAis().parse_transactions(body=_body(pending=[_row("x", amount="0.07")]))[0]
    assert t.amount_minor == 7


def test_verdict_carries_the_matched_amount_and_whether_it_is_still_pending():
    v = run([ob("INV-1001", amount=100_000), ob("INV-1001 part 2", amount=50_000, state="booked",
                                                 booked=datetime(2026, 11, 9, tzinfo=UTC))])
    assert v.matched_minor == 150_000 and v.pending_only is False
    assert run([ob("INV-1001")]).pending_only is True
    assert run([ob("nothing")]).matched_minor == 0


def test_a_vulnerability_flag_stops_automated_chasing_and_a_dispute_still_surfaces():
    rec = record(due=date(2026, 9, 1))
    assert rules.classify(rec, facts(), TODAY).label == "overdue_60"
    v = rules.classify(rec, facts(vulnerable=True), TODAY)
    assert (v.label, v.rule_id) == ("UNKNOWN", "vulnerability_hold")
    assert rules.classify(rec, facts(vulnerable=True, disputed=True), TODAY).label == "disputed"
    assert rules.classify(rec, facts(vulnerable=True, promised_by=date(2026, 11, 1)), TODAY).rule_id == "vulnerability_hold"
    with pytest.raises(ValueError):
        facts(vulnerable="yes")


def test_a_credit_with_a_matching_reference_but_part_of_the_amount_is_routed_not_held():
    from app import inbound_reader as ir

    v = run([ob("INV-1001", amount=1)])
    assert ir.check_claim(v, ir.Context("N", "INV-1001", 250_000, "eur")) == "found_part"


# ---------------- bank parser findings from the independent security review ----------------

def _pending_body(*rows, status="pending"):
    import json as _json

    return _json.dumps({"transactions": {status: list(rows)}}).encode()


def _line(desc="INV-1001", amount="2500.00", **extra):
    return {"transactionAmount": {"amount": amount, "currency": "EUR"}, "bookingDate": "2026-11-05",
            "remittanceInformationUnstructured": desc, **extra}


def test_a_line_with_no_id_gets_an_id_from_its_content_not_its_position():
    from app.bank.adapters import berlin_group as bg

    a, b = _line("INV-1001"), _line("OTHER", amount="10.00")
    first = bg.BerlinGroupAis().parse_transactions(body=_pending_body(a, b))
    swapped = bg.BerlinGroupAis().parse_transactions(body=_pending_body(b, a))
    ids_first = {t.description: t.transaction_id for t in first}
    ids_swapped = {t.description: t.transaction_id for t in swapped}
    assert ids_first == ids_swapped
    assert len(set(ids_first.values())) == 2 and all(i.startswith("derived-") for i in ids_first.values())


def test_a_pending_hold_does_not_renew_when_the_feed_reorders_unidentified_lines():
    from app.bank.adapters import berlin_group as bg

    credit, other = _line("INV-1001"), _line("OTHER", amount="10.00")
    seen: dict[str, date] = {}
    day1 = bg.BerlinGroupAis().parse_transactions(body=_pending_body(credit, other))
    f.observe(day1, seen, TODAY)
    seen.update({t.transaction_id: TODAY for t in day1})
    later = TODAY + timedelta(days=2)
    day3 = bg.BerlinGroupAis().parse_transactions(body=_pending_body(other, credit))
    obs = f.observe(day3, seen, later)
    assert [o.first_seen for o in obs if "INV" in o.txn.description] == [TODAY]


@pytest.mark.parametrize(
    "tx",
    [
        {"transactionAmount": "100"},
        {"transactionAmount": ["100"]},
        {"transactionAmount": {"amount": chr(0xB2), "currency": "EUR"}},
        {"transactionAmount": {"amount": "+-500.00", "currency": "EUR"}, "bookingDate": "2026-11-01"},
        {"transactionAmount": {"amount": "1" * 4400, "currency": "EUR"}, "bookingDate": "2026-11-01"},
        {"transactionAmount": {"amount": "1e3", "currency": "EUR"}, "bookingDate": "2026-11-01"},
        {"transactionAmount": {"amount": {"x": 1}, "currency": "EUR"}, "bookingDate": "2026-11-01"},
    ],
)
def test_malformed_bank_amounts_raise_provider_error_and_nothing_else(tx):
    from app.bank.adapters import berlin_group as bg
    from app.bank.provider import ProviderError

    with pytest.raises(ProviderError):
        bg.BerlinGroupAis().parse_transactions(body=_pending_body(tx))


def test_the_providers_documented_sample_parses_and_its_pending_line_gets_a_content_id():
    """The shape below is the GoCardless Bank Account Data quickstart sample (read 2026-11-23):
    booked lines carry transactionId, the pending line carries none."""
    import json as _json

    from app.bank.adapters import berlin_group as bg

    body = _json.dumps({
        "transactions": {
            "booked": [
                {"transactionId": "2020103000624289-1", "debtorName": "MON MOTHMA",
                 "transactionAmount": {"currency": "EUR", "amount": "45.00"},
                 "bookingDate": "2020-10-30", "valueDate": "2020-10-30",
                 "remittanceInformationUnstructured": "For the support of Restoration of the Republic foundation"},
                {"transactionId": "2020111101899195-1",
                 "transactionAmount": {"currency": "EUR", "amount": "-15.00"},
                 "bankTransactionCode": "PMNT", "bookingDate": "2020-11-11", "valueDate": "2020-11-11",
                 "remittanceInformationUnstructured": "PAYMENT Alderaan Coffe"},
            ],
            "pending": [
                {"transactionAmount": {"currency": "EUR", "amount": "-10.00"}, "valueDate": "2020-11-03",
                 "remittanceInformationUnstructured": "Reserved PAYMENT Emperor's Burgers"},
            ],
        }
    }).encode()
    txns = bg.BerlinGroupAis().parse_transactions(body=body)
    assert [t.amount_minor for t in txns] == [4500, -1500, -1000]
    assert [t.status for t in txns] == ["booked", "booked", "pending"]
    assert txns[0].transaction_id == "2020103000624289-1"
    assert txns[2].transaction_id.startswith("derived-")
    again = bg.BerlinGroupAis().parse_transactions(body=body)
    assert [t.transaction_id for t in again] == [t.transaction_id for t in txns]
