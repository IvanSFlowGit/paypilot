# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Latency distribution and inference cost, measured rather than assumed.

Two gaps this closes, both of which were invisible because the thing that was
reported looked like a measurement:

* ``/metrics`` reported ``avg_latency_ms`` and nothing else. An average is the
  one statistic that hides a tail, so a p99 of four seconds and a p99 of forty
  read identically there.
* Inference cost was not measured at all. No token count, no cost per decision,
  no ceiling. ``_llm_text`` in app/nodes.py is the single funnel every model
  call passes through and it discarded the response's ``usage_metadata`` before
  anybody could see it.

THREE DESIGN RULES, each of which is the opposite of the obvious shortcut.

1. A PERCENTILE OVER A HANDFUL OF SAMPLES IS THE MAXIMUM WEARING A PERCENTILE'S
   NAME. Below :data:`MIN_LATENCY_SAMPLE` the percentiles are withheld and the
   sample size is reported, because a denominator of five exaggerates wildly
   while reading as precision. ``max_ms`` is always honest and always given.

2. THE LATENCY WINDOW IS RECENT AND THE AVERAGE IS ALL TIME, SO BOTH SAY WHICH.
   They are two populations. A snapshot that printed them side by side with no
   labels would be the frame error: a number carries its frame, not only its
   date.

3. AN UNPRICED MODEL COSTS ``None``, NEVER ZERO. A zero would make the budget
   verdict read "within" forever, which is the silent-success failure arriving
   inside the one control meant to stop spending. The price table is EMPTY by
   default and operator-supplied, every entry must carry a source and a read
   date, and the loader refuses an entry without them, because a price with no
   provenance is a memory. On 2026-10-16 the first-party pricing page was
   fetched (HTTP 200, 597,532 bytes, control fired) and the served text carried
   transcription and tool rows but NOT the text-model row for the configured
   default, so there is no first-hand price for it to ship with. See
   docs/claims.md.

The verdict is three-valued for the same reason every gate here is: collapsing
``unknown`` into ``within`` spends money while reporting safety, and collapsing
it into ``exceeded`` refuses correct work and gets the control switched off.
"""

from __future__ import annotations

import json
import os
import threading

#: Below this many recorded requests, percentiles are withheld and the count is
#: reported instead. A p99 over nine samples is the maximum.
MIN_LATENCY_SAMPLE = 20

#: Ring size. Bounded by construction so memory cannot grow with uptime, which
#: is the whole reason this is a window and not a list.
LATENCY_WINDOW = 2048

#: JSON object, model id -> {input_per_1m, output_per_1m, source, read_date}.
PRICES_ENV = "PAYPILOT_MODEL_PRICES"

#: USD ceiling for inference. Unset means no ceiling is configured, which is
#: reported as such rather than as an unlimited budget.
BUDGET_ENV = "PAYPILOT_LLM_BUDGET_USD"

WITHIN = "within"
EXCEEDED = "exceeded"
UNKNOWN = "unknown"
NO_BUDGET = "no_budget_configured"

_REQUIRED_PRICE_FIELDS = ("input_per_1m", "output_per_1m", "source", "read_date")


class PriceTableError(ValueError):
    """A price entry is unusable. Raised rather than dropped, because a silently
    dropped price becomes an unpriced call, which reads as a missing model."""


def load_prices(raw: str | None = None) -> dict:
    """Parse the operator-supplied price table, or return an empty one.

    Refuses an entry missing a source or a read date. The point is not
    bureaucracy: an unsourced price is indistinguishable from one somebody
    remembered, and it is multiplied by every token this system ever spends.
    """
    if raw is None:
        raw = os.getenv(PRICES_ENV, "")
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise PriceTableError(f"{PRICES_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PriceTableError(f"{PRICES_ENV} must be a JSON object keyed by model id")

    table = {}
    for model, entry in parsed.items():
        if not isinstance(entry, dict):
            raise PriceTableError(f"price for {model!r} must be an object")
        missing = [f for f in _REQUIRED_PRICE_FIELDS if not entry.get(f)]
        if missing:
            raise PriceTableError(
                f"price for {model!r} is missing {', '.join(missing)}: "
                "a price with no source and no read date is a memory"
            )
        for field in ("input_per_1m", "output_per_1m"):
            try:
                value = float(entry[field])
            except (TypeError, ValueError) as exc:
                raise PriceTableError(f"{model!r} {field} is not a number") from exc
            if value < 0:
                raise PriceTableError(f"{model!r} {field} is negative")
            entry[field] = value
        table[model] = {
            "input_per_1m": entry["input_per_1m"],
            "output_per_1m": entry["output_per_1m"],
            "source": str(entry["source"]),
            "read_date": str(entry["read_date"]),
        }
    return table


def load_budget(raw: str | None = None) -> float | None:
    """USD ceiling, or ``None`` when none is configured.

    ``None`` is not infinity. It is reported as :data:`NO_BUDGET` so nobody
    reads an unconfigured ceiling as a cleared one.
    """
    if raw is None:
        raw = os.getenv(BUDGET_ENV, "")
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise PriceTableError(f"{BUDGET_ENV} is not a number: {raw!r}") from exc
    if value < 0:
        raise PriceTableError(f"{BUDGET_ENV} is negative: {value}")
    return value


class LatencyWindow:
    """The last :data:`LATENCY_WINDOW` durations, as a bounded ring."""

    def __init__(self, capacity: int = LATENCY_WINDOW) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._buf: list[float] = []
        self._next = 0
        self._lock = threading.Lock()

    def record(self, ms: float) -> None:
        value = float(ms)
        with self._lock:
            if len(self._buf) < self._capacity:
                self._buf.append(value)
            else:
                self._buf[self._next] = value
                self._next = (self._next + 1) % self._capacity

    @staticmethod
    def _nearest_rank(ordered: list[float], pct: float) -> float:
        # Nearest-rank, so every reported value is one that was actually
        # observed. An interpolated percentile invents a duration no request
        # ever took, which is a fabricated number in a metrics endpoint.
        if not ordered:
            raise ValueError("no samples")
        import math

        rank = max(1, math.ceil(pct / 100 * len(ordered)))
        return ordered[min(rank, len(ordered)) - 1]

    def snapshot(self) -> dict:
        with self._lock:
            samples = list(self._buf)
        ordered = sorted(samples)
        n = len(ordered)
        enough = n >= MIN_LATENCY_SAMPLE
        return {
            # Named so nobody reads a recent window as an all-time figure.
            "window_size": self._capacity,
            "sample": n,
            "percentiles_withheld": not enough,
            "min_sample": MIN_LATENCY_SAMPLE,
            "p50_ms": round(self._nearest_rank(ordered, 50), 2) if enough else None,
            "p95_ms": round(self._nearest_rank(ordered, 95), 2) if enough else None,
            "p99_ms": round(self._nearest_rank(ordered, 99), 2) if enough else None,
            # Always honest: the worst observed request in the window, whatever
            # the sample size, and the number an average is built to hide.
            "max_ms": round(max(ordered), 2) if ordered else None,
        }


class CostMeter:
    """Inference cost, counted at the one call site, with a ceiling that refuses.

    Totals are kept in process AND are recomputable from the audit log, which is
    what makes them survive a restart: :meth:`load_from_audit` sums the durable
    ``llm_call`` rows rather than reading a snapshot nobody can re-derive.
    """

    def __init__(self, prices: dict | None = None, budget_usd: float | None = None) -> None:
        self._prices = prices if prices is not None else load_prices()
        self._budget = budget_usd if budget_usd is not None else load_budget()
        self._lock = threading.Lock()
        self._calls = 0
        self._unpriced_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_usd = 0.0
        self._refusals = 0

    def cost_of(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        """USD for one call, or ``None`` when the model has no sourced price."""
        price = self._prices.get(model)
        if price is None:
            return None
        return (
            int(input_tokens) * price["input_per_1m"]
            + int(output_tokens) * price["output_per_1m"]
        ) / 1_000_000

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        cost = self.cost_of(model, input_tokens, output_tokens)
        with self._lock:
            self._calls += 1
            self._input_tokens += int(input_tokens)
            self._output_tokens += int(output_tokens)
            if cost is None:
                # Counted separately rather than as zero. An unpriced call that
                # added nothing to the total would keep the verdict at "within"
                # while real money was being spent.
                self._unpriced_calls += 1
            else:
                self._cost_usd += cost
        return cost

    def record_refusal(self) -> None:
        with self._lock:
            self._refusals += 1

    def verdict(self) -> tuple[str, str]:
        """``(state, reason)`` where state is within/exceeded/unknown/no_budget."""
        with self._lock:
            budget, cost = self._budget, self._cost_usd
            unpriced, calls = self._unpriced_calls, self._calls
        if budget is None:
            return NO_BUDGET, f"{BUDGET_ENV} is unset, so no ceiling is in force"
        if cost > budget:
            return EXCEEDED, f"spent {cost:.4f} USD against a {budget:.4f} ceiling"
        if unpriced:
            # Within the measured spend, and the measured spend is incomplete.
            # Saying "within" here would certify a number known to be partial.
            return (
                UNKNOWN,
                f"{unpriced} of {calls} calls have no sourced price, so the "
                f"{cost:.4f} USD measured so far is a floor",
            )
        return WITHIN, f"spent {cost:.4f} USD of a {budget:.4f} ceiling"

    def allows_inference(self) -> bool:
        """False only on a MEASURED breach.

        ``unknown`` permits and says so, because refusing on an unpriced model
        would make an unconfigured price table silently disable the product.
        """
        return self.verdict()[0] != EXCEEDED

    def snapshot(self) -> dict:
        state, reason = self.verdict()
        with self._lock:
            calls = self._calls
            cost = round(self._cost_usd, 6)
            return {
                "calls": calls,
                "unpriced_calls": self._unpriced_calls,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "cost_usd": cost,
                "cost_is_floor": self._unpriced_calls > 0,
                "cost_per_call_usd": round(cost / calls, 6) if calls else None,
                "budget_usd": self._budget,
                "budget_state": state,
                "budget_reason": reason,
                "refusals": self._refusals,
                "priced_models": sorted(self._prices),
            }

    def load_from_audit(self, log) -> int:
        """Re-derive the totals from the durable ``llm_call`` rows. Returns the
        number of rows folded in.

        This is why the figure survives a restart without a snapshot table: the
        events are already append-only, so the total is a QUERY rather than a
        counter somebody has to remember to persist, and it cannot drift from
        the thing it describes.
        """
        rows = log.query(event="llm_call", limit=100_000)
        with self._lock:
            self._calls = self._unpriced_calls = 0
            self._input_tokens = self._output_tokens = 0
            self._cost_usd = 0.0
        folded = 0
        for row in rows:
            fields = row.get("fields") or {}
            if not isinstance(fields, dict):
                continue
            # A row written before tokens were recorded carries neither field.
            # It is skipped rather than counted as a zero-token call, so the
            # total stays a floor and says so instead of quietly understating.
            if "input_tokens" not in fields and "output_tokens" not in fields:
                continue
            self.record(
                str(fields.get("model") or ""),
                int(fields.get("input_tokens") or 0),
                int(fields.get("output_tokens") or 0),
            )
            folded += 1
        return folded


def usage_from_response(response: object) -> tuple[int, int]:
    """``(input_tokens, output_tokens)`` from a model response, or ``(0, 0)``.

    Reads LangChain's ``usage_metadata`` first and the provider's raw
    ``token_usage`` second, because the second survives wrappers that drop the
    first. Returns zeros rather than raising: a metrics read must never be able
    to fail the request it is measuring.
    """
    meta = getattr(response, "usage_metadata", None)
    if isinstance(meta, dict):
        return int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)
    raw = getattr(response, "response_metadata", None)
    if isinstance(raw, dict):
        usage = raw.get("token_usage") or raw.get("usage") or {}
        if isinstance(usage, dict):
            return (
                int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            )
    return 0, 0


# ---------------------------------------------------------------------------
# Process singletons
# ---------------------------------------------------------------------------
#
# One meter and one window per process, created lazily so importing this module
# reads no environment and a test can replace them before anything records.

_cost_meter: CostMeter | None = None
_latency: LatencyWindow | None = None
_singleton_lock = threading.Lock()


def get_cost_meter() -> CostMeter:
    global _cost_meter
    with _singleton_lock:
        if _cost_meter is None:
            _cost_meter = CostMeter()
        return _cost_meter


def get_latency_window() -> LatencyWindow:
    global _latency
    with _singleton_lock:
        if _latency is None:
            _latency = LatencyWindow()
        return _latency


def reset_meters(cost: CostMeter | None = None, latency: LatencyWindow | None = None) -> None:
    """Replace the singletons. For tests and for a deliberate reload at boot."""
    global _cost_meter, _latency
    with _singleton_lock:
        _cost_meter = cost
        _latency = latency


def restore_cost_from_audit() -> dict:
    """Re-derive the cost total from the durable audit rows at boot.

    Returns a small report rather than nothing, because a restore that folded
    in zero rows and a restore that never ran produce the same totals, and only
    one of those is a working restore.
    """
    from app.audit import get_audit_log

    log = get_audit_log()
    if log is None:
        return {"restored": False, "reason": "no audit log configured", "rows": 0}
    try:
        rows = get_cost_meter().load_from_audit(log)
    except Exception as exc:  # noqa: BLE001 - a metrics restore never blocks boot
        return {"restored": False, "reason": f"{type(exc).__name__}: {exc}", "rows": 0}
    return {"restored": True, "reason": "", "rows": rows}
