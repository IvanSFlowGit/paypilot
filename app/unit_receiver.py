# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Receive a Unit webhook delivery, decide each returned ACH payment, record every decision.

This is the receiver ``app/unit_map.py`` said was not mounted. It decides nothing itself:
the decision comes from ``unit_map.parse_unit_delivery`` and the ACH table. What it adds is
the part a real delivery needs and the offline replay did not:

* the signature is verified over the raw body (fail closed: no secret and signatures
  required is a 400, and a wrong or missing signature is a 400 plus a security audit event);
* every event that is a return is RECORDED before the answer is sent, held ones included,
  because a held return is exactly the one a person must be able to find. A write that fails
  is a 503, so Unit retries (it documents at-least-once delivery) rather than a 200 that
  loses the decision;
* it never sends, schedules or resubmits anything.

KNOWN LIMITS, stated so nobody reads this as proven. NO LIVE UNIT PAYLOAD HAS EVER REACHED
THIS CODE: the tests use synthetic bodies shaped like Unit's published pages, which proves
the plumbing and refusals and does not prove Unit's real events carry a reason. A retried
delivery can write a second row for the same event (the audit table is append-only history and
this does not dedupe by Unit event id). The account-to-client mapping is the single configured
client, so this is single-tenant only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.unit_map import MAX_BYTES, UnitPayloadError, parse_unit_delivery, verify_unit_signature


def _decision(r, client_id: str, now: datetime) -> dict:
    code = r.code or "unknown"
    rule = f"unit_ach_{code}_row{r.decision_row}" if r.status == "decided" and r.decision_row else f"unit_ach_{r.status}"
    return {
        "invoice_id": r.payment_id or r.event_id or "unknown",
        "client_id": client_id,
        "rule_fired": rule,
        "decided_at": now.isoformat(),
        "input": {"source": "unit", "event_id": r.event_id, "reason": r.reason, "code": r.code},
        "rail": "ach",
        "status": r.status,
        "person_required": r.person_required,
        "why": r.why,
        "customer_message": r.customer_message,
        "acted": False,
    }


def handle_unit_webhook(
    raw: bytes,
    signature: str | None,
    secret: str,
    signature_required: bool,
    audit,
    client_id: str,
    on_reject: Callable[[str, str], None] = lambda event, detail: None,
    now: datetime | None = None,
) -> tuple[int, dict]:
    """Returns ``(status, body)``. Pure apart from ``audit.record`` and ``on_reject``."""
    if len(raw) > MAX_BYTES:
        return 413, {"error": "body_too_large"}
    secret = (secret or "").strip()
    if secret:
        if not verify_unit_signature(raw, signature, secret):
            on_reject("unit_webhook_signature_rejected", "Unit webhook signature missing or wrong")
            return 400, {"error": "bad_signature"}
    elif signature_required:
        on_reject("unit_webhook_unsigned_refused", "UNIT_WEBHOOK_SECRET is unset and signatures are required")
        return 400, {"error": "unsigned_not_accepted"}
    try:
        results = parse_unit_delivery(raw)
    except UnitPayloadError as exc:
        return 400, {"error": "invalid_payload", "detail": str(exc)[:120]}
    stamp = now or datetime.now(UTC)
    ids: list[str] = []
    decided = held = not_returns = duplicates = 0
    for r in results:
        if r.status == "not_a_return":
            not_returns += 1
            continue
        decision = _decision(r, client_id, stamp)
        try:
            # Unit delivers at least once and retried each event up to 7 times within seconds, measured
            # 2026-11-10 in the sandbox. A return is terminal, so one decision per payment id (the event
            # id when a payment id is missing) is the key; a repeat is acknowledged and not recorded twice.
            # No key at all ("unknown") cannot be deduplicated, so it is recorded rather than dropped.
            key = decision["invoice_id"]
            if key != "unknown" and audit.has_decision(key, client_id):
                duplicates += 1
                continue
            ids.append(audit.record(decision))
        except Exception:  # noqa: BLE001 - any storage failure fails closed
            return 503, {"error": "audit_unavailable", "detail": "decision not recorded", "recorded_before_failure": len(ids)}
        if r.status == "decided":
            decided += 1
        else:
            held += 1
    return 200, {"received": True, "decided": decided, "held_for_a_person": held, "not_returns": not_returns,
                 "duplicates": duplicates, "audit_ids": ids, "acted": False}
