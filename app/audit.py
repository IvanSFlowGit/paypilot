"""Structured audit logging for every LLM call.

One JSON event per LLM call, emitted *after* the full guard + re-hydrate +
re-check chain so it carries the final verdict (baseline item 7). The event is
written to stdout via a dedicated logger, which Fly captures.

No PII in the event: it records the node, model, a boundary-normalized prompt
hash, the guard verdict, and the injection/fallback flags - never the customer
name/email or the prompt text itself.

The prompt hash normalizes the per-request random fence boundary to a constant
before hashing, so the same template with the same data hashes identically
across requests (an un-normalized hash would be unique every time - the random
boundary changes each call - and so useless for comparison or dedup).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone

_audit_log = logging.getLogger("paypilot.audit")

# Emit audit events to stdout explicitly. Uvicorn configures only its own
# loggers, and the root logger's last-resort handler is WARNING-only, so an
# INFO-level app logger would otherwise be dropped in production - the audit
# trail must reach stdout for Fly to capture it. A dedicated INFO stdout handler
# guarantees that. ``propagate`` stays True so pytest's caplog still captures the
# records via the root logger (no double output in prod: root has no INFO handler).
_audit_log.setLevel(logging.INFO)
if not any(getattr(h, "_paypilot_audit", False) for h in _audit_log.handlers):
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _handler._paypilot_audit = True  # idempotency marker across reimports
    _audit_log.addHandler(_handler)

# Constant that every per-request boundary is normalized to before hashing.
_BOUNDARY_CONST = "BOUNDARY"


def prompt_sha256(prompt: str, boundary: str | None) -> str:
    """sha256 of ``prompt`` with the random ``boundary`` normalized to a constant."""
    normalized = prompt.replace(boundary, _BOUNDARY_CONST) if boundary else prompt
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def audit_llm_call(
    *,
    node: str,
    model: str,
    prompt_template_id: str,
    prompt: str,
    boundary: str | None,
    guards_failed: list[str],
    injection_suspected: bool,
    fallback_used: bool,
    duration_ms: float,
    sink=None,
) -> dict:
    """Build, emit, and return one audit event for a single LLM call.

    ``guards_failed`` is the list of output-safety violations that tripped the
    fail-closed path (empty when the model's text shipped as-is). ``sink``, when
    given, receives the JSON line instead of the module logger (used by tests).
    """
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": "llm_call",
        "node": node,
        "model": model,
        "prompt_template_id": prompt_template_id,
        "prompt_sha256": prompt_sha256(prompt, boundary),
        "boundary_id": boundary,
        "guards_passed": not guards_failed,
        "guards_failed": list(guards_failed),
        "injection_suspected": bool(injection_suspected),
        "fallback_used": bool(fallback_used),
        "duration_ms": round(float(duration_ms), 1),
    }
    line = json.dumps(event)
    if sink is not None:
        sink(line)
    else:
        _audit_log.info(line)
    return event
