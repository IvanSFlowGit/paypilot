"""Five-state invoice reader: frozen synthetic rows, offline, no key."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import invoice_rules as rules
from app import invoice_source as inv

TODAY = date(2026, 11, 9)
SALT = "test-salt-not-a-secret"
CASES = json.loads((Path(__file__).parent / "fixtures" / "invoice_rules_cases.json").read_text())


def build(c):
    due = None if c["days_overdue"] is None else TODAY - timedelta(days=c["days_overdue"])
    record = inv.InvoiceRecord(
        source="csv", source_id=c["id"], invoice_no="INV-" + c["id"],
        customer_ref=inv.make_customer_ref("Synthetic Co", SALT),
        amount_minor=10_000, currency="usd", status=c["status"], due=due,
    )
    promised = None if c["promise_offset"] is None else TODAY + timedelta(days=c["promise_offset"])
    facts = rules.InvoiceFacts(
        chases_sent=c["chases"], replies_received=c["replies"],
        disputed=bool(c["disputed"]), promised_by=promised,
    )
    return record, facts


@pytest.mark.parametrize("c", CASES, ids=[c["id"] for c in CASES])
def test_every_fixture_row_gets_its_frozen_verdict(c):
    v = rules.classify(*build(c), TODAY)
    assert (v.label, v.rule_id) == (c["expect_label"], c["expect_rule"])


def test_the_fixture_is_not_trivially_confident():
    labels = [c["expect_label"] for c in CASES]
    assert "UNKNOWN" in labels
    assert set(rules.LABELS) <= set(labels)


def test_the_reader_says_unknown_on_a_real_share_of_rows():
    out = [rules.classify(*build(c), TODAY).label for c in CASES]
    assert out.count("UNKNOWN") >= len(out) // 4


def get(i):
    return next(c for c in CASES if c["id"] == i)


def test_disputed_beats_overdue_60():
    assert rules.classify(*build(get("disputed_d90")), TODAY).label == "disputed"


def test_disputed_beats_a_lapsed_promise():
    assert rules.classify(*build(get("disputed_and_promise_lapsed")), TODAY).label == "disputed"


def test_promised_not_paid_beats_no_reply():
    assert rules.classify(*build(get("promise_lapsed_and_no_reply")), TODAY).label == "promised_not_paid"


def test_promised_not_paid_beats_overdue_60():
    assert rules.classify(*build(get("promise_lapsed_and_d90")), TODAY).label == "promised_not_paid"


def test_no_reply_beats_overdue_60():
    assert rules.classify(*build(get("no_reply_and_d90")), TODAY).label == "no_reply"


def test_boundaries_live_in_named_constants():
    assert (rules.FIRST_CHASE_DAYS, rules.FINANCE_ESCALATION_DAYS, rules.NO_REPLY_CHASES) == (30, 60, 2)


def test_a_paid_invoice_with_a_stale_dispute_flag_needs_no_human():
    assert rules.classify(*build(get("paid_with_stale_dispute_flag")), TODAY).rule_id == "not_open"


@pytest.mark.parametrize("kw", [{"chases_sent": -1}, {"chases_sent": True}, {"replies_received": -1}, {"disputed": 1}])
def test_bad_facts_are_refused(kw):
    with pytest.raises(ValueError):
        rules.InvoiceFacts(**kw)


def test_the_module_has_no_model_network_or_write_path():
    src = Path(rules.__file__).read_text()
    for banned in ("import requests", "import urllib", "import socket", "anthropic", "openai", "open(", "datetime.now", "date.today"):
        assert banned not in src
