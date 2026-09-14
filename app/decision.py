# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The dunning decision, with nothing else attached.

One request in, the rules table consulted, a decision out, and the rule that
fired named in the response. This is the part of PayPilot that decides money,
so it is kept free of the model, the retriever, Stripe and the web framework:
standard library only, so the same code runs inside the FastAPI app on Fly and
inside a Lambda function without dragging LangChain into the deployment package.

``app/nodes.py`` imports the table and the scoring from here, so the graph and
the standalone endpoint cannot drift into two rule sets.

Validation lives here too, not in either HTTP layer. Both deployments return
the exact same status and body for the exact same input, which is what lets one
contract test suite run against both.
"""

from __future__ import annotations

import hmac
import re
from datetime import UTC, datetime

#: Env var holding the bearer token for the decision routes. Unlike the demo
#: routes in app/auth.py, these FAIL CLOSED when it is unset: the decision
#: endpoint writes an audit row per call, so an open one is a free write path
#: into a paid database.
DECISION_TOKEN_ENV = "DECISION_API_TOKEN"

# Deterministic dunning rules keyed by Stripe-style failure code. Kept here as a
# table (not LLM-decided) so strategy is stable and unit-testable. The values
# mirror the "Retry cadence summary" in data/playbook.md.
STRATEGY_RULES: dict[str, dict] = {
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

# Fallback for any unexpected failure code, so a decision never crashes on a
# value outside the three documented codes.
DEFAULT_STRATEGY: dict = {
    "action": "retry_and_verify",
    "retry_in_days": 2,
    "offer": "Retry once and ask the customer to verify their payment method.",
}

#: Name recorded as the fired rule when the failure code is not in the table.
DEFAULT_RULE = "default"

# Bounds on the request. An invoice id is the only identifier accepted, and it
# must look like one: this endpoint never sees a name, an email or a card.
_INVOICE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_FAILURE_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_MAX_ATTEMPT = 50
_MAX_PRIOR_FAILURES = 50

#: Largest request body either deployment accepts. The valid input is four short fields.
MAX_BODY_BYTES = 4096

_ALLOWED_FIELDS = frozenset({"invoice_id", "failure_code", "attempt", "prior_failures"})


class DecisionInputError(ValueError):
    """The request body is not a valid decision input. Carries a safe message."""


def score_churn_risk(attempt: int, prior_failures: int) -> str:
    """Bucket churn risk from the dunning attempt number and recent failure streak.

    Later attempts and a run of recent failures both push the risk up: by the
    third attempt, or with enough recent misses, this is a customer at real risk
    of involuntary churn rather than a routine retry.
    """
    score = attempt + prior_failures
    if attempt >= 3 or score >= 4:
        return "high"
    if attempt >= 2 or score >= 2:
        return "medium"
    return "low"


def rule_name(failure_code: str) -> str:
    """The rule that fires for ``failure_code``: the code itself, or ``default``."""
    return failure_code if failure_code in STRATEGY_RULES else DEFAULT_RULE


def strategy_for(failure_code: str, *, escalate: bool) -> dict:
    """Return a fresh strategy dict for ``failure_code``, escalated if asked.

    Always a copy, so a caller mutating the result cannot corrupt the shared
    rules table.
    """
    strategy = dict(STRATEGY_RULES.get(failure_code, DEFAULT_STRATEGY))
    if escalate:
        # Repeat failure: pull the retry in by a day (floor at 1) and make the
        # ask firmer, since a warm-but-passive nudge clearly hasn't landed.
        strategy["retry_in_days"] = max(1, int(strategy["retry_in_days"]) - 1)
        strategy["offer"] = (
            strategy["offer"]
            + " This is a repeat failure - tighten the retry window and make the "
            "call to action firmer and time-boxed."
        )
        strategy["escalated"] = True
    else:
        strategy["escalated"] = False
    return strategy


def _int_field(body: dict, name: str, *, default: int, minimum: int, maximum: int) -> int:
    value = body.get(name, default)
    # bool is an int subclass; True must not pass as attempt=1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionInputError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise DecisionInputError(f"{name} must be between {minimum} and {maximum}")
    return value


def parse_input(body: object) -> dict:
    """Validate a decision request body. Raises :class:`DecisionInputError`.

    Error messages name the field and the constraint, never the rejected value,
    so a caller who pastes something sensitive does not get it echoed back into
    a response or an access log.
    """
    if not isinstance(body, dict):
        raise DecisionInputError("body must be a JSON object")
    unknown = set(body) - _ALLOWED_FIELDS
    if unknown:
        raise DecisionInputError(f"unknown fields: {', '.join(sorted(unknown))}")

    invoice_id = body.get("invoice_id")
    if not isinstance(invoice_id, str) or not _INVOICE_ID_RE.fullmatch(invoice_id):
        raise DecisionInputError("invoice_id must be 1-64 characters of [A-Za-z0-9_-]")
    failure_code = body.get("failure_code")
    if not isinstance(failure_code, str) or not _FAILURE_CODE_RE.fullmatch(failure_code):
        raise DecisionInputError("failure_code must be 1-64 characters of [a-z0-9_]")

    return {
        "invoice_id": invoice_id,
        "failure_code": failure_code,
        "attempt": _int_field(body, "attempt", default=1, minimum=1, maximum=_MAX_ATTEMPT),
        "prior_failures": _int_field(
            body, "prior_failures", default=0, minimum=0, maximum=_MAX_PRIOR_FAILURES
        ),
    }


def decide(decision_input: dict, *, now: datetime | None = None) -> dict:
    """Decide the dunning action for a validated input. Pure: no I/O."""
    attempt = decision_input["attempt"]
    prior_failures = decision_input["prior_failures"]
    failure_code = decision_input["failure_code"]
    churn_risk = score_churn_risk(attempt, prior_failures)
    strategy = strategy_for(failure_code, escalate=churn_risk == "high")
    return {
        "invoice_id": decision_input["invoice_id"],
        "rule_fired": rule_name(failure_code),
        "input": dict(decision_input),
        "churn_risk": churn_risk,
        "strategy": strategy,
        "decided_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
    }


def check_bearer(auth_header: str | None, expected_token: str | None) -> tuple[int, dict] | None:
    """Return ``None`` when authorised, else the ``(status, body)`` to send.

    Fails closed: no configured token is a 503, never an allow. Constant-time
    comparison over bytes, matching app/auth.py's reason for bytes.
    """
    if not (expected_token and expected_token.strip()):
        return 503, {"error": "not_configured", "detail": "decision API token not configured"}
    if not auth_header or not auth_header.startswith("Bearer "):
        return 401, {"error": "unauthorised", "detail": "bearer token required"}
    supplied = auth_header[len("Bearer "):].strip()
    if not hmac.compare_digest(supplied.encode("utf-8"), expected_token.strip().encode("utf-8")):
        return 401, {"error": "unauthorised", "detail": "bearer token required"}
    return None


def handle_decision_request(body: object, audit) -> tuple[int, dict]:
    """Validate, decide, record. Returns ``(status_code, json_body)``.

    ``audit`` is anything with ``record(decision) -> str`` returning the audit
    row id. The decision is only returned once the row is written: a decision
    with no audit trail is refused with a 503 rather than returned unrecorded,
    because an unrecorded money decision is the one thing this path must never
    produce.
    """
    try:
        decision_input = parse_input(body)
    except DecisionInputError as exc:
        return 422, {"error": "invalid_input", "detail": str(exc)}
    decision = decide(decision_input)
    try:
        audit_id = audit.record(decision)
    except Exception:  # noqa: BLE001 - any storage failure fails closed
        return 503, {"error": "audit_unavailable", "detail": "decision not recorded"}
    return 200, {**decision, "audit_id": audit_id}


def handle_audit_lookup(invoice_id: object, audit) -> tuple[int, dict]:
    """Return the recorded decisions for one invoice, newest first."""
    if not isinstance(invoice_id, str) or not _INVOICE_ID_RE.fullmatch(invoice_id):
        return 422, {"error": "invalid_input", "detail": "invalid invoice_id"}
    try:
        rows = audit.for_invoice(invoice_id)
    except Exception:  # noqa: BLE001
        return 503, {"error": "audit_unavailable", "detail": "audit store unreachable"}
    if not rows:
        return 404, {"error": "not_found", "detail": "no decisions for that invoice"}
    return 200, {"invoice_id": invoice_id, "decisions": rows}
