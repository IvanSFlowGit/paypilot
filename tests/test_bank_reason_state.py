"""The third reason state, which is the protocol change the ASN Bank read forced.

WHAT LOOKS LIKE A BUG HERE AND IS NOT. Several tests below assert that a
returned debit produces a 202 with `handled: false`. That is the designed
answer: the rail table is deliberately NOT consulted when there is no
reason code, because a lookup with no code returns an answer about nothing.

THE SHAPE OF THE PROBLEM. docs/claims.md row 12 measures that a return
reason CAN survive into a PSD2 AIS feed and that it is OPTIONAL
(Mandatory N). So a feed reporting a returned debit with no reason is
conformant, and there are two different absences:

    absent_field_supported  this ASPSP exposes the field, this row has none
    unsupported_by_aspsp    this ASPSP exposes no such field at all

Collapsing either into "not a return" loses a real event. Collapsing them
into each other loses the only thing that says whether asking again would
help. Both collapses are mutation-tested in test_bank_mutations.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.bank import fake, provider as bp, routes as br
from app.bank.provider import (
    REASON_ABSENT_SUPPORTED,
    REASON_PRESENT,
    REASON_UNSUPPORTED,
    ProviderError,
    ReturnEvent,
)

FIXED = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)


def _event(**over) -> dict:
    base = dict(
        rail="sepa",
        code="AC04",
        account_id="acct-1",
        amount_minor=-4500,
        currency="EUR",
        occurred_at=FIXED,
        raw_diagnostic="t",
    )
    base.update(over)
    return base


# --- the invariant, in both directions -------------------------------------
# A state and its payload cannot disagree. One direction alone is half a
# guard: without the first a present state can carry nothing, without the
# second an absent state can carry a code and the caller will use it.


def test_present_with_no_code_is_refused():
    with pytest.raises(ProviderError) as exc:
        ReturnEvent(**_event(code=None, reason_state=REASON_PRESENT))
    assert "present" in str(exc.value)


def test_present_with_empty_code_is_refused():
    """Empty string is the shape a stripped CSV field arrives in, and it is
    not a code. Falsy rather than None, so a truthiness check catches it and
    an `is None` check does not."""
    with pytest.raises(ProviderError):
        ReturnEvent(**_event(code="", reason_state=REASON_PRESENT))


@pytest.mark.parametrize("state", [REASON_ABSENT_SUPPORTED, REASON_UNSUPPORTED])
def test_an_absent_state_carrying_a_code_is_refused(state):
    with pytest.raises(ProviderError) as exc:
        ReturnEvent(**_event(code="AC04", reason_state=state))
    assert state in str(exc.value)


@pytest.mark.parametrize("state", [REASON_ABSENT_SUPPORTED, REASON_UNSUPPORTED])
def test_an_absent_state_with_no_code_is_accepted(state):
    ev = ReturnEvent(**_event(code=None, reason_state=state))
    assert ev.code is None
    assert ev.reason_state == state


def test_an_unknown_state_is_refused_and_names_the_permitted_ones():
    with pytest.raises(ProviderError) as exc:
        ReturnEvent(**_event(code=None, reason_state="probably_fine"))
    msg = str(exc.value)
    for state in bp.REASON_STATES:
        assert state in msg, "the refusal does not say what it would accept"


def test_present_is_the_default_so_existing_callers_are_unchanged():
    assert ReturnEvent(**_event()).reason_state == REASON_PRESENT


# --- the fake, so the state is drivable with no network --------------------


def _parse(fields: str):
    """Field separator is `;`, read from fake.parse_return_event rather than
    assumed. A newline-separated body parses to one field and returns None,
    which reads as "not a return" and would have passed three of these
    tests for the wrong reason."""
    # enabled=True rather than the env var: the fake REFUSES construction
    # otherwise, by design, so it cannot be reached by a configuration typo.
    return fake.FakeBank(enabled=True).parse_return_event(
        body=fields.encode("utf-8")
    )


def test_the_fake_emits_present_when_a_code_is_given():
    ev = _parse("rail=sepa;code=AC04;account=a;amount=-4500;currency=EUR")
    assert ev is not None
    assert (ev.code, ev.reason_state) == ("AC04", REASON_PRESENT)


def test_the_fake_emits_the_supported_absence_by_default():
    ev = _parse("rail=sepa;returned=1;account=a;amount=-4500;currency=EUR")
    assert ev is not None
    assert ev.code is None
    assert ev.reason_state == REASON_ABSENT_SUPPORTED


def test_the_fake_can_emit_the_unsupported_absence():
    ev = _parse(
        "rail=sepa;returned=1;reason_state=unsupported_by_aspsp;"
        "account=a;amount=-4500;currency=EUR"
    )
    assert ev is not None
    assert ev.reason_state == REASON_UNSUPPORTED


def test_no_code_and_no_returned_flag_is_not_a_return():
    """THE CASE THAT KEEPS THE THIRD STATE HONEST. Without this, emitting
    the absence for every codeless row would turn every ordinary credit into
    a returned debit, which is the inference the adapter's own docstring
    refuses for the same reason."""
    assert _parse("rail=sepa;account=a;amount=4500;currency=EUR") is None


# --- the route, which must not consult the rail table ----------------------


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(fake.ENABLE_ENV, "1")
    yield


def _post(fields: str):
    body = fields.encode("utf-8")
    return br.handle_bank_webhook(
        provider="fake",
        body=body,
        headers={fake.SIGNATURE_HEADER: fake.sign_body(body)},
    )


def test_a_coded_return_is_decided(enabled):
    status, payload = _post(
        "rail=sepa;code=AC04;account=a;amount=-4500;currency=EUR"
    )
    assert status == 202
    assert payload["decision"]["abstains"] is False
    assert payload["decision"]["code"] == "AC04"


def test_a_codeless_return_abstains_and_says_which_absence(enabled):
    status, payload = _post(
        "rail=sepa;returned=1;account=a;amount=-4500;currency=EUR"
    )
    assert status == 202
    d = payload["decision"]
    assert d["abstains"] is True
    assert d["code"] is None
    assert d["reason_state"] == REASON_ABSENT_SUPPORTED
    assert "sepa" in d["abstain_reason"]


def test_the_two_absences_are_distinguishable_on_the_payload(enabled):
    """THE DISCRIMINATING TEST. Both absences abstain with the same keys, so
    every per-case assertion above passes under a mutation that collapses
    them into one generic absence. Only comparing them catches that."""
    seen = {}
    for state in (REASON_ABSENT_SUPPORTED, REASON_UNSUPPORTED):
        _, payload = _post(
            f"rail=sepa;returned=1;reason_state={state};"
            "account=a;amount=-4500;currency=EUR"
        )
        seen[state] = payload["decision"]
    assert seen[REASON_ABSENT_SUPPORTED] != seen[REASON_UNSUPPORTED]
    assert (
        seen[REASON_ABSENT_SUPPORTED]["abstain_reason"]
        != seen[REASON_UNSUPPORTED]["abstain_reason"]
    ), "the two absences give the same reason, so the state carries nothing"


def test_the_rail_table_is_never_consulted_without_a_code(enabled, monkeypatch):
    """A lookup with no code returns an answer about nothing, so the right
    behaviour is not to ask. Asserting on the call rather than on the output,
    because a table that happened to return an abstention would make an
    incorrect lookup invisible."""
    from app import rails

    calls: list = []
    real = rails.lookup

    def spy(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(rails, "lookup", spy)

    _post("rail=sepa;returned=1;account=a;amount=-4500;currency=EUR")
    assert calls == [], "the rail table was consulted with no code"

    _post("rail=sepa;code=AC04;account=a;amount=-4500;currency=EUR")
    assert len(calls) == 1, "the control did not fire, so the spy proves nothing"


def test_not_a_return_at_all_is_200_and_not_unattributed(enabled):
    """200 rather than 202, because nothing was dropped. A feed row that is
    not a return is a successful no-op, and reporting it as unattributed
    would fill the audit log with ordinary traffic."""
    status, payload = _post("rail=sepa;account=a;amount=4500;currency=EUR")
    assert status == 200
    assert payload["reason"] == "no_return_code"
