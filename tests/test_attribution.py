"""Tests for holdout assignment and the recovery report.

The failure mode being defended against is not a crash. It is a dashboard that
reads well and is wrong: crediting the tool for invoices it never touched,
quoting a lift from four data points, or summing euros into dollars.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import loop, report
from app.attribution import bucket_of, holdout_pct, is_holdout
from app.store import STATE_MESSAGED, STATE_RECOVERED


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    return monkeypatch


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("PAYPILOT_HOLDOUT_PCT", "PAYPILOT_HOLDOUT_SEED", "ADMIN_TOKEN",
                "PAYPILOT_SEND_EMAIL", "PAYPILOT_ALLOWED_RECIPIENTS", "RESEND_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Holdout assignment
# ---------------------------------------------------------------------------

def test_holdout_is_off_by_default():
    """Nobody is denied a recovery attempt because a config value was left
    at a demo setting."""
    assert holdout_pct() == 0
    assert is_holdout("in_anything") is False


def test_assignment_is_deterministic(monkeypatch):
    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", "50")
    first = [is_holdout(f"in_{i}") for i in range(50)]
    second = [is_holdout(f"in_{i}") for i in range(50)]
    assert first == second


def test_assignment_survives_a_restart():
    """SHA-256, not Python's salted hash(), which would reassign on restart."""
    assert bucket_of("in_1", seed="fixed") == bucket_of("in_1", seed="fixed")
    assert bucket_of("in_1", seed="fixed") != bucket_of("in_2", seed="fixed")


def test_a_different_seed_reshuffles(monkeypatch):
    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", "50")
    a = [is_holdout(f"in_{i}", seed="seed-a") for i in range(200)]
    b = [is_holdout(f"in_{i}", seed="seed-b") for i in range(200)]
    assert a != b


def test_split_is_roughly_the_requested_share(monkeypatch):
    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", "20")
    held = sum(is_holdout(f"in_{i}") for i in range(2000))
    assert 300 < held < 500, f"expected about 400 of 2000, got {held}"


def test_hundred_percent_holds_everything(monkeypatch):
    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", "100")
    assert all(is_holdout(f"in_{i}") for i in range(100))


@pytest.mark.parametrize("value,expected", [("-5", 0), ("500", 100), ("banana", 0), ("", 0)])
def test_bad_config_clamps_rather_than_misfiring(monkeypatch, value, expected):
    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", value)
    assert holdout_pct() == expected


# ---------------------------------------------------------------------------
# Holdout in the loop
# ---------------------------------------------------------------------------

def _failed_event(invoice_id="in_1", event_id="evt_1"):
    return {
        "id": event_id,
        "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": "cus_1",
            "subscription": "sub_1", "customer_email": "me@mine.test",
            "amount_due": 4900, "currency": "eur", "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
        }},
    }


def _paid_event(invoice_id="in_1", event_id="evt_paid", amount=4900):
    return {
        "id": event_id, "type": "invoice.paid",
        "data": {"object": {"object": "invoice", "id": invoice_id,
                            "customer": "cus_1", "amount_paid": amount, "currency": "eur"}},
    }


def test_a_held_out_invoice_is_recorded_but_never_messaged(no_key, isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", "100")
    result = loop.handle_payment_failed(_failed_event(), isolated_store)

    assert result["delivery"]["status"] == "holdout"
    assert result["recovery"] is None, "no draft is produced for the control arm"
    row = isolated_store.get_failure("in_1")
    assert row["holdout"] == 1
    assert isolated_store.messages_for("in_1") == []


def test_a_held_out_invoice_can_still_recover(no_key, isolated_store, monkeypatch):
    """The whole point of the control arm: Stripe's own retry pays it."""
    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", "100")
    loop.handle_payment_failed(_failed_event(), isolated_store)
    loop.handle_recovery(_paid_event(), isolated_store)
    assert isolated_store.get_failure("in_1")["state"] == STATE_RECOVERED


def test_arm_assignment_is_sticky_across_a_config_change(no_key, isolated_store, monkeypatch):
    """An invoice dunned on one attempt and withheld on the next belongs to
    neither arm and would corrupt both rates."""
    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", "100")
    loop.handle_payment_failed(_failed_event(), isolated_store)
    assert isolated_store.get_failure("in_1")["holdout"] == 1

    monkeypatch.setenv("PAYPILOT_HOLDOUT_PCT", "0")
    result = loop.handle_payment_failed(_failed_event(event_id="evt_2"), isolated_store)
    assert result["delivery"]["status"] == "holdout", "must keep its original arm"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _seed(store, invoice_id, *, holdout=False, sent=False, recovered=False,
          currency="eur", amount=4900, recovered_amount=None):
    store.record_failure(
        invoice_id=invoice_id, customer_id="cust_1", amount_minor=amount,
        currency=currency, failure_code="card_expired", holdout=holdout,
    )
    if sent:
        store.record_message(invoice_id=invoice_id, status="sent",
                             provider_message_id=f"rs_{invoice_id}")
        store.transition(invoice_id, STATE_MESSAGED, reason="test")
    if recovered:
        store.transition(invoice_id, STATE_RECOVERED, reason="test",
                         recovered_amount_minor=recovered_amount
                         if recovered_amount is not None else amount)


def test_empty_ledger_reports_no_data_not_zero(isolated_store):
    """"No data" and "nothing recovered" are different statements."""
    rep = report.build_report(isolated_store)
    assert rep["totals"]["failed"] == 0
    assert rep["arms"]["treated"]["recovery_rate"] is None
    assert rep["attribution"]["baseline_kind"] == "none"


def test_untouched_invoices_are_not_counted_as_treated(isolated_store):
    """The easiest way to fake this number: credit recoveries on invoices we
    never contacted."""
    _seed(isolated_store, "in_sent", sent=True, recovered=True)
    _seed(isolated_store, "in_never_sent", recovered=True)

    rep = report.build_report(isolated_store)
    assert rep["arms"]["treated"]["count"] == 1
    assert rep["arms"]["untouched"]["count"] == 1
    assert rep["arms"]["treated"]["recovery_rate"] == 1.0


def test_dry_run_invoices_land_in_untouched(isolated_store):
    isolated_store.record_failure(
        invoice_id="in_dry", customer_id="c", amount_minor=1000,
        currency="eur", failure_code="card_expired",
    )
    isolated_store.record_message(invoice_id="in_dry", status="dry_run")
    rep = report.build_report(isolated_store)
    assert rep["arms"]["untouched"]["count"] == 1
    assert rep["arms"]["treated"]["count"] == 0


def test_money_never_crosses_currencies(isolated_store):
    _seed(isolated_store, "in_eur", sent=True, recovered=True, currency="eur", amount=5000)
    _seed(isolated_store, "in_usd", sent=True, recovered=True, currency="usd", amount=9900)

    rep = report.build_report(isolated_store)
    assert rep["by_currency"]["eur"]["recovered_value"] == 50.0
    assert rep["by_currency"]["usd"]["recovered_value"] == 99.0
    assert set(rep["by_currency"]) == {"eur", "usd"}


def test_partial_recovery_is_not_rounded_up_to_the_invoice(isolated_store):
    _seed(isolated_store, "in_partial", sent=True, recovered=True,
          amount=10000, recovered_amount=2500)
    rep = report.build_report(isolated_store)
    assert rep["by_currency"]["eur"]["recovered_value"] == 25.0
    assert rep["by_currency"]["eur"]["failed_value"] == 100.0


def test_lift_is_withheld_when_an_arm_is_too_small(isolated_store):
    _seed(isolated_store, "in_t", sent=True, recovered=True)
    _seed(isolated_store, "in_c", holdout=True)

    attr = report.build_report(isolated_store)["attribution"]
    assert attr["lift_pp"] is None
    assert "fewer than" in attr["note"]
    # The underlying rates are still reported; only the comparison is withheld.
    assert attr["treated_rate"] == 1.0
    assert attr["baseline_rate"] == 0.0


def test_lift_is_computed_once_both_arms_are_large_enough(isolated_store):
    for i in range(40):
        _seed(isolated_store, f"in_t{i}", sent=True, recovered=(i < 20))
    for i in range(40):
        _seed(isolated_store, f"in_c{i}", holdout=True, recovered=(i < 10))

    attr = report.build_report(isolated_store)["attribution"]
    assert attr["baseline_kind"] == "randomised_holdout"
    assert attr["treated_rate"] == 0.5
    assert attr["baseline_rate"] == 0.25
    assert attr["lift_pp"] == 25.0


def test_observational_baseline_is_labelled_as_not_causal(isolated_store):
    for i in range(40):
        _seed(isolated_store, f"in_t{i}", sent=True, recovered=(i < 20))
    for i in range(40):
        _seed(isolated_store, f"in_u{i}", recovered=(i < 10))

    attr = report.build_report(isolated_store)["attribution"]
    assert attr["baseline_kind"] == "observational_untouched"
    assert "not randomised" in attr["note"]


def test_holdout_baseline_is_preferred_over_the_untouched_one(isolated_store):
    for i in range(40):
        _seed(isolated_store, f"in_t{i}", sent=True, recovered=True)
    for i in range(40):
        _seed(isolated_store, f"in_c{i}", holdout=True)
    for i in range(40):
        _seed(isolated_store, f"in_u{i}")

    attr = report.build_report(isolated_store)["attribution"]
    assert attr["baseline_kind"] == "randomised_holdout"


def test_messaged_count_is_cumulative_not_current_state(isolated_store):
    """A live-state count reports FEWER messages the better the tool performs,
    because recovered invoices stop sitting in "messaged"."""
    for i in range(5):
        _seed(isolated_store, f"in_{i}", sent=True, recovered=(i < 3))

    totals = report.build_report(isolated_store)["totals"]
    assert totals["messaged"] == 5, "all five were messaged, three then recovered"
    assert totals["recovered"] == 3


def test_clicked_count_is_cumulative_too(isolated_store):
    from app.store import STATE_CLICKED

    _seed(isolated_store, "in_1", sent=True)
    isolated_store.transition("in_1", STATE_CLICKED, reason="portal opened")
    isolated_store.transition("in_1", STATE_RECOVERED, recovered_amount_minor=4900)

    totals = report.build_report(isolated_store)["totals"]
    assert totals["clicked"] == 1
    assert totals["recovered"] == 1


def test_time_to_recovery_is_reported(isolated_store):
    _seed(isolated_store, "in_1", sent=True, recovered=True)
    arm = report.build_report(isolated_store)["arms"]["treated"]
    assert arm["median_time_to_recovery_hours"] is not None


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def test_report_endpoints_require_admin_when_a_token_is_set(no_key, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret-token")
    client = TestClient(api_module.app)

    assert client.get("/recovery-report").status_code == 401
    assert client.get("/report").status_code == 401
    ok = client.get("/recovery-report", headers={"authorization": "Bearer secret-token"})
    assert ok.status_code == 200
    assert "attribution" in ok.json()


def test_report_page_renders_without_external_assets(no_key, isolated_store):
    """A strict same-origin CSP ships with the app; a dashboard that silently
    fails to style itself is worse than a plain one."""
    _seed(isolated_store, "in_1", sent=True, recovered=True)
    client = TestClient(api_module.app)
    page = client.get("/report")

    assert page.status_code == 200
    body = page.text
    assert "recovery report" in body.lower()
    assert "src=\"http" not in body and "href=\"http" not in body


def test_report_reflects_a_full_loop_over_http(no_key, isolated_store, monkeypatch):
    from collections import OrderedDict

    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS", "1")
    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())
    client = TestClient(api_module.app)

    client.post("/webhooks/stripe", json=_failed_event())
    before = client.get("/recovery-report").json()
    assert before["totals"]["failed"] == 1
    assert before["totals"]["recovered"] == 0

    client.post("/webhooks/stripe", json=_paid_event())
    after = client.get("/recovery-report").json()
    assert after["totals"]["recovered"] == 1
    assert after["by_currency"]["eur"]["recovered_value"] == 49.0


# ---------------------------------------------------------------------------
# Public sample dashboard
# ---------------------------------------------------------------------------

def test_sample_dashboard_is_public_and_labelled(no_key):
    """The closed loop is the differentiator and the real dashboard is gated,
    so the sample must be visible - and must say it is sample data."""
    client = TestClient(api_module.app)
    page = client.get("/report/sample")

    assert page.status_code == 200
    assert "Sample data" in page.text
    assert "not measured results" in page.text


def test_sample_dashboard_never_reads_the_real_ledger(no_key, isolated_store):
    """It builds its own in-memory cohort, so a client's invoices can never
    appear on a public page."""
    isolated_store.record_failure(
        invoice_id="in_real_secret", customer_id="cus_real", amount_minor=999999,
        currency="eur", failure_code="card_expired",
    )
    page = TestClient(api_module.app).get("/report/sample")
    assert "in_real_secret" not in page.text
    assert "9,999.99" not in page.text


def test_sample_data_does_not_flatter_the_product(no_key):
    """A sample that showed only wins would be a sales lie. It carries a
    holdout that recovered on its own, a partial payment, and churn."""
    from app.report import sample_report

    rep = sample_report()
    assert rep["arms"]["holdout"]["recovered"] > 0, "control arm recovers unaided"
    assert rep["totals"]["churned"] > 0, "not every invoice is a win"
    assert rep["attribution"]["lift_pp"] is None, "arms too small to claim lift"


def test_the_real_dashboard_stays_gated(no_key, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    client = TestClient(api_module.app)
    assert client.get("/report").status_code == 401
    assert client.get("/report/sample").status_code == 200


def test_sample_median_times_are_realistic_not_zero():
    """Seeding a failure and recovering it in the same instant made every arm
    report "0.0h", which reads as broken data on a page whose whole job is to
    look trustworthy."""
    from app.report import sample_report

    arms = sample_report()["arms"]
    for name, arm in arms.items():
        if arm["recovered"]:
            hours = arm["median_time_to_recovery_hours"]
            assert hours and hours > 1, f"{name} median is {hours}h"


def test_an_arm_with_no_recoveries_reports_no_time_not_zero():
    """"n/a" and "0.0h" say different things; rendering the first as the second
    is a lie a dashboard tells easily."""
    from app.report import build_report
    from app.store import Store

    store = Store(":memory:")
    try:
        store.record_failure(invoice_id="in_x", customer_id="c", amount_minor=100,
                             currency="eur", failure_code="card_expired")
        arm = build_report(store)["arms"]["untouched"]
        assert arm["recovered"] == 0
        assert arm["median_time_to_recovery_hours"] is None
    finally:
        store.close()


def test_backdating_never_happens_on_the_live_path(isolated_store):
    """time-to-recovery is a reported number, so it must not be settable by an
    event payload - only by an explicit backfill call."""
    import inspect

    from app import loop

    assert "backdate" not in inspect.getsource(loop)
