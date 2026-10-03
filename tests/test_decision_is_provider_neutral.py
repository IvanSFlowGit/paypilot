"""The decision response must never name a payment provider.

WHY. The product was described for months as a Stripe and Recharge product, and the
agency conversations all met the same wall. Measured 2026-10-03, the decision engine is
provider agnostic: app/decision.py imports only hmac, re and datetime, decide() is pure,
and the contract is two required fields, invoice_id and failure_code, plus attempt and
prior_failures which default to 1 and 0. The two provider modules are convenience
adapters onto the same engine.

The one exception was copy rather than logic. STRATEGY_RULES["direct_debit_not_retried"]
shipped an offer reading "and Stripe is not retrying it", so a merchant on any other rail
got advice naming a processor they do not use. One of seven rules, in the one field that
is written for the merchant's customer to act on.

This test exists because that is a string, and a string comes back. It walks EVERY rule
rather than a sample, in both the plain and the escalated path, because strategy_for takes
an escalate flag and a leak could hide in either branch.
"""

import json

import pytest

from app.decision import STRATEGY_RULES, decide, parse_input

PROVIDERS = ("Stripe", "stripe", "Recharge", "recharge", "Shopify", "shopify",
             "Adyen", "Braintree", "PayPal")

# Every rule key, plus a code the table has never seen so the default path is covered too.
CODES = sorted(STRATEGY_RULES) + ["code_no_table_has_ever_seen"]


def _strategy(code, *, escalate):
    body = {"invoice_id": "INV-1", "failure_code": code}
    if escalate:
        body["attempt"] = 9
        body["prior_failures"] = 9
    return decide(parse_input(body))["strategy"]


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("escalate", [False, True])
def test_no_provider_name_in_the_decision_response(code, escalate):
    blob = json.dumps(_strategy(code, escalate=escalate))
    named = [p for p in PROVIDERS if p in blob]
    assert not named, (
        f"rule {code!r} (escalate={escalate}) names {named} in the response a merchant "
        f"on another rail would read"
    )


def test_the_sweep_actually_covers_every_rule():
    """A parametrised test that silently covered nothing would pass forever.

    Canon: a control validates the instrument at the moment of measurement, not once at
    build time. If a rule is added to the table and this list is not regenerated, this
    fails rather than quietly testing a stale subset.
    """
    assert len(CODES) == len(STRATEGY_RULES) + 1
    assert set(CODES) - {"code_no_table_has_ever_seen"} == set(STRATEGY_RULES)


def test_two_fields_are_enough_to_get_a_decision():
    """The contract an agency integrates against: an invoice id and a failure code.

    This is the sentence the product is sold on, so it is a test rather than a doc line.
    """
    parsed = parse_input({"invoice_id": "INV-1", "failure_code": "card_expired"})
    assert parsed["attempt"] == 1
    assert parsed["prior_failures"] == 0


def test_an_unknown_failure_code_is_a_decision_and_not_an_error():
    """A table that refused unknown codes would make every new platform a blocker.

    Falling through to "default" is what turns an incomplete mapping into a scoped job:
    the before and after a client can read is default becoming their own code.
    """
    out = decide(parse_input({"invoice_id": "INV-1", "failure_code": "vtex_boleto_expirado"}))
    assert out["rule_fired"] == "default"
    assert out["strategy"]["action"]
