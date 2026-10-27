"""The latency distribution and the cost ceiling.

These exist because /metrics reported an average and nothing else, and because
inference cost was not measured at all. Both gaps were invisible: an average
looks like a measurement, and an unmeasured cost looks like a cheap one.
"""

from __future__ import annotations

import json

import pytest

from app.meter import (
    EXCEEDED,
    MIN_LATENCY_SAMPLE,
    NO_BUDGET,
    UNKNOWN,
    WITHIN,
    CostMeter,
    LatencyWindow,
    PriceTableError,
    load_budget,
    load_prices,
    usage_from_response,
)

_PRICE = {
    "m1": {
        "input_per_1m": 1.0,
        "output_per_1m": 2.0,
        "source": "operator-supplied fixture",
        "read_date": "2026-10-16",
    }
}


# --- latency ---------------------------------------------------------------


def test_percentiles_are_withheld_below_the_sample_floor():
    w = LatencyWindow()
    for ms in range(MIN_LATENCY_SAMPLE - 1):
        w.record(ms)
    s = w.snapshot()
    # A p99 over nineteen samples is the maximum wearing a percentile's name.
    assert s["percentiles_withheld"] is True
    assert s["p50_ms"] is None and s["p95_ms"] is None and s["p99_ms"] is None
    assert s["sample"] == MIN_LATENCY_SAMPLE - 1
    # The worst observed request is always reported, whatever the sample size.
    assert s["max_ms"] == float(MIN_LATENCY_SAMPLE - 2)


def test_percentiles_appear_at_the_floor_and_order_correctly():
    w = LatencyWindow()
    for ms in range(1, MIN_LATENCY_SAMPLE + 1):
        w.record(ms)
    s = w.snapshot()
    assert s["percentiles_withheld"] is False
    assert s["p50_ms"] <= s["p95_ms"] <= s["p99_ms"] <= s["max_ms"]


def test_every_reported_percentile_is_an_observed_duration():
    # Nearest-rank, never interpolated: an interpolated percentile invents a
    # duration no request ever took, inside a metrics endpoint.
    w = LatencyWindow()
    observed = [3.0, 5.0, 11.0, 400.0]
    for _ in range(10):
        for ms in observed:
            w.record(ms)
    s = w.snapshot()
    for key in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
        assert s[key] in observed, (key, s[key])


def test_the_tail_an_average_hides_is_visible():
    w = LatencyWindow()
    for _ in range(99):
        w.record(10)
    w.record(5000)
    s = w.snapshot()
    avg = (99 * 10 + 5000) / 100
    assert avg < 60  # the average says everything is fine
    assert s["max_ms"] == 5000  # the distribution does not
    assert s["p99_ms"] >= 10


def test_the_window_is_bounded_and_forgets_the_oldest():
    w = LatencyWindow(capacity=4)
    for ms in (1, 2, 3, 4, 5, 6):
        w.record(ms)
    s = w.snapshot()
    assert s["sample"] == 4
    assert s["window_size"] == 4
    # 1 and 2 have rolled out, so the worst in the WINDOW is 6 and the window
    # reports its own size, which is what stops it being read as all time.
    assert s["max_ms"] == 6


def test_window_capacity_must_be_positive():
    with pytest.raises(ValueError):
        LatencyWindow(capacity=0)


# --- the price table -------------------------------------------------------


def test_price_table_is_empty_by_default():
    # Shipping a guessed price would multiply a remembered number by every
    # token the system ever spends.
    assert load_prices("") == {}
    assert load_budget("") is None


def test_a_price_without_a_source_or_a_read_date_is_refused():
    for bad in (
        {"m": {"input_per_1m": 1, "output_per_1m": 2, "read_date": "2026-10-16"}},
        {"m": {"input_per_1m": 1, "output_per_1m": 2, "source": "somewhere"}},
        {"m": {"source": "s", "read_date": "d"}},
    ):
        with pytest.raises(PriceTableError, match="missing"):
            load_prices(json.dumps(bad))


def test_a_malformed_price_table_is_refused_rather_than_ignored():
    with pytest.raises(PriceTableError, match="valid JSON"):
        load_prices("{not json")
    with pytest.raises(PriceTableError, match="JSON object"):
        load_prices("[1, 2]")
    with pytest.raises(PriceTableError, match="negative"):
        load_prices(json.dumps({"m": {**_PRICE["m1"], "input_per_1m": -1}}))
    with pytest.raises(PriceTableError, match="not a number"):
        load_prices(json.dumps({"m": {**_PRICE["m1"], "output_per_1m": "free"}}))
    with pytest.raises(PriceTableError, match="not a number"):
        load_budget("lots")


# --- cost ------------------------------------------------------------------


def test_an_unpriced_model_costs_none_and_never_zero():
    m = CostMeter(prices=_PRICE, budget_usd=10.0)
    assert m.cost_of("m1", 1_000_000, 0) == 1.0
    # The whole point: zero would leave the verdict reading "within" forever.
    assert m.cost_of("nobody-priced-this", 1_000_000, 1_000_000) is None


def test_an_unpriced_call_makes_the_verdict_unknown_and_the_total_a_floor():
    m = CostMeter(prices=_PRICE, budget_usd=10.0)
    m.record("m1", 1_000_000, 0)
    m.record("unpriced", 5_000_000, 5_000_000)
    s = m.snapshot()
    assert s["calls"] == 2
    assert s["unpriced_calls"] == 1
    assert s["cost_usd"] == 1.0
    assert s["cost_is_floor"] is True
    assert s["budget_state"] == UNKNOWN
    assert "floor" in s["budget_reason"]
    # Unknown permits, so an unconfigured price table cannot silently disable
    # the product, and it says so rather than reporting safety.
    assert m.allows_inference() is True


def test_a_measured_breach_refuses_and_a_clean_meter_permits():
    m = CostMeter(prices=_PRICE, budget_usd=1.5)
    m.record("m1", 1_000_000, 0)
    assert m.verdict()[0] == WITHIN
    assert m.allows_inference() is True
    m.record("m1", 1_000_000, 0)  # total 2.00 against a 1.50 ceiling
    state, reason = m.verdict()
    assert state == EXCEEDED
    assert "2.0000" in reason and "1.5000" in reason
    assert m.allows_inference() is False


def test_no_budget_configured_is_not_an_unlimited_budget():
    m = CostMeter(prices=_PRICE, budget_usd=None)
    m.record("m1", 10_000_000, 10_000_000)  # 30 USD and nothing refuses
    state, reason = m.verdict()
    assert state == NO_BUDGET
    assert "unset" in reason
    assert m.allows_inference() is True
    assert m.snapshot()["budget_usd"] is None


def test_cost_per_call_is_none_rather_than_zero_with_no_calls():
    assert CostMeter(prices=_PRICE, budget_usd=1.0).snapshot()["cost_per_call_usd"] is None


# --- durability ------------------------------------------------------------


def test_totals_are_re_derived_from_the_durable_audit_rows(tmp_path):
    from app.audit import AuditEventLog

    log = AuditEventLog(tmp_path / "audit.db")
    for _ in range(3):
        log.record("llm_call", model="m1", input_tokens=1_000_000, output_tokens=0)
    # A row written before tokens were recorded carries neither field. It is
    # skipped rather than folded in as a zero-token call.
    log.record("llm_call", model="m1")
    log.record("something_else", model="m1", input_tokens=9_000_000, output_tokens=0)

    m = CostMeter(prices=_PRICE, budget_usd=10.0)
    folded = m.load_from_audit(log)
    assert folded == 3, "the legacy row and the unrelated event must not be folded in"
    assert m.snapshot()["cost_usd"] == 3.0
    # Idempotent: a reload after a restart must not double the total.
    m.load_from_audit(log)
    assert m.snapshot()["cost_usd"] == 3.0
    log.close()


# --- reading usage off a response -----------------------------------------


def test_usage_is_read_from_either_shape_and_never_raises():
    class _Lc:
        usage_metadata = {"input_tokens": 11, "output_tokens": 7}

    class _Raw:
        response_metadata = {"token_usage": {"prompt_tokens": 3, "completion_tokens": 5}}

    assert usage_from_response(_Lc()) == (11, 7)
    assert usage_from_response(_Raw()) == (3, 5)
    # A bare string is what the template engine returns, and a metrics read must
    # never be able to fail the request it is measuring.
    assert usage_from_response("just text") == (0, 0)
    assert usage_from_response(None) == (0, 0)


# --- the wiring ------------------------------------------------------------
#
# The module above is unit tested. These prove it is CONNECTED: a correct meter
# nothing calls and an absent meter produce the same /metrics output and the
# same bill, and the good one is worse because its existence stops anybody
# writing a replacement.


@pytest.fixture
def fresh_meters():
    """Replace the process singletons, and put them back afterwards."""
    from app import meter as meter_module

    meter_module.reset_meters()
    yield meter_module
    meter_module.reset_meters()


def test_the_one_call_site_records_the_tokens_it_used_to_discard(fresh_meters, monkeypatch):
    from app import nodes

    class _Response:
        content = "drafted text"
        usage_metadata = {"input_tokens": 1_000_000, "output_tokens": 500_000}

    class _Model:
        def invoke(self, prompt):
            return _Response()

    monkeypatch.setattr(nodes, "get_llm", lambda *_a, **_k: _Model())
    monkeypatch.setattr(nodes, "model_name", lambda *_a, **_k: "m1")
    fresh_meters.reset_meters(cost=CostMeter(prices=_PRICE, budget_usd=100.0))

    assert nodes._llm_text("prompt") == "drafted text"
    s = fresh_meters.get_cost_meter().snapshot()
    assert s["calls"] == 1
    assert s["input_tokens"] == 1_000_000
    assert s["output_tokens"] == 500_000
    assert s["cost_usd"] == 2.0  # 1.00 input + 1.00 output
    assert s["unpriced_calls"] == 0


def test_metering_never_fails_the_request_it_measures(fresh_meters, monkeypatch):
    from app import nodes

    class _Model:
        def invoke(self, prompt):
            return "bare string from the template engine"

    class _Exploding(CostMeter):
        def record(self, *a, **k):
            raise RuntimeError("meter is broken")

    monkeypatch.setattr(nodes, "get_llm", lambda *_a, **_k: _Model())
    fresh_meters.reset_meters(cost=_Exploding(prices=_PRICE, budget_usd=1.0))
    # The deterministic path returns a bare string and reports (0, 0), so record
    # is not even reached; the text must come back either way.
    assert nodes._llm_text("prompt").startswith("bare string")


def test_a_measured_breach_falls_back_to_committed_copy_and_is_audited(
    fresh_meters, monkeypatch
):
    from app import nodes

    monkeypatch.setattr(nodes, "llm_drafting_enabled", lambda *_a, **_k: True)

    class _Tripwire:
        def __init__(self, *a, **k):
            raise AssertionError("a breached budget must not construct a model")

    monkeypatch.setattr(nodes, "ChatOpenAI", _Tripwire)

    events = []
    import app.audit as audit_module

    monkeypatch.setattr(
        audit_module, "audit_security_event", lambda **kw: events.append(kw)
    )

    meter = CostMeter(prices=_PRICE, budget_usd=0.5)
    meter.record("m1", 1_000_000, 0)  # 1.00 against a 0.50 ceiling
    fresh_meters.reset_meters(cost=meter)
    assert meter.verdict()[0] == EXCEEDED

    engine = nodes.get_llm()
    # The fallback is this product's own default rather than a lesser
    # substitute, and the refusal is recorded rather than silent.
    assert isinstance(engine, nodes._TemplateEngine)
    assert [e["event"] for e in events] == ["llm_budget_exceeded"]
    assert events[0]["severity"] == "error"
    assert meter.snapshot()["refusals"] == 1


def test_an_unpriced_model_does_not_switch_the_drafting_off(fresh_meters, monkeypatch):
    from app import nodes

    monkeypatch.setattr(nodes, "llm_drafting_enabled", lambda *_a, **_k: True)
    built = []

    class _Model:
        def __init__(self, *a, **k):
            built.append(k)

    monkeypatch.setattr(nodes, "ChatOpenAI", _Model)

    meter = CostMeter(prices={}, budget_usd=0.5)
    meter.record("nobody-priced-this", 9_000_000, 9_000_000)
    fresh_meters.reset_meters(cost=meter)
    assert meter.verdict()[0] == UNKNOWN

    nodes.get_llm()
    # Refusing here would mean an unconfigured price table silently disables
    # the product, which is a worse failure than an unmeasured spend.
    assert len(built) == 1
    assert meter.snapshot()["refusals"] == 0


def test_metrics_reports_the_distribution_beside_the_average(fresh_meters):
    from app import api as api_module

    for ms in range(1, MIN_LATENCY_SAMPLE + 1):
        api_module._record_request(200, ms)
    snap = api_module._latency_snapshot()
    assert snap["percentiles_withheld"] is False
    assert snap["p99_ms"] >= snap["p50_ms"]
    assert snap["window_size"] >= MIN_LATENCY_SAMPLE
    inf = api_module._inference_snapshot()
    for key in ("calls", "cost_usd", "budget_state", "cost_is_floor"):
        assert key in inf, inf


def test_a_metrics_read_that_cannot_run_says_so_rather_than_returning_zeros(monkeypatch):
    from app import api as api_module
    from app import meter as meter_module

    def _boom():
        raise RuntimeError("window gone")

    monkeypatch.setattr(meter_module, "get_latency_window", _boom)
    snap = api_module._latency_snapshot()
    # Zeros would read as a fast system, which is the reassuring direction.
    assert "unavailable" in snap
    assert "window gone" in snap["unavailable"]


def test_the_boot_restore_reports_whether_it_actually_ran(monkeypatch, fresh_meters):
    from app import audit as audit_module

    monkeypatch.setattr(audit_module, "get_audit_log", lambda: None)
    result = fresh_meters.restore_cost_from_audit()
    # A restore that folded zero rows and a restore that never ran leave
    # identical totals, so the two must be distinguishable in the report.
    assert result == {"restored": False, "reason": "no audit log configured", "rows": 0}


def test_the_published_metrics_payload_keeps_its_old_key_and_gains_the_new_blocks():
    # Asserts on the PAYLOAD rather than on api.py's source text. A source grep
    # would pass on a key that exists in a comment or in dead code, which is the
    # check-satisfied-by-its-own-documentation shape.
    from app.api import metrics

    payload = metrics()
    # /metrics is consumed by things this repository does not own, so the old
    # key stays beside the clearer one rather than being replaced.
    assert "avg_latency_ms" in payload
    assert "avg_latency_ms_all_time" in payload
    assert payload["avg_latency_ms"] == payload["avg_latency_ms_all_time"]
    for key in ("sample", "window_size", "percentiles_withheld", "max_ms"):
        assert key in payload["latency_recent"], payload["latency_recent"]
    for key in ("calls", "cost_usd", "cost_is_floor", "budget_state", "unpriced_calls"):
        assert key in payload["inference"], payload["inference"]
