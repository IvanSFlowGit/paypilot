"""The Berlin Group AIS adapter: a per-ASPSP field map and honest refusals.

WHAT LOOKS LIKE A GAP HERE AND IS DELIBERATE. Four of the seven protocol
methods raise instead of returning a value, and so does verify_webhook.
There is no ASPSP credential on this machine, and an empty tuple of
accounts is indistinguishable from a consent that really covers none,
while `verify_webhook` returning False asserts that a signature was BAD
when the truth is that no signing secret is configured. One is a claim
about the provider, the other is a claim about us.

THE HALF THAT WORKS WITH NO CREDENTIAL is the half the plan's open
question turned on: taking a transaction and deciding whether it is a
return, and which of the three reason states applies. That is exercised
end to end below.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.bank import provider as bp
from app.bank.adapters import berlin_group as bg
from app.bank.provider import (
    REASON_ABSENT_SUPPORTED,
    REASON_PRESENT,
    REASON_UNSUPPORTED,
    ProviderError,
)


def _tx(**over) -> bytes:
    """A Berlin Group transaction object, in the standard's own envelope."""
    tx = {
        "transactionAmount": {"amount": "-45.00", "currency": "EUR"},
        "bookingDate": "2026-03-04",
        "internalTransactionId": "tx-1",
        "returnInformationCode": "AC04",
    }
    tx.update(over)
    return json.dumps({"transactions": {"booked": [tx]}}).encode("utf-8")


# --- registration, which is the import-time deny-list doing its job ------


def test_the_adapter_is_registered_under_its_own_name():
    assert "berlin_group_ais" in bp.providers()


def test_every_endpoint_is_a_read_and_none_names_a_payment():
    """The registry refuses a payment-initiation endpoint at import, so this
    asserts the ENDPOINTS table rather than re-testing the registry: the
    adapter passing import is only evidence if the table is also read."""
    joined = " ".join(bg.BerlinGroupAis.ENDPOINTS.values())
    for forbidden in ("payment", "transfer", "credit-transfer", "initiat"):
        assert forbidden not in joined.lower(), forbidden
    assert "/v1/accounts" in joined and "/v1/consents" in joined


def test_no_endpoint_points_at_a_resolvable_host():
    """RFC 2606 reserves .invalid, so nothing here can reach a real bank by
    accident. A placeholder that happened to resolve is the shape that gets
    a request sent somewhere nobody intended."""
    for url in bg.BerlinGroupAis.ENDPOINTS.values():
        assert ".invalid" in url, url


# --- the profile: absent and never-looked are different facts ------------


def test_an_unknown_aspsp_is_refused_and_names_what_it_knows():
    with pytest.raises(ProviderError) as exc:
        bg.BerlinGroupAis(aspsp="some-bank-nobody-read")
    assert "asn" in str(exc.value), "the refusal does not say what it accepts"


def test_every_shipped_profile_names_its_source_and_its_read_date():
    """A profile with no source is a guessed field name wearing a record's
    clothes, and a guessed name reports every return as having no reason."""
    assert bg.PROFILES, "no profiles at all"
    for key, prof in bg.PROFILES.items():
        assert prof.source and prof.read_on, key
        assert prof.read_on.count("-") == 2, prof.read_on


def test_the_unsupported_question_is_asked_before_the_per_row_question():
    """THE ORDERING IS THE WHOLE POINT. A provider exposing no field at all
    and a row that happens to carry none are different facts, and asking
    only the second question cannot tell them apart."""
    exposes = bg.AspspProfile("x", "returnInformationCode", "sepa", "s", "2026-01-01")
    exposes_not = bg.AspspProfile("y", None, "sepa", "s", "2026-01-01")

    assert exposes.reason_state_for({}) == (None, REASON_ABSENT_SUPPORTED)
    assert exposes_not.reason_state_for({}) == (None, REASON_UNSUPPORTED)
    # and the second one stays unsupported even when a row DOES carry a code,
    # because a field this ASPSP does not publish is not a field we read.
    assert exposes_not.reason_state_for(
        {"returnInformationCode": "AC04"}
    ) == (None, REASON_UNSUPPORTED)


def test_an_empty_string_is_an_absence_rather_than_a_code():
    """A stripped CSV or a cleared JSON field arrives as empty, not as null,
    and an empty code would fail the ReturnEvent invariant on construction."""
    prof = bg.PROFILES["asn"]
    assert prof.reason_state_for({"returnInformationCode": "   "}) == (
        None,
        REASON_ABSENT_SUPPORTED,
    )


# --- the three states, through the real parse path -----------------------


def _parse(**over):
    return bg.BerlinGroupAis().parse_return_event(body=_tx(**over))


def test_a_coded_return_is_present_and_keeps_the_scheme_code():
    ev = _parse()
    assert ev is not None
    assert (ev.code, ev.reason_state) == ("AC04", REASON_PRESENT)
    assert ev.rail == "sepa"
    assert ev.amount_minor == -4500
    assert ev.occurred_at == datetime(2026, 3, 4, tzinfo=UTC)


def test_a_declared_return_with_no_code_is_the_supported_absence():
    ev = _parse(returnInformationCode=None, returnIndicator=True)
    assert ev is not None
    assert ev.code is None
    assert ev.reason_state == REASON_ABSENT_SUPPORTED


def test_the_diagnostic_names_the_field_and_the_aspsp():
    """An abstention a human has to act on must say which provider and which
    field produced it, or the reader cannot tell a gap from a bad read."""
    ev = _parse(returnInformationCode=None, returnIndicator=True)
    assert "returnInformationCode" in ev.raw_diagnostic
    assert "ASN" in ev.raw_diagnostic


def test_an_ordinary_credit_is_not_read_as_a_returned_debit():
    """THE CASE THAT KEEPS THE WHOLE THING HONEST. A return flips the amount
    sign, so inferring from a positive amount would classify every incoming
    payment as a returned debit. Nothing here infers that."""
    body = _tx(returnInformationCode=None)
    tx = json.loads(body)["transactions"]["booked"][0]
    tx["transactionAmount"]["amount"] = "45.00"
    envelope = json.dumps({"transactions": {"booked": [tx]}}).encode("utf-8")
    assert bg.BerlinGroupAis().parse_return_event(body=envelope) is None


def test_a_bare_transaction_object_parses_as_well_as_the_envelope():
    """A caller holding one real response should not have to unwrap it to
    find out whether it parses."""
    tx = json.loads(_tx())["transactions"]["booked"][0]
    ev = bg.BerlinGroupAis().parse_return_event(
        body=json.dumps(tx).encode("utf-8")
    )
    assert ev is not None and ev.code == "AC04"


def test_a_pending_only_response_is_read_too():
    tx = json.loads(_tx())["transactions"]["booked"][0]
    body = json.dumps({"transactions": {"pending": [tx]}}).encode("utf-8")
    assert bg.BerlinGroupAis().parse_return_event(body=body) is not None


def test_an_empty_feed_is_not_a_return_and_does_not_raise():
    body = json.dumps({"transactions": {"booked": []}}).encode("utf-8")
    assert bg.BerlinGroupAis().parse_return_event(body=body) is None


def test_a_body_that_is_not_json_is_refused_rather_than_read_as_empty():
    with pytest.raises(ProviderError):
        bg.BerlinGroupAis().parse_return_event(body=b"<html>login</html>")


# --- money, which is the half a float quietly corrupts -------------------


@pytest.mark.parametrize(
    "amount,minor",
    [("-45.00", -4500), ("45.00", 4500), ("-0.07", -7), ("0.1", 10),
     ("-1234.56", -123456), ("12", 1200), ("+3.05", 305)],
)
def test_amounts_are_parsed_as_decimal_strings(amount, minor):
    """0.07 has no exact float, so a float parse of a returned debit is a
    rounding error in somebody's money. These are string arithmetic."""
    assert bg._minor(amount, "EUR") == minor


def test_a_currency_with_no_configured_exponent_is_refused():
    """Scaling a three-decimal or zero-decimal currency by 100 is a guess
    about an amount, so it refuses rather than returning a wrong integer.

    TWO decimals on purpose. The first version of this test used "45.000",
    which the decimal-length refusal catches first, so it passed with the
    currency check deleted and the mutation reddened nothing. Both refusals
    name the currency, so asserting on "BHD" could not separate them
    either: the amount AND the assertion both had to change."""
    with pytest.raises(ProviderError) as exc:
        bg._minor("45.00", "BHD")
    msg = str(exc.value)
    assert "exponent" in msg, "a different guard refused this, not the currency one"
    assert "BHD" in msg


def test_an_unparseable_amount_is_refused():
    for bad in ("", "   ", "forty five", "4,500.00"):
        with pytest.raises(ProviderError):
            bg._minor(bad, "EUR")


def test_a_booking_date_with_no_zone_is_read_as_utc_rather_than_local():
    """A naive datetime compared against an aware one raises, and a naive one
    silently read as local time moves a cohort a day in reporting."""
    assert bg._utc("2026-03-04").tzinfo is UTC
    assert bg._utc("2026-03-04T09:00:00Z") == datetime(
        2026, 3, 4, 9, tzinfo=UTC
    )


def test_a_missing_or_unparseable_booking_date_is_refused():
    for bad in ("", None, "the fourth of March"):
        with pytest.raises(ProviderError):
            bg._utc(bad)


# --- the refusals, which are the honest answer to having no credential ---


@pytest.mark.parametrize(
    "call",
    [
        lambda a: a.exchange_code(code="c", redirect_uri="https://x.invalid/cb"),
        lambda a: a.refresh(refresh_token="r"),
        lambda a: a.accounts(access_token="t"),
        lambda a: a.transactions(
            access_token="t",
            account_id="a",
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        lambda a: a.authorize_url(
            state="s", redirect_uri="https://x.invalid/cb", scopes=("ais",)
        ),
        lambda a: a.verify_webhook(body=b"{}", headers={}),
    ],
)
def test_what_needs_a_credential_refuses_rather_than_returning_empty(call):
    """An empty tuple of accounts reads as a real answer of nothing, and
    False from verify_webhook reads as a bad signature. Both are claims the
    adapter is not entitled to make."""
    with pytest.raises(ProviderError):
        call(bg.BerlinGroupAis())


def test_the_refusal_says_what_is_missing_rather_than_only_that_it_failed():
    adapter = bg.BerlinGroupAis()
    with pytest.raises(ProviderError) as exc:
        adapter.accounts(access_token="t")
    msg = str(exc.value)
    assert "accounts" in msg, "the refusal does not name the method"
    assert "credential" in msg.lower(), "the refusal does not name the cause"


# --- the case the first version of this file could not discriminate -----
# THE MUTATION FOUND THIS, NOT THE TEST. Replacing the string parse with
# int(round(float(x) * 100)) left every amount above GREEN, because float
# happens to give the right answer for -45.00, -0.07 and the rest. Searched
# across 200,000 three-decimal amounts, 90,000 of them diverge between the
# two readings, so a third decimal is where the two implementations differ
# and nothing else in the table was ever going to tell them apart. For
# amounts a real feed could carry the two agree exactly, which is why the
# last test in this file reaches 10**14 to pin the arithmetic at all.
#
# And the search found a real defect rather than only a weak test: the
# original parser TRUNCATED the third decimal, so "0.006" returned 0 and
# reported it as an answer. It refuses now.


@pytest.mark.parametrize("amount", ["0.006", "0.015", "-1.005", "45.123"])
def test_more_decimals_than_the_currency_has_is_refused(amount):
    with pytest.raises(ProviderError) as exc:
        bg._minor(amount, "EUR")
    assert "truncat" in str(exc.value), "the refusal does not say why"


def test_two_decimals_and_fewer_still_parse():
    """The quiet half of the widening: refusing three decimals must not
    refuse the ordinary amounts, or the check gets switched off."""
    assert bg._minor("45", "EUR") == 4500
    assert bg._minor("45.0", "EUR") == 4500
    assert bg._minor("45.07", "EUR") == 4507


def test_the_decimal_parse_is_pinned_where_a_float_would_drift():
    """THE ONE INPUT WHERE THE TWO READINGS DIVERGE, and the only test in
    this file that distinguishes the string parse from int(round(x * 100)).

    Searched: every two-decimal amount across sixteen magnitudes. Float and
    string agree on all of them until 10**14, where the mantissa runs out
    and 100000000000000.01 parses as ...002. So for any amount a real feed
    could carry the two implementations are identical and no plausible
    fixture can tell them apart. That is why the decimal-length refusal
    above is the thing carrying the safety, and this case exists so the
    implementation cannot be swapped for a float without a red row.
    """
    assert bg._minor("100000000000000.01", "EUR") == 10000000000000001
    assert int(round(float("100000000000000.01") * 100)) == 10000000000000002
