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

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.graph import run_recovery

app = FastAPI(
    title="PayPilot",
    summary="AI dunning agent that recovers failed payments via RAG + LangGraph.",
    version="0.1.0",
)


class PaymentFailedEvent(BaseModel):
    """Inbound failed-payment event (e.g. a Stripe ``invoice.payment_failed``).

    Field names match the keys the graph nodes read off ``state['event']``, so
    ``model_dump()`` produces exactly the dict :func:`run_recovery` expects.
    """

    customer_id: str = Field(..., description="ID matching a record in data/customers.json")
    amount: float = Field(..., description="Amount that failed to charge")
    currency: str = Field("usd", description="ISO currency code")
    failure_code: str = Field(
        ...,
        description="Why the charge failed: card_expired | insufficient_funds | generic_decline",
    )
    attempt: int = Field(1, ge=1, description="Which dunning attempt this is (1-based)")


@app.get("/health")
def health() -> dict:
    """Liveness probe used by CI / orchestrators."""
    return {"status": "ok"}


@app.post("/payment-failed")
def payment_failed(event: PaymentFailedEvent) -> dict:
    """Run a failed-payment event through the recovery graph.

    Returns the graph's ``output`` payload: ``{diagnosis, strategy, message}``,
    where ``strategy`` is ``{action, retry_in_days, offer}``.
    """
    return run_recovery(event.model_dump())
