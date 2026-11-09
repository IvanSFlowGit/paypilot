# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Label an unpaid invoice with one of five states, by arithmetic and flags only.

Input is an :class:`InvoiceRecord` plus :class:`InvoiceFacts` (chases sent,
replies received, a dispute flag, a promised-by date). Output is a label and the
rule that fired. No model, no network, no clock: ``today`` is always passed in.

Precedence, highest first: disputed, promised_not_paid, no_reply, overdue_60,
overdue_30, UNKNOWN. Any label whose action escalates or suppresses comes from
deterministic evidence, or the answer is UNKNOWN and a person decides.

UNKNOWN is a real answer, and the rule id says why. A reader that never says
UNKNOWN is guessing.

This module does not touch the strategy table and does not send anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.invoice_source import InvoiceRecord, days_overdue

#: Day boundaries are starting numbers, to be re-read against a real export.
FIRST_CHASE_DAYS = 30
FINANCE_ESCALATION_DAYS = 60
#: Chases with zero inbound before the channel changes.
NO_REPLY_CHASES = 2

LABELS = ("disputed", "promised_not_paid", "no_reply", "overdue_60", "overdue_30", "UNKNOWN")


@dataclass(frozen=True)
class InvoiceFacts:
    """What the ageing alone cannot say. ``replies_received`` None means untracked."""

    chases_sent: int = 0
    replies_received: int | None = None
    disputed: bool = False
    promised_by: date | None = None

    def __post_init__(self) -> None:
        if isinstance(self.chases_sent, bool) or not isinstance(self.chases_sent, int) or self.chases_sent < 0:
            raise ValueError("chases_sent: expected a non-negative integer")
        r = self.replies_received
        if r is not None and (isinstance(r, bool) or not isinstance(r, int) or r < 0):
            raise ValueError("replies_received: expected a non-negative integer or None")
        if not isinstance(self.disputed, bool):
            raise ValueError("disputed: expected a bool")
        if self.promised_by is not None and not isinstance(self.promised_by, date):
            raise ValueError("promised_by: expected a date or None")


@dataclass(frozen=True)
class Verdict:
    label: str
    rule_id: str


def classify(record: InvoiceRecord, facts: InvoiceFacts, today: date) -> Verdict:
    if record.status in ("paid", "cancelled", "processing"):
        return Verdict("UNKNOWN", "not_open")
    if record.status == "unknown":
        return Verdict("UNKNOWN", "status_unknown")
    if facts.disputed:
        return Verdict("disputed", "disputed")
    if facts.promised_by is not None:
        if facts.promised_by < today:
            return Verdict("promised_not_paid", "promised_not_paid")
        return Verdict("UNKNOWN", "promise_pending")
    overdue = days_overdue(record, today)
    if overdue is None:
        return Verdict("UNKNOWN", "no_due_date")
    if overdue > 0 and facts.chases_sent >= NO_REPLY_CHASES:
        if facts.replies_received is None:
            return Verdict("UNKNOWN", "replies_untracked")
        if facts.replies_received == 0:
            return Verdict("no_reply", "no_reply")
    if overdue >= FINANCE_ESCALATION_DAYS:
        return Verdict("overdue_60", "overdue_60")
    if overdue >= FIRST_CHASE_DAYS:
        return Verdict("overdue_30", "overdue_30")
    return Verdict("UNKNOWN", "below_first_chase")
