"""PayPilot dunning eval cases: events, rubrics, and guardrails.

These drive the quality, guardrail, and regression evals. They exercise the
graph in its offline mock mode (no OPENAI_API_KEY), so the text under test is
PayPilot's own playbook-grounded output - free, deterministic, realistic.

Only the deterministic decision fields (risk / strategy / impact) are snapshot
regression-tested. The generated ``message`` (customer email) and ``diagnosis``
(internal brief) are quality-judged and guardrail-checked, since their exact
wording is allowed to evolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evalkit.guardrails import (
    max_words,
    min_words,
    must_not_contain,
    no_dashes,
    no_disclosure,
    no_unfilled_placeholders,
)
from evalkit.judge import Criterion, Rubric


@dataclass
class DunningCase:
    """One failed-payment scenario to evaluate end to end."""

    key: str
    event: dict
    name: str      # expected customer name (from data/customers.json)
    plan: str      # expected plan tier

    @property
    def context(self) -> dict:
        return {"name": self.name, "plan": self.plan, "failure_code": self.event["failure_code"]}


# Grounded in data/customers.json (id -> name / plan).
CASES: list[DunningCase] = [
    DunningCase("acme_card_expired_a1",
                {"customer_id": "cust_001", "amount": 1499.0, "currency": "usd",
                 "failure_code": "card_expired", "attempt": 1},
                "Acme Robotics", "Scale"),
    DunningCase("brightleaf_insufficient_a3",
                {"customer_id": "cust_002", "amount": 299.0, "currency": "usd",
                 "failure_code": "insufficient_funds", "attempt": 3},
                "Brightleaf Studios", "Pro"),
    DunningCase("nimbus_generic_a1",
                {"customer_id": "cust_003", "amount": 4200.0, "currency": "usd",
                 "failure_code": "generic_decline", "attempt": 1},
                "Nimbus Health", "Enterprise"),
    DunningCase("pixel_card_expired_a2",
                {"customer_id": "cust_004", "amount": 49.0, "currency": "usd",
                 "failure_code": "card_expired", "attempt": 2},
                "Pixel & Pour", "Starter"),
]


# ---------------------------------------------------------------------------
# Quality rubrics (heuristic by default; live judge with EVAL_JUDGE=live)
# ---------------------------------------------------------------------------

def _has_any(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(w in low for w in words)


MESSAGE_RUBRIC = Rubric(
    name="dunning_email",
    threshold=0.8,
    criteria=[
        Criterion("greeting", "Opens with a greeting to the customer by name.",
                  lambda t, c: t.strip().lower().startswith("hi") and c["name"].split()[0].lower() in t.lower()),
        Criterion("names_plan", "Names the customer's plan/subscription.",
                  lambda t, c: c["plan"].lower() in t.lower()),
        Criterion("reassures", "Reassures the customer their service is still active.",
                  lambda t, c: _has_any(t, ["still", "active", "nothing to worry", "service is", "stays active"])),
        Criterion("one_cta", "Gives a clear next action (update card / retry / check bank / reply).",
                  lambda t, c: _has_any(t, ["update", "retry", "check", "reply", "card", "payment method"])),
        Criterion("warm_signoff", "Ends on a warm sign-off.",
                  lambda t, c: _has_any(t, ["warmly", "thanks", "best,", "team"])),
    ],
)

DIAGNOSIS_RUBRIC = Rubric(
    name="dunning_diagnosis",
    threshold=0.66,
    criteria=[
        Criterion("grounded", "References the customer or their plan.",
                  lambda t, c: c["name"].split()[0].lower() in t.lower() or c["plan"].lower() in t.lower()),
        Criterion("explains_reason", "Explains why the payment failed.",
                  lambda t, c: _has_any(t, ["expired", "declined", "decline", "insufficient", "failed", "hold"])),
        Criterion("actionable", "Points at a recovery action.",
                  lambda t, c: _has_any(t, ["retry", "update", "verify", "check", "recover", "space"])),
    ],
)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

# Build mechanics that must never surface in customer-facing copy.
_BUILD_TERMS = [
    "faiss", "langgraph", "langchain", "openai", "chatgpt", "embedding",
    "vector store", "rag", "api key", "prompt", "mock", "llm",
]
_AI_TELLS = ["as an ai", "language model", "i cannot ", "i am unable", "as a large"]

MESSAGE_GUARDS = [
    no_dashes(),
    no_unfilled_placeholders(),
    min_words(25),
    max_words(220),
    no_disclosure(_BUILD_TERMS),
    must_not_contain(_AI_TELLS),
]

DIAGNOSIS_GUARDS = [
    no_dashes(),
    no_unfilled_placeholders(),
    min_words(15),
    max_words(160),
    must_not_contain(_AI_TELLS),
]

# Fields that are deterministic and safe to snapshot (dates excluded).
SNAPSHOT_FIELDS = ("risk", "strategy", "impact")

# Where regression snapshots live.
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
