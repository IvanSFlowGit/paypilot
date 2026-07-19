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

# Make the app's own logs reach stdout in production. Uvicorn configures only its
# own loggers, and the root last-resort handler is WARNING-only, so without this
# the INFO audit events (and even the startup WARNINGs, which never had a stdout
# handler) are dropped and Fly never captures them. One INFO stdout handler on the
# shared "paypilot" PARENT logger covers audit events, the access log, and the
# startup warnings via propagation - emitted exactly once (only the parent carries
# a handler). ``propagate`` stays True so pytest's caplog still captures via root.
# Imported on every LLM path (nodes -> audit), so it is installed before app
# startup fires.
_app_log = logging.getLogger("paypilot")
_app_log.setLevel(logging.INFO)
if not any(getattr(h, "_paypilot_stdout", False) for h in _app_log.handlers):
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _handler._paypilot_stdout = True  # idempotency marker across reimports
    _app_log.addHandler(_handler)

# Constant that every per-request boundary is normalized to before hashing.
_BOUNDARY_CONST = "BOUNDARY"


def prompt_sha256(prompt: str, boundary: str | None) -> str:
    """sha256 of ``prompt`` with the random ``boundary`` normalized to a constant."""
    normalized = prompt.replace(boundary, _BOUNDARY_CONST) if boundary else prompt
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def audit_security_event(
    *,
    event: str,
    detail: str,
    severity: str = "warning",
    sink=None,
) -> dict:
    """Emit one security audit event (the alert channel for a rejected request).

    Used for webhook signature failures and for a missing signing secret in a
    production deployment. A rejected webhook is either a misconfiguration or
    someone forging events at a revenue system, and both deserve a log line
    loud enough to alert on rather than a silent 400.

    ``detail`` must never carry the payload, the signature, or the secret - only
    a description of what was rejected and why.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        "severity": severity,
        "detail": detail,
    }
    line = json.dumps(record)
    if sink is not None:
        sink(line)
    elif severity == "error":
        _audit_log.error(line)
    else:
        _audit_log.warning(line)
    return record


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
