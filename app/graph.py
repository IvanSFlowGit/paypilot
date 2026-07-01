"""LangGraph wiring for the PayPilot failed-payment recovery flow.

This module owns three things:

* :class:`RecoveryState` - the shared, typed state passed between nodes. Every
  node in :mod:`app.nodes` reads from and returns a partial update to this
  ``TypedDict``, so its keys are the contract the whole graph agrees on.
* :func:`build_graph` - assembles the seven recovery nodes into a linear
  ``StateGraph`` and compiles it.
* :func:`run_recovery` - the single entry point the API (and tests) call to run
  one failed-payment event through the graph and get the final ``output``.

The flow is intentionally linear::

    retrieve_context -> assess_risk -> diagnose_reason -> choose_strategy
      -> schedule_retry -> draft_message -> finalize -> END

A module-level compiled ``graph`` is built once at import time so callers reuse
the same instance.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.nodes import (
    assess_risk,
    choose_strategy,
    diagnose_reason,
    draft_message,
    finalize,
    retrieve_context,
    schedule_retry,
)


class RecoveryState(TypedDict, total=False):
    """Shared state threaded through the recovery graph.

    Only ``event`` is supplied by the caller; every other key is filled in by a
    node as the flow progresses. ``total=False`` lets nodes return partial
    updates (the contract the nodes in :mod:`app.nodes` rely on).
    """

    event: dict      # input: {customer_id, amount, currency, failure_code, attempt}
    customer: dict   # filled by retrieve_context (a record from data/customers.json)
    context: str     # filled by retrieve_context (RAG playbook snippets)
    risk: dict       # filled by assess_risk: {attempt, prior_failures, churn_risk, escalate}
    diagnosis: str   # filled by diagnose_reason
    strategy: dict   # filled by choose_strategy: {action, retry_in_days, offer, escalated}
    schedule: dict   # filled by schedule_retry: {retry_in_days, next_retry_at, retry_on, timezone}
    message: str     # filled by draft_message (dunning email body)
    output: dict     # filled by finalize: {diagnosis, risk, strategy, schedule, message, impact}


def build_graph():
    """Build and compile the linear seven-node recovery graph.

    Nodes run in a fixed sequence, each enriching :class:`RecoveryState`, until
    ``finalize`` assembles the response payload into ``state['output']``.
    """
    builder = StateGraph(RecoveryState)

    # Register the seven recovery nodes. Node names double as the labels used in
    # the README's mermaid diagram, so keep them in sync.
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("assess_risk", assess_risk)
    builder.add_node("diagnose_reason", diagnose_reason)
    builder.add_node("choose_strategy", choose_strategy)
    builder.add_node("schedule_retry", schedule_retry)
    builder.add_node("draft_message", draft_message)
    builder.add_node("finalize", finalize)

    # Linear edges: enter at retrieval, walk through to finalize, then stop.
    builder.set_entry_point("retrieve_context")
    builder.add_edge("retrieve_context", "assess_risk")
    builder.add_edge("assess_risk", "diagnose_reason")
    builder.add_edge("diagnose_reason", "choose_strategy")
    builder.add_edge("choose_strategy", "schedule_retry")
    builder.add_edge("schedule_retry", "draft_message")
    builder.add_edge("draft_message", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile()


# Compiled once at import time; the API and tests reuse this instance.
graph = build_graph()


def run_recovery(event: dict) -> dict:
    """Run one failed-payment event through the graph and return its output.

    Parameters
    ----------
    event:
        A failed-payment event dict, e.g.
        ``{"customer_id", "amount", "currency", "failure_code", "attempt"}``.

    Returns
    -------
    dict
        The ``output`` payload produced by ``finalize``:
        ``{"diagnosis", "risk", "strategy", "schedule", "message", "impact"}``.
    """
    final_state = graph.invoke({"event": event})
    return final_state["output"]


def run_recovery_batch(events: list[dict]) -> dict:
    """Run many failed-payment events and roll them up into a portfolio view.

    Dunning happens in bulk - a billing run fails dozens of invoices at once -
    so this processes a list of events and returns both the per-event outputs and
    an ``aggregate`` that answers the question a finance lead actually asks: how
    much revenue is recoverable across all of them?

    Returns ``{"results": [...], "aggregate": {...}}``. The aggregate is empty-safe.
    """
    results = [run_recovery(event) for event in events]

    total_at_risk = round(sum(r["impact"]["amount_at_risk"] for r in results), 2)
    total_recovered = round(sum(r["impact"]["expected_recovered"] for r in results), 2)
    total_annual = round(sum(r["impact"]["annual_value_at_risk"] for r in results), 2)
    high_risk = sum(1 for r in results if r["risk"].get("churn_risk") == "high")

    currencies = {r["impact"]["currency"] for r in results}
    if not currencies:
        currency = "USD"
    elif len(currencies) == 1:
        currency = next(iter(currencies))
    else:
        currency = "MIXED"

    return {
        "results": results,
        "aggregate": {
            "count": len(results),
            "total_at_risk": total_at_risk,
            "total_expected_recovered": total_recovered,
            "total_annual_value_at_risk": total_annual,
            "currency": currency,
            "high_risk_count": high_risk,
        },
    }
