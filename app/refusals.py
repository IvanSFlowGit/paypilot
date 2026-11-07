# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""One read-only view of every refusal, over the stores that already hold them.

Nothing here writes. The mailer and the approval flow already record each refusal as a
``suppressed`` message carrying a reason code, and ``POST /decide`` records a refused body
in ``decision_refusals``. What was missing was a single place to read them, and a statement
of WHICH GATE each reason belongs to. Duplicating the writes into a second table would have
created two records of one event that can disagree.

THE GATE MAP IS THE CONTROL, and it is checked against the code rather than against itself:
``tests/test_refusals.py`` walks ``app/mailer.py`` and ``app/approval.py`` and fails when a
reason code the code can write has no gate here. A reason that gets past the map still
appears in the listing, as gate ``unclassified`` with ``classified: false``, because
dropping it would make an unnamed refusal look like no refusal at all.
"""

from __future__ import annotations

#: Reason code -> the gate that refused. Names a gate by what it protects.
REASON_GATES: dict[str, str] = {
    # mailer.py
    "tenant_suspended": "tenant_active",
    "recipient_not_allowlisted": "recipient_allowlist",
    "missing_resend_api_key": "send_credentials",
    "sent_unrecorded_today": "send_cooldown",
    "failed_output_guard": "output_guard",
    "missing_from_address": "sender_identity",
    # approval.py: opening a request
    "approver_skipped": "approval_gate",
    "no_approver_configured": "approval_gate",
    "no_public_base_url": "approval_gate",
    "approval_request_undeliverable": "approval_gate",
    # approval.py: a person's decision, and the clock
    "skipped_by_approver": "approval_decision",
    "approval_expired": "approval_gate",
    # approval.py: _recheck_before_sending, the situation changed since the request
    "max_touches_reached": "approval_recheck",
    "cooldown_active": "approval_recheck",
    "body_changed_since_request": "approval_recheck",
}

#: A reason that carries a variable suffix, so the code cannot be listed in full.
PREFIX_GATES: dict[str, str] = {
    "invoice_already_": "approval_recheck",
}

UNCLASSIFIED = "unclassified"


def gate_for(reason: str) -> str | None:
    """The gate for a reason code, or ``None`` when the map does not know it."""
    if reason in REASON_GATES:
        return REASON_GATES[reason]
    for prefix, gate in PREFIX_GATES.items():
        if reason.startswith(prefix):
            return gate
    return None


def from_message(row: dict) -> dict:
    """One ledger entry from a ``suppressed`` message row."""
    reason = row["error"] or ""
    gate = gate_for(reason)
    return {
        "source": "message_store",
        "gate": gate or UNCLASSIFIED,
        "classified": gate is not None,
        "reason": reason,
        "invoice_id": row["invoice_id"],
        "at": row["created_at"],
    }


def from_decision(row: dict) -> dict:
    """One ledger entry from a ``decision_refusals`` row."""
    return {
        "source": "decision_ledger",
        "gate": row["gate"],
        "classified": True,
        "reason": row["reason"],
        "invoice_id": row["invoice_id"],
        "at": row["refused_at"],
    }


def from_fallthrough(row: dict) -> dict:
    """One entry for a decision that fell through to the conservative default row.

    Not an error and not a refusal of the request: the decision was made and recorded, on
    the row nobody wrote a rule for. It is listed because ``rule_fired: default`` alone does
    not say that no rule matched, and a reader asking what the system declined to guess at
    needs both.
    """
    return {
        "source": "decision_audit",
        "gate": "rule_lookup",
        "classified": True,
        "reason": "fell_through",
        "invoice_id": row["invoice_id"],
        "at": row["decided_at"],
    }


def from_audit_event(event: dict) -> dict:
    """One entry for a draft the output guard replaced with fixed copy."""
    fields = event.get("fields") or {}
    return {
        "source": "audit_event_log",
        "gate": "output_guard",
        "classified": True,
        "reason": "draft_replaced_with_fixed_copy",
        "invoice_id": "",
        "at": event["ts"],
        "node": fields.get("node"),
    }


def _guard_swaps(audit_log, limit: int) -> list[dict]:
    events = audit_log.query(event="llm_call", limit=5000)
    # The durable event carries status "fallback" (written by app.audit.audit_llm_call), not the
    # boolean the stdout line has; the test drives the real writer so the two cannot drift.
    swaps = [e for e in events if (e.get("fields") or {}).get("status") == "fallback"]
    return [from_audit_event(e) for e in swaps[-limit:]]


def unified(store, audit, *, limit: int = 100, audit_log=None, audit_log_scoped: bool = True) -> dict:
    """Every refusal for one tenant, newest first, from the stores that hold them.

    ``audit.refusals_for`` is read with the TENANT id as the client id. In multi mode that
    is exactly the decision client; in single mode the decision API's client ids come from
    its token map and may differ, so a refusal recorded under a different client id is not
    listed here, and ``decision_client_id`` says which id was read rather than implying all.
    """
    entries = [from_message(r) for r in store.refused_messages(limit)]
    entries += [from_decision(r) for r in audit.refusals_for(store.tenant_id, limit)]
    entries += [from_fallthrough(r) for r in audit.fallthroughs_for(store.tenant_id, limit)]

    # THE FOURTH SOURCE MAY NOT EXIST, AND AN EMPTY LIST WOULD HIDE THAT. The queryable event
    # log is opt-in (PAYPILOT_AUDIT_DB_PATH) and absent on the public deployment on purpose,
    # so a view with no swaps in it cannot tell "none happened" from "nowhere to look". The
    # state of every source is returned beside the entries so the reader never has to guess.
    sources = {
        "message_store": "ok",
        "decision_ledger": "ok",
        "decision_audit": "ok",
    }
    if audit_log is None:
        sources["audit_event_log"] = "not_configured"
    elif not audit_log_scoped:
        # The event log is platform-level and carries no tenant, so in multi mode listing it
        # here would show one tenant another's events.
        sources["audit_event_log"] = "not_tenant_scoped"
    else:
        sources["audit_event_log"] = "ok"
        entries += _guard_swaps(audit_log, limit)

    entries.sort(key=lambda e: e["at"] or "", reverse=True)
    entries = entries[:limit]
    return {
        "decision_client_id": store.tenant_id,
        "sources": sources,
        "refusals": entries,
        "unclassified": sum(1 for e in entries if not e["classified"]),
    }
