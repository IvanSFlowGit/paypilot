"""LangGraph node functions for the PayPilot recovery flow.

Each node is a pure function ``(state) -> dict`` that returns a *partial* update
to the shared :class:`~app.graph.RecoveryState`. LangGraph merges the returned
dict back into the running state, so a node only returns the keys it produces.

The flow (wired in ``app/graph.py``) is::

    retrieve_context -> diagnose_reason -> choose_strategy -> draft_message -> finalize

Two seams keep this testable with **no network and no API key**:

* :func:`get_llm` is the single place a ``ChatOpenAI`` instance is created, so
  tests can monkeypatch it with a fake chat model.
* The retriever is obtained lazily via :func:`app.ingest.get_retriever`, which
  tests monkeypatch to avoid building a real Chroma store / calling OpenAI.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from langchain_openai import ChatOpenAI

from app.ingest import get_retriever

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

# Resolve data files relative to the repo root (parent of this ``app`` package)
# so the nodes work regardless of the process's current working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
CUSTOMERS_PATH = _REPO_ROOT / "data" / "customers.json"

# Deterministic dunning rules keyed by Stripe-style failure code. Kept here as a
# table (not LLM-decided) so strategy is stable and unit-testable. The values
# mirror the "Retry cadence summary" in data/playbook.md.
_STRATEGY_RULES: dict[str, dict] = {
    "card_expired": {
        "action": "request_card_update",
        "retry_in_days": 1,
        "offer": "Send a one-click update-card link; the saved card has expired and "
        "retrying it will keep failing until it's replaced.",
    },
    "insufficient_funds": {
        "action": "wait_and_retry",
        "retry_in_days": 3,
        "offer": "Space the retry out to land after a likely top-up, and use a soft, "
        "no-pressure tone; offer a short grace period if it keeps recurring.",
    },
    "generic_decline": {
        "action": "retry_and_verify",
        "retry_in_days": 2,
        "offer": "Retry once and invite the customer to check with their bank or try "
        "another card; the decline reason is unspecified.",
    },
}

# Fallback for any unexpected failure code, so the graph never crashes on a
# value outside the three documented codes.
_DEFAULT_STRATEGY: dict = {
    "action": "retry_and_verify",
    "retry_in_days": 2,
    "offer": "Retry once and ask the customer to verify their payment method.",
}


# ---------------------------------------------------------------------------
# Demo / mock mode
# ---------------------------------------------------------------------------
# PayPilot is a public portfolio demo: it must produce a realistic, grounded
# result for any visitor with *no* OpenAI key and zero cost. When OPENAI_API_KEY
# is unset, get_llm() returns a deterministic _MockLLM that writes diagnosis and
# email copy straight from the dunning playbook rules, keyed on the failure code.
# Set OPENAI_API_KEY (and restart) to switch every node to the real ChatOpenAI.


def use_mock() -> bool:
    """True when no OpenAI key is configured, so the demo runs offline + free."""
    return not os.getenv("OPENAI_API_KEY")


class _MockResponse:
    """Mimics a chat-model response object: exposes ``.content``."""

    def __init__(self, content: str) -> None:
        self.content = content


# Grounded, playbook-derived templates. {name}/{plan} are filled per request.
_MOCK_DIAGNOSIS: dict[str, str] = {
    "card_expired": (
        "The card on file for {name} has expired, so the {plan} renewal couldn't "
        "be charged. This is the most recoverable kind of failure: the "
        "subscription is active and only needs a current card, so retrying the old "
        "one will keep failing until it's updated."
    ),
    "insufficient_funds": (
        "The latest {plan} charge for {name} was declined for insufficient funds, "
        "which is almost always a temporary timing issue rather than a churn "
        "signal. The card itself is valid, so spacing the retry to land after a "
        "likely top-up should recover the payment."
    ),
    "generic_decline": (
        "The {plan} payment for {name} hit a generic decline, meaning the issuer "
        "blocked it without a specific reason - often a temporary bank hold. It's "
        "a recoverable middle case: one well-timed retry plus a nudge to check "
        "with their bank usually clears it."
    ),
}

_MOCK_MESSAGE: dict[str, str] = {
    "card_expired": (
        "Hi {name}, we tried to renew your {plan} plan but the card we have on file "
        "has expired, so the latest payment didn't go through. There's nothing to "
        "worry about - your service is still running for now. Whenever you have a "
        "moment, just update your card and we'll handle the rest in one click. "
        "Reply here anytime if you'd like a hand.\n\nWarmly,\nThe PayPilot Team"
    ),
    "insufficient_funds": (
        "Hi {name}, a quick heads-up: your most recent {plan} payment didn't clear, "
        "and it looks like a temporary funding hiccup rather than anything wrong "
        "with your card. Your account stays active, so there's nothing urgent to "
        "do. We'll automatically retry in a few days - and if it'd help, just reply "
        "and we can sort out timing or options together.\n\nThanks for being with "
        "us,\nThe PayPilot Team"
    ),
    "generic_decline": (
        "Hi {name}, we weren't able to process your {plan} renewal - the bank "
        "declined the charge without a specific reason, which usually points to a "
        "temporary hold on their side. Your service is still on, so nothing changes "
        "for now. It often helps to give your bank a quick check or try another "
        "card, and we'll retry shortly either way. Reach out anytime.\n\nBest,\n"
        "The PayPilot Team"
    ),
}

_MOCK_FALLBACK_DIAGNOSIS = (
    "The {plan} payment for {name} failed for an unrecognised reason. Treat it as "
    "recoverable: retry once and invite the customer to verify their payment method."
)
_MOCK_FALLBACK_MESSAGE = (
    "Hi {name}, we ran into a problem renewing your {plan} plan and the latest "
    "payment didn't go through. Your service is still active - when you have a "
    "moment, please check or update your payment method and we'll retry. Reply "
    "here if you need anything.\n\nBest,\nThe PayPilot Team"
)


def _mock_fields(prompt: str) -> tuple[str, str, str]:
    """Pull (failure_code, name, plan) out of a node prompt for the mock LLM."""
    code = next(
        (c for c in ("card_expired", "insufficient_funds", "generic_decline") if c in prompt),
        "",
    )
    # diagnose prompt embeds the customer dict repr; draft prompt embeds "to NAME
    # about a failed payment on their PLAN plan". Try both shapes.
    name = "there"
    plan = "your"
    m = re.search(r"'name':\s*'([^']+)'", prompt)
    if m:
        name = m.group(1)
    else:
        m = re.search(r"\bto (.+?) about a failed payment", prompt)
        if m:
            name = m.group(1).strip()
    m = re.search(r"'plan':\s*'([^']+)'", prompt)
    if m:
        plan = m.group(1)
    else:
        m = re.search(r"on their (.+?) plan", prompt)
        if m:
            plan = m.group(1).strip()
    return code, name, plan


class _MockLLM:
    """Deterministic stand-in for ChatOpenAI used when no API key is set.

    Inspects the prompt to tell the diagnosis node from the drafting node, then
    fills the matching playbook-grounded template with the customer's name/plan.
    """

    def invoke(self, prompt: str):
        code, name, plan = _mock_fields(prompt)
        is_email = "dunning email body" in prompt
        if is_email:
            template = _MOCK_MESSAGE.get(code, _MOCK_FALLBACK_MESSAGE)
        else:
            template = _MOCK_DIAGNOSIS.get(code, _MOCK_FALLBACK_DIAGNOSIS)
        return _MockResponse(template.format(name=name, plan=plan))


# ---------------------------------------------------------------------------
# LLM factory (monkeypatched in tests)
# ---------------------------------------------------------------------------

def get_llm():
    """Return the chat model used by the LLM-backed nodes.

    With no ``OPENAI_API_KEY`` set, returns a deterministic :class:`_MockLLM` so
    the public demo runs offline and free. With a key set, returns the real
    ``ChatOpenAI``. Centralised so tests can monkeypatch ``app.nodes.get_llm``.
    """
    if use_mock():
        return _MockLLM()
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.4,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_customer(customer_id: str) -> dict:
    """Look up a customer record from ``data/customers.json`` by ``id``.

    Returns an empty dict if the file is missing or no record matches, so the
    downstream nodes degrade gracefully instead of raising.
    """
    try:
        records = json.loads(CUSTOMERS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    for record in records:
        if record.get("id") == customer_id:
            return record
    return {}


def _llm_text(message: str) -> str:
    """Invoke the chat model with a single prompt and return plain text.

    Accepts both real LangChain message objects (``.content``) and fakes that
    return a bare string, keeping the test seam simple.
    """
    response = get_llm().invoke(message)
    content = getattr(response, "content", response)
    return str(content).strip()


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def retrieve_context(state: dict) -> dict:
    """Load the customer record and fetch RAG playbook snippets.

    Reads the failed-payment ``event`` from state, looks up the matching
    customer, then queries the playbook retriever using the failure code and
    plan so the relevant dunning guidance is pulled in.
    """
    event = state["event"]

    customer = _load_customer(event.get("customer_id", ""))

    # Build a focused retrieval query from the signals that drive dunning
    # handling: why the payment failed and which plan the customer is on.
    plan = customer.get("plan", "")
    failure_code = event.get("failure_code", "")
    query = f"failure reason {failure_code} dunning strategy for {plan} plan".strip()

    retriever = get_retriever()
    docs = retriever.invoke(query)
    context = "\n\n".join(getattr(doc, "page_content", str(doc)) for doc in docs)

    return {"customer": customer, "context": context}


def diagnose_reason(state: dict) -> dict:
    """Produce a short, grounded diagnosis of why the payment failed."""
    event = state["event"]
    customer = state.get("customer", {})
    context = state.get("context", "")

    prompt = (
        "You are PayPilot, a payments recovery analyst. In 1-2 sentences, "
        "diagnose why this subscription payment failed and what it means for "
        "recovery. Be concrete and ground your answer in the playbook context.\n\n"
        f"Failed payment event: {event}\n"
        f"Customer record: {customer}\n\n"
        f"Playbook context:\n{context}\n"
    )

    diagnosis = _llm_text(prompt)
    return {"diagnosis": diagnosis}


def choose_strategy(state: dict) -> dict:
    """Pick the recovery strategy deterministically from the failure code.

    No LLM here on purpose: the action / retry cadence / offer come from a fixed
    rules table (see ``_STRATEGY_RULES``) so behaviour is stable and testable.
    """
    failure_code = state["event"].get("failure_code", "")
    rule = _STRATEGY_RULES.get(failure_code, _DEFAULT_STRATEGY)
    # Return a copy so downstream mutation can't corrupt the shared rules table.
    strategy = dict(rule)
    return {"strategy": strategy}


def draft_message(state: dict) -> dict:
    """Draft a short, warm, on-brand dunning email body from the full state."""
    event = state["event"]
    customer = state.get("customer", {})
    context = state.get("context", "")
    diagnosis = state.get("diagnosis", "")
    strategy = state.get("strategy", {})

    name = customer.get("name", "there")
    plan = customer.get("plan", "your")

    prompt = (
        "You are PayPilot, writing on behalf of a friendly SaaS billing team. "
        "Write a SHORT dunning email body (no subject line, 3-5 sentences) to "
        f"{name} about a failed payment on their {plan} plan.\n\n"
        "Requirements:\n"
        "- Warm and helpful, never blaming. Frame it as 'let's fix this together'.\n"
        "- Reference the specific plan and gently explain the issue.\n"
        "- Give ONE clear call to action that matches the recovery strategy.\n"
        "- Reassure them their service stays on for now, and invite a reply.\n"
        "- Plain text only; sign off as 'The PayPilot Team'.\n\n"
        f"Failed payment event: {event}\n"
        f"Diagnosis: {diagnosis}\n"
        f"Recovery strategy: {strategy}\n\n"
        f"Playbook tone & guidance:\n{context}\n"
    )

    message = _llm_text(prompt)
    return {"message": message}


def finalize(state: dict) -> dict:
    """Assemble the final API payload from the produced state fields."""
    output = {
        "diagnosis": state.get("diagnosis", ""),
        "strategy": state.get("strategy", {}),
        "message": state.get("message", ""),
    }
    return {"output": output}
