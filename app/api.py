"""FastAPI surface for PayPilot.

A thin HTTP layer over the recovery graph. It does two jobs:

* Validate the inbound failed-payment webhook with a typed pydantic model
  (:class:`PaymentFailedEvent`).
* Hand the validated event to :func:`app.graph.run_recovery`, which runs the
  seven-node LangGraph flow and returns the recovery ``output``.

All the intelligence lives in the graph/nodes; this module deliberately stays
boring so the request contract is easy to read and the LLM seam (mocked in
tests) is untouched here.

Run locally with::

    uvicorn app.api:app --reload
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path

_log = logging.getLogger("paypilot.access")

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.graph import run_recovery, run_recovery_batch
from app.nodes import use_mock
from app.stripe_map import stripe_event_to_internal, verify_stripe_signature

app = FastAPI(
    title="PayPilot",
    summary="AI dunning agent that recovers failed payments via RAG + LangGraph.",
    version="0.1.0",
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
# Serve static assets (the OG preview image). The landing page itself is served
# by the explicit "/" route below so it can stay the site root.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
_CUSTOMERS_PATH = Path(__file__).resolve().parent.parent / "data" / "customers.json"

# Response hardening: conservative headers for a public demo. The CSP allows the
# page's inline <style>/<script> and inline-SVG favicon, but locks everything
# else to same-origin, so the surface stays small.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}

# Swagger UI (/docs) and ReDoc load their assets from jsDelivr, which the strict
# same-origin CSP above would block, leaving the reviewer-facing API docs blank.
# These paths get a scoped CSP that additionally trusts that one CDN.
_DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})
_DOCS_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self' https://cdn.jsdelivr.net; "
    "worker-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

# Rate limit on the recovery endpoint. With a real OPENAI_API_KEY set and a
# public URL, an open /payment-failed would let anyone burn API credits, so we
# cap each client to N calls per rolling window. In-memory and per-process (fine
# for a single-instance portfolio deploy). Three defences layer up:
#   * per-IP cap (the normal case),
#   * an LRU ceiling on how many IP buckets we retain, so a flood of unique or
#     spoofed client IPs can't grow memory without bound, and
#   * a global cap across all clients per window, so rotating the client-IP
#     header per request still can't uncap the (real-key) API spend.
# A lock guards the shared state since sync endpoints run in a threadpool.
_RATE_MAX = int(os.getenv("RATE_LIMIT_MAX", "30"))
_RATE_WINDOW = float(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
_RATE_MAX_TRACKED_IPS = int(os.getenv("RATE_LIMIT_MAX_IPS", "10000"))
_RATE_GLOBAL_MAX = int(os.getenv("RATE_LIMIT_GLOBAL_MAX", "600"))
_rate_lock = threading.Lock()
_rate_hits: "OrderedDict[str, deque[float]]" = OrderedDict()
_global_hits: deque[float] = deque()


def _client_ip(request: Request) -> str:
    """Resolve the real client IP behind Fly's proxy.

    ``request.client.host`` is the upstream proxy inside Fly, so rate limiting on
    it would lump every visitor into one bucket. Fly's edge sets (and overwrites
    any client-supplied) ``Fly-Client-IP``, so it's the trusted signal; fall back
    to the first ``X-Forwarded-For`` hop, then the socket peer for local runs.
    The global cap below backstops the fact that off-Fly these headers are
    client-spoofable.
    """
    fly_ip = request.headers.get("fly-client-ip")
    if fly_ip:
        return fly_ip.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(client_ip: str) -> bool:
    """Record a hit for ``client_ip`` and report whether it exceeds a limit.

    Returns True (rejected) if either the global window or this client's window
    is full. Empty buckets are evicted and the bucket map is LRU-capped so memory
    stays bounded regardless of how many distinct IPs appear.
    """
    now = time.monotonic()
    with _rate_lock:
        # Global window first: the backstop against client-IP rotation.
        while _global_hits and now - _global_hits[0] > _RATE_WINDOW:
            _global_hits.popleft()
        if len(_global_hits) >= _RATE_GLOBAL_MAX:
            return True

        hits = _rate_hits.get(client_ip)
        if hits is None:
            hits = deque()
            _rate_hits[client_ip] = hits
        _rate_hits.move_to_end(client_ip)  # mark most-recently-used

        while hits and now - hits[0] > _RATE_WINDOW:
            hits.popleft()

        limited = len(hits) >= _RATE_MAX
        if not limited:
            hits.append(now)
            _global_hits.append(now)

        # Reclaim memory: drop this bucket if it drained, and enforce the LRU cap.
        if not hits:
            _rate_hits.pop(client_ip, None)
        while len(_rate_hits) > _RATE_MAX_TRACKED_IPS:
            _rate_hits.popitem(last=False)

        return limited


# Idempotency: dedupe repeated work. Stripe retries webhooks (on timeout/5xx),
# and clients retry POSTs, so we cache the result of a given key and replay it
# instead of re-running the graph (which, in real mode, re-spends the LLM). Keyed
# by the Stripe event id or a client-supplied Idempotency-Key. LRU-capped, locked.
_IDEMPOTENCY_MAX = int(os.getenv("IDEMPOTENCY_MAX", "5000"))
_idem_lock = threading.Lock()
_idem_store: "OrderedDict[str, dict]" = OrderedDict()


def _idem_get(key: str):
    with _idem_lock:
        if key in _idem_store:
            _idem_store.move_to_end(key)
            return _idem_store[key]
    return None


def _idem_put(key: str, value: dict) -> None:
    with _idem_lock:
        _idem_store[key] = value
        _idem_store.move_to_end(key)
        while len(_idem_store) > _IDEMPOTENCY_MAX:
            _idem_store.popitem(last=False)


# Lightweight in-process metrics. Thread-safe counters exposed at /metrics; no
# external dependency (Prometheus/statsd would be the next step in a real deploy).
_metrics_lock = threading.Lock()
_metrics: dict = {
    "requests_total": 0,
    "by_status": {},
    "rate_limited_total": 0,
    "recoveries_total": 0,
    "expected_recovered_total": 0.0,
    "latency_ms_sum": 0.0,
}


def _record_request(status_code: int, ms: float) -> None:
    with _metrics_lock:
        _metrics["requests_total"] += 1
        code = str(status_code)
        _metrics["by_status"][code] = _metrics["by_status"].get(code, 0) + 1
        _metrics["latency_ms_sum"] += ms
        if status_code == 429:
            _metrics["rate_limited_total"] += 1


def _record_recovery(expected_recovered: float) -> None:
    """Record a completed recovery for the business metrics."""
    with _metrics_lock:
        _metrics["recoveries_total"] += 1
        _metrics["expected_recovered_total"] = round(
            _metrics["expected_recovered_total"] + float(expected_recovered or 0), 2
        )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Security headers, request id, timing, structured access log, and metrics.

    The API docs get a CDN-friendly CSP so Swagger UI / ReDoc actually render;
    every other path gets the strict same-origin policy. Each request gets an
    X-Request-ID, an X-Process-Time, a JSON access-log line, and a metrics tick.
    """
    request_id = uuid.uuid4().hex[:12]
    start = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - start) * 1000

    for key, value in _SECURITY_HEADERS.items():
        if key == "Content-Security-Policy" and request.url.path in _DOCS_PATHS:
            response.headers.setdefault(key, _DOCS_CSP)
        else:
            response.headers.setdefault(key, value)
    response.headers.setdefault("X-Process-Time", f"{elapsed_ms:.1f}ms")
    response.headers.setdefault("X-Request-ID", request_id)
    if response.status_code == 429:
        response.headers.setdefault("Retry-After", str(int(_RATE_WINDOW)))

    _record_request(response.status_code, elapsed_ms)
    _log.info(
        json.dumps({
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round(elapsed_ms, 1),
            "client_ip": _client_ip(request),
        })
    )
    return response


def _customers_summary() -> list[dict]:
    """Compact customer list for the demo UI dropdown.

    Includes the most recent failed-payment code so the form can pre-select a
    realistic failure reason per customer.
    """
    try:
        records = json.loads(_CUSTOMERS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out = []
    for r in records:
        last_failure = next(
            (
                p.get("failure_code")
                for p in reversed(r.get("payment_history", []))
                if p.get("status") == "failed"
            ),
            None,
        )
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "plan": r.get("plan"),
                "mrr": r.get("mrr"),
                "currency": r.get("currency", "usd"),
                "last_failure_code": last_failure,
            }
        )
    return out


class PaymentFailedEvent(BaseModel):
    """Inbound failed-payment event (e.g. a Stripe ``invoice.payment_failed``).

    Field names match the keys the graph nodes read off ``state['event']``, so
    ``model_dump()`` produces exactly the dict :func:`run_recovery` expects.
    """

    customer_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="ID matching a record in data/customers.json",
    )
    amount: float = Field(..., ge=0, le=1_000_000, description="Amount that failed to charge")
    currency: str = Field("usd", pattern=r"^[A-Za-z]{3}$", description="ISO currency code")
    failure_code: str = Field(
        ...,
        min_length=1,
        max_length=40,
        pattern=r"^[a-z_]+$",
        description="Why the charge failed: card_expired | insufficient_funds | generic_decline",
    )
    attempt: int = Field(1, ge=1, le=20, description="Which dunning attempt this is (1-based)")


# Response models. These give the OpenAPI docs a precise, typed schema for the
# recovery payload (what a reviewer sees at /docs) and keep the output contract
# explicit. Field order/keys mirror what ``app.nodes.finalize`` assembles.

class RiskModel(BaseModel):
    attempt: int = Field(..., description="Which dunning attempt this is")
    prior_failures: int = Field(..., description="Recent failed charges before this one")
    churn_risk: str = Field(..., description="low | medium | high")
    escalate: bool = Field(..., description="True when the strategy should be escalated")


class StrategyModel(BaseModel):
    action: str = Field(..., description="Recovery action, e.g. request_card_update")
    retry_in_days: int = Field(..., description="Days until the next retry")
    offer: str = Field(..., description="Human-readable rationale / offer")
    escalated: bool = Field(..., description="True when repeat-failure escalation applied")


class ScheduleModel(BaseModel):
    retry_in_days: int
    next_retry_at: str = Field(..., description="Concrete next-retry instant (ISO 8601, UTC)")
    retry_on: str = Field(..., description="Calendar date of the next retry (YYYY-MM-DD)")
    timezone: str = Field("UTC", description="Timezone of the schedule")


class ImpactModel(BaseModel):
    amount_at_risk: float
    currency: str
    recovery_likelihood: float = Field(..., description="Estimated probability of recovery (0-1)")
    expected_recovered: float
    annual_value_at_risk: float = Field(..., description="MRR annualised, lost if they churn")
    churn_risk: str


class RecoveryResponse(BaseModel):
    """The recovery payload returned by ``POST /payment-failed``."""

    diagnosis: str
    risk: RiskModel
    strategy: StrategyModel
    schedule: ScheduleModel
    message: str = Field(..., description="Drafted dunning email body")
    impact: ImpactModel


class HealthResponse(BaseModel):
    status: str


class BatchRequest(BaseModel):
    """A batch of failed-payment events (e.g. one billing run's failures)."""

    events: list[PaymentFailedEvent] = Field(
        ..., min_length=1, max_length=50, description="1-50 failed-payment events"
    )


class CurrencyBucket(BaseModel):
    count: int
    total_at_risk: float
    total_expected_recovered: float
    total_annual_value_at_risk: float
    high_risk_count: int


class AggregateModel(BaseModel):
    """Portfolio roll-up across a batch: the recoverable-revenue headline.

    Top-level totals are the primary (most-accounts) currency; ``by_currency``
    carries the full per-currency split so mixed billing runs stay correct.
    """

    count: int
    total_at_risk: float
    total_expected_recovered: float
    total_annual_value_at_risk: float
    currency: str
    high_risk_count: int
    by_currency: dict[str, CurrencyBucket] = {}


class BatchResponse(BaseModel):
    results: list[RecoveryResponse]
    aggregate: AggregateModel


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """Serve the PayPilot landing page + live demo UI."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/robots.txt", include_in_schema=False)
def robots() -> FileResponse:
    """Crawler directives (points at the sitemap and welcomes AI engines)."""
    return FileResponse(_STATIC_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> FileResponse:
    """XML sitemap for search engines."""
    return FileResponse(_STATIC_DIR / "sitemap.xml", media_type="application/xml")


@app.get("/llms.txt", include_in_schema=False)
def llms() -> FileResponse:
    """Structured project summary for LLM / answer-engine crawlers (AEO/GEO)."""
    return FileResponse(_STATIC_DIR / "llms.txt", media_type="text/plain")


@app.get("/pricing", include_in_schema=False)
def pricing() -> FileResponse:
    """Performance-based pricing page (paid use via Streamflow Solutions)."""
    return FileResponse(_STATIC_DIR / "pricing.html")


@app.get("/terms", include_in_schema=False)
def terms() -> FileResponse:
    """Terms & Conditions for the demo and paid engagements."""
    return FileResponse(_STATIC_DIR / "terms.html")


@app.get("/config", include_in_schema=False)
def config() -> dict:
    """Front-end bootstrap: demo-mode flag, model, and the customer list."""
    return {
        "mock": use_mock(),
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "customers": _customers_summary(),
    }


@app.get("/health", response_model=HealthResponse)
def health() -> dict:
    """Liveness probe used by CI / orchestrators."""
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> dict:
    """In-process operational + business metrics as a JSON snapshot.

    Request counts by status, throttling, average latency, and the business
    metric that matters here: how many recoveries have run and the total expected
    recovered value. (Prometheus/statsd would be the next step in a real deploy.)
    """
    with _metrics_lock:
        total = _metrics["requests_total"]
        return {
            "requests_total": total,
            "by_status": dict(_metrics["by_status"]),
            "rate_limited_total": _metrics["rate_limited_total"],
            "recoveries_total": _metrics["recoveries_total"],
            "expected_recovered_total": round(_metrics["expected_recovered_total"], 2),
            "avg_latency_ms": round(_metrics["latency_ms_sum"] / total, 2) if total else 0.0,
            "mock_mode": use_mock(),
        }


@app.post(
    "/payment-failed",
    response_model=RecoveryResponse,
    responses={429: {"description": "Rate limit exceeded"}},
)
def payment_failed(event: PaymentFailedEvent, request: Request):
    """Run a failed-payment event through the recovery graph.

    Returns the graph's ``output`` payload:
    ``{diagnosis, risk, strategy, schedule, message, impact}``, where ``risk``
    scores churn from the attempt count + recent failures, ``strategy`` is
    ``{action, retry_in_days, offer, escalated}``, ``schedule`` pins the concrete
    next-retry time, and ``impact`` quantifies the revenue at stake. Rate limited
    per client IP to protect the (real-mode) LLM budget.
    """
    # Idempotency first: a retried request with the same key replays the cached
    # result without re-running the graph and without counting against the limit.
    idem_key = request.headers.get("idempotency-key")
    if idem_key:
        cached = _idem_get(f"pf:{idem_key}")
        if cached is not None:
            return cached

    client_ip = _client_ip(request)
    if _rate_limited(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down and retry shortly."},
        )
    result = run_recovery(event.model_dump())
    _record_recovery(result.get("impact", {}).get("expected_recovered", 0))
    if idem_key:
        _idem_put(f"pf:{idem_key}", result)
    return result


@app.post(
    "/payment-failed/batch",
    response_model=BatchResponse,
    responses={429: {"description": "Rate limit exceeded"}},
)
def payment_failed_batch(batch: BatchRequest, request: Request):
    """Run a batch of failed-payment events (one billing run) in a single call.

    Returns each event's recovery output plus an ``aggregate`` roll-up: total
    revenue at risk, total expected recovered, and how many accounts are high
    churn risk. Rate limited per client IP; the batch is capped at 50 events.
    """
    idem_key = request.headers.get("idempotency-key")
    if idem_key:
        cached = _idem_get(f"bt:{idem_key}")
        if cached is not None:
            return cached

    client_ip = _client_ip(request)
    if _rate_limited(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down and retry shortly."},
        )
    result = run_recovery_batch([event.model_dump() for event in batch.events])
    for r in result["results"]:
        _record_recovery(r.get("impact", {}).get("expected_recovered", 0))
    if idem_key:
        _idem_put(f"bt:{idem_key}", result)
    return result


def _portfolio_events() -> list[dict]:
    """Build one representative failed-payment event per demo customer."""
    return [
        {
            "customer_id": c["id"],
            "amount": float(c.get("mrr") or 0),
            "currency": c.get("currency") or "usd",
            "failure_code": c.get("last_failure_code") or "generic_decline",
            "attempt": 1,
        }
        for c in _customers_summary()
    ]


@app.get("/portfolio-impact", response_model=AggregateModel)
def portfolio_impact() -> dict:
    """Recoverable-revenue roll-up across every demo customer (homepage headline)."""
    events = _portfolio_events()
    if not events:
        return {
            "count": 0,
            "total_at_risk": 0.0,
            "total_expected_recovered": 0.0,
            "total_annual_value_at_risk": 0.0,
            "currency": "USD",
            "high_risk_count": 0,
            "by_currency": {},
        }
    return run_recovery_batch(events)["aggregate"]


@app.post("/webhooks/stripe", responses={400: {"description": "Invalid signature or payload"}})
async def stripe_webhook(request: Request):
    """Accept a Stripe ``invoice.payment_failed`` webhook and run recovery.

    Speaks Stripe directly: it verifies the ``Stripe-Signature`` header when
    ``STRIPE_WEBHOOK_SECRET`` is configured (skipped in the keyless demo),
    acknowledges any non-target event type with a 200 so Stripe doesn't retry,
    and otherwise maps the Stripe event to PayPilot's internal shape and returns
    the recovery output. Rate limited per client IP.

    Point a Stripe webhook (or `stripe trigger invoice.payment_failed`) at this
    route; add ``metadata.paypilot_customer_id`` to resolve a demo customer.
    """
    payload = await request.body()

    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if secret:
        signature = request.headers.get("stripe-signature", "")
        if not verify_stripe_signature(payload, signature, secret):
            return JSONResponse(status_code=400, content={"detail": "Invalid Stripe signature"})

    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON payload"})

    if event.get("type") != "invoice.payment_failed":
        return {"received": True, "handled": False}

    # Stripe retries deliver the same event id; replay the stored result so a
    # retry never double-processes (or double-spends the LLM in real mode).
    event_id = event.get("id")
    if event_id:
        cached = _idem_get(f"stripe:{event_id}")
        if cached is not None:
            return {**cached, "idempotent": True}

    if _rate_limited(_client_ip(request)):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down and retry shortly."},
        )

    recovery = run_recovery(stripe_event_to_internal(event))
    _record_recovery(recovery.get("impact", {}).get("expected_recovered", 0))
    response = {"received": True, "handled": True, "recovery": recovery}
    if event_id:
        _idem_put(f"stripe:{event_id}", response)
    return response
