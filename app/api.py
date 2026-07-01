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
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.graph import run_recovery
from app.nodes import use_mock

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
    "connect-src 'self'; "
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


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Attach the security headers to every response.

    The API docs get a CDN-friendly CSP so Swagger UI / ReDoc actually render;
    every other path gets the strict same-origin policy.
    """
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        if key == "Content-Security-Policy" and request.url.path in _DOCS_PATHS:
            response.headers.setdefault(key, _DOCS_CSP)
        else:
            response.headers.setdefault(key, value)
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
    client_ip = _client_ip(request)
    if _rate_limited(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down and retry shortly."},
        )
    return run_recovery(event.model_dump())
