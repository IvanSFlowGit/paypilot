"""Regression tests for defects found by an adversarial audit.

Each test names the concrete attack or failure it prevents. These are the
findings, not hypotheticals: every one was reproduced against this code before
being fixed.
"""

from __future__ import annotations

import pytest

from app import loop, mailer, stripe_client
from app.money import minor_to_major
from app.safety import find_foreign_urls
from app.store import Store

# ---------------------------------------------------------------------------
# Link allowlist: parser-confusion bypass
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://phish.attacker.test#@invoice.stripe.com/pay/in_123",
    "https://evil.test?x=@billing.stripe.com",
    "https://evil.test:8080#@billing.stripe.com",
    "https://evil.test\\@billing.stripe.com",
    "https://user:pw@evil.test/billing.stripe.com",
    "HTTPS://EVIL.TEST#@BILLING.STRIPE.COM",
    "https://billing.stripe.com.evil.test/x",
])
def test_host_confusion_urls_are_blocked(url):
    """Splitting on "/" and "@" let a phishing link through as the sanctioned
    card-update URL. A browser treats "#", "?" and "\\" as delimiters too, so
    the allowlisted host ended up in the fragment while the real host was the
    attacker's."""
    assert find_foreign_urls(url) == [url]


@pytest.mark.parametrize("url", [
    "https://billing.stripe.com/p/session/test_abc",
    "https://invoice.stripe.com/i/acct_1/xyz",
    "https://pay.stripe.com/invoice/abc",
])
def test_genuine_stripe_links_still_pass(url):
    assert find_foreign_urls(url) == []


def test_a_crafted_link_from_stripe_is_rejected(monkeypatch):
    """recovery_link re-checks whatever the upstream returned."""
    monkeypatch.setattr(
        stripe_client, "create_portal_session",
        lambda cid: "https://evil.test#@billing.stripe.com/p/session/x",
    )
    from app.safety import PAYMENT_UPDATE_URL

    assert stripe_client.recovery_link(stripe_customer_id="cus_1") == PAYMENT_UPDATE_URL


# ---------------------------------------------------------------------------
# PII must not reach the model
# ---------------------------------------------------------------------------

def test_event_pii_is_stripped_before_the_prompt():
    """The customer record was masked, but the prompt also embedded a repr of
    the raw event, and Stripe's customer_name/customer_email live there."""
    from app.nodes import _event_for_prompt

    event = {
        "customer_id": "cus_1", "amount": 49.0, "failure_code": "card_expired",
        "customer_name": "Alice Smith", "customer_email": "alice@example.test",
    }
    text = str(_event_for_prompt(event))
    assert "Alice Smith" not in text
    assert "alice@example.test" not in text
    assert "card_expired" in text, "non-PII fields must survive"


# ---------------------------------------------------------------------------
# Dedupe must not swallow an event that failed to process
# ---------------------------------------------------------------------------

def test_a_failed_event_can_be_retried(tmp_path):
    """Claiming an event then failing to process it is worse than not claiming
    it: Stripe retries, the retry hits the dedupe row, and the event is lost.
    A lost invoice.paid is revenue never recorded as recovered."""
    store = Store(tmp_path / "t.db")
    try:
        assert store.mark_event_seen("evt_1", "invoice.paid") is True
        store.forget_event("evt_1")  # processing raised
        assert store.mark_event_seen("evt_1", "invoice.paid") is True
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Currency exponents
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("minor,currency,expected", [
    (500000, "jpy", 500000.0),   # zero-decimal: /100 under-reported by 100x
    (4900, "eur", 49.0),
    (4900, "usd", 49.0),
    (4900, "kwd", 4.9),          # three-decimal: /100 over-reported by 10x
    (0, "jpy", 0.0),
])
def test_amounts_respect_the_currency_exponent(minor, currency, expected):
    assert minor_to_major(minor, currency) == expected


def test_report_uses_the_right_exponent(isolated_store):
    from app.report import build_report

    isolated_store.record_failure(
        invoice_id="in_jp", customer_id="c", amount_minor=500000,
        currency="jpy", failure_code="card_expired",
    )
    bucket = build_report(isolated_store)["by_currency"]["jpy"]
    assert bucket["failed_value"] == 500000.0


# ---------------------------------------------------------------------------
# Sequence cap and cooldown: business-level dedup
# ---------------------------------------------------------------------------

def _row(invoice_id="in_1", customer="cus_1"):
    return {
        "customer_email": "me@mine.test", "failure_code": "card_expired",
        "stripe_customer_id": customer, "customer_id": customer,
    }


def _record(store, invoice_id="in_1", customer="cus_1"):
    """deliver_recovery transitions to messaged on a send, which needs the row."""
    store.record_failure(
        invoice_id=invoice_id, customer_id=customer, amount_minor=4900,
        currency="eur", failure_code="card_expired", stripe_customer_id=customer,
    )


@pytest.fixture
def sending_on(monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")
    monkeypatch.setattr(stripe_client, "create_portal_session",
                        lambda cid: "https://billing.stripe.com/p/session/x")

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_1"}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    return monkeypatch


def test_touches_are_capped_per_invoice(isolated_store, sending_on):
    """Stripe emits invoice.payment_failed once per retry, each with its own
    event id, so event dedupe does not bound this. Without a cap, five retries
    meant five emails to the same person."""
    sending_on.setenv("PAYPILOT_MAX_TOUCHES", "2")
    sending_on.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "0")
    _record(isolated_store)

    statuses = [
        loop.deliver_recovery(invoice_id="in_1", recovery={"message": "hi"},
                              row=_row(), store=isolated_store)["status"]
        for _ in range(5)
    ]
    assert statuses.count(mailer.STATUS_SENT) == 2
    assert statuses[-1] == "suppressed"
    assert isolated_store.sent_message_count("in_1") == 2


def test_a_cooldown_blocks_a_second_email_about_the_same_invoice(isolated_store, sending_on):
    """Business-level dedup, not transport dedup: two DIFFERENT, legitimate
    events about the same invoice must not both reach the customer."""
    sending_on.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "24")
    _record(isolated_store)

    first = loop.deliver_recovery(invoice_id="in_1", recovery={"message": "hi"},
                                  row=_row(), store=isolated_store)
    second = loop.deliver_recovery(invoice_id="in_1", recovery={"message": "hi"},
                                   row=_row(), store=isolated_store)

    assert first["status"] == mailer.STATUS_SENT
    assert second["status"] == "suppressed"
    assert second["error"] == "cooldown_active"
    assert second["hours_remaining"] > 23


def test_the_cooldown_also_spans_invoices_for_one_customer(isolated_store, sending_on):
    """A billing run failing three of one customer's invoices produces three
    distinct events. They must not become three emails within minutes."""
    sending_on.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "24")

    isolated_store.record_failure(invoice_id="in_a", customer_id="cus_1",
                                  amount_minor=1000, currency="eur",
                                  failure_code="card_expired",
                                  stripe_customer_id="cus_1")
    isolated_store.record_failure(invoice_id="in_b", customer_id="cus_1",
                                  amount_minor=2000, currency="eur",
                                  failure_code="card_expired",
                                  stripe_customer_id="cus_1")

    a = loop.deliver_recovery(invoice_id="in_a", recovery={"message": "hi"},
                              row=_row("in_a"), store=isolated_store)
    b = loop.deliver_recovery(invoice_id="in_b", recovery={"message": "hi"},
                              row=_row("in_b"), store=isolated_store)

    assert a["status"] == mailer.STATUS_SENT
    assert b["status"] == "suppressed"
    assert b["error"] == "cooldown_active"


def test_a_different_customer_is_not_blocked(isolated_store, sending_on):
    sending_on.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "24")

    isolated_store.record_failure(invoice_id="in_a", customer_id="cus_1",
                                  amount_minor=1000, currency="eur",
                                  failure_code="card_expired",
                                  stripe_customer_id="cus_1")
    isolated_store.record_failure(invoice_id="in_b", customer_id="cus_2",
                                  amount_minor=1000, currency="eur",
                                  failure_code="card_expired",
                                  stripe_customer_id="cus_2")

    loop.deliver_recovery(invoice_id="in_a", recovery={"message": "hi"},
                          row=_row("in_a", "cus_1"), store=isolated_store)
    b = loop.deliver_recovery(invoice_id="in_b", recovery={"message": "hi"},
                              row=_row("in_b", "cus_2"), store=isolated_store)
    assert b["status"] == mailer.STATUS_SENT


def test_a_dry_run_does_not_start_a_cooldown(isolated_store, monkeypatch):
    """Only mail that reached a person may suppress the next send."""
    monkeypatch.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "24")
    monkeypatch.setattr(stripe_client, "create_portal_session",
                        lambda cid: "https://billing.stripe.com/p/session/x")
    _record(isolated_store)

    first = loop.deliver_recovery(invoice_id="in_1", recovery={"message": "hi"},
                                  row=_row(), store=isolated_store)
    assert first["status"] == mailer.STATUS_DRY_RUN
    assert loop._cooldown_remaining(
        isolated_store, invoice_id="in_1", stripe_customer_id="cus_1"
    ) == 0.0
