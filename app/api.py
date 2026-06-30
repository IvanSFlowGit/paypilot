"""FastAPI surface for PayPilot.

A thin HTTP layer over the recovery graph. It does two jobs:

* Validate the inbound failed-payment webhook with a typed pydantic model
  (:class:`PaymentFailedEvent`).
* Hand the validated event to :func:`app.graph.run_recovery`, which runs the
  five-node LangGraph flow and returns the recovery ``output``.

All the intelligence lives in the graph/nodes; this module deliberately stays
boring so the request contract is easy to read and the LLM seam (mocked in
tests) is untouched here.

Run locally with::

    uvicorn app.api:app --reload
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
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
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}

# Lightweight per-IP rate limit on the recovery endpoint. With a real
# OPENAI_API_KEY set and a public URL, an open /payment-failed would let anyone
# burn API credits, so cap each client to N calls per rolling window. In-memory
# and per-process (fine for a single-instance portfolio deploy).
_RATE_MAX = int(os.getenv("RATE_LIMIT_MAX", "30"))
_RATE_WINDOW = float(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
_rate_hits: dict[str, deque[float]] = {}


def _client_ip(request: Request) -> str:
    """Resolve the real client IP behind Fly's proxy.

    ``request.client.host`` is the upstream proxy inside Fly, so rate limiting on
    it would lump every visitor into one bucket. Fly sets the true client IP in
    ``Fly-Client-IP``; fall back to the first ``X-Forwarded-For`` hop, then the
    socket peer for local runs.
    """
    fly_ip = request.headers.get("fly-client-ip")
    if fly_ip:
        return fly_ip.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(client_ip: str) -> bool:
    """Record a hit for ``client_ip`` and report whether it exceeds the window."""
    now = time.monotonic()
    hits = _rate_hits.setdefault(client_ip, deque())
    while hits and now - hits[0] > _RATE_WINDOW:
        hits.popleft()
    if len(hits) >= _RATE_MAX:
        return True
    hits.append(now)
    return False


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Attach the security headers to every response."""
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
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


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """Serve the PayPilot landing page + live demo UI."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/config", include_in_schema=False)
def config() -> dict:
    """Front-end bootstrap: demo-mode flag, model, and the customer list."""
    return {
        "mock": use_mock(),
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "customers": _customers_summary(),
    }


@app.get("/health")
def health() -> dict:
    """Liveness probe used by CI / orchestrators."""
    return {"status": "ok"}


@app.post("/payment-failed")
def payment_failed(event: PaymentFailedEvent, request: Request):
    """Run a failed-payment event through the recovery graph.

    Returns the graph's ``output`` payload: ``{diagnosis, strategy, message}``,
    where ``strategy`` is ``{action, retry_in_days, offer}``. Rate limited per
    client IP to protect the (real-mode) LLM budget.
    """
    client_ip = _client_ip(request)
    if _rate_limited(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down and retry shortly."},
        )
    return run_recovery(event.model_dump())
