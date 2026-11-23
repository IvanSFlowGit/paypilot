# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Is money for this invoice already moving? Match a bank feed to an invoice and say HOLD or RELEASE.

Pure function. No network, no clock (``today`` is passed in), no write. It reads
:class:`InvoiceRecord` plus what a bank feed showed and returns one verdict and the
rule that fired. It never marks an invoice paid and never sends anything.

What counts as in flight, and why it is narrow:

* Only a CREDIT whose description carries the invoice number as a whole token holds a
  chase. An amount-only match is reported as ``possible_match`` and does NOT hold,
  because a hold suppresses a chase and a suppression needs deterministic evidence.
* Debits are ignored. A currency mismatch is UNKNOWN, never a guess.
* EVERY HOLD EXPIRES. A suppression with no exit is a blind spot with a reason
  attached: a payment that never lands would never be chased. Pending credits hold
  ``PENDING_HOLD_BUSINESS_DAYS`` from first seen; booked credits hold
  ``BOOKED_HOLD_BUSINESS_DAYS`` for the ledger to catch up. Both are starting numbers
  to be re-read against a real feed, not measurements.
* A feed that is not alive returns UNKNOWN ``feed_unreachable``. It never reads as
  "nothing pending".

Not handled, stated rather than implied: weekends are skipped but there is no bank
holiday calendar, and a credit that is later returned is released only by expiry, since
this module is not shown the return event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.bank.provider import Transaction
from app.invoice_source import InvoiceRecord

# A credit below the invoice amount never holds: anyone can send one cent with the reference.
# It is UNKNOWN with the rule ``partial_credit``, which a person decides.
PENDING_HOLD_BUSINESS_DAYS = 3
BOOKED_HOLD_BUSINESS_DAYS = 5

STATE_PENDING = "pending"
STATE_BOOKED = "booked"
STATES = (STATE_PENDING, STATE_BOOKED)

HOLD = "HOLD"
RELEASE = "RELEASE"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Observation:
    """One bank-feed line and what the feed called it. ``first_seen`` is when WE first saw it."""

    txn: Transaction
    state: str
    first_seen: date

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"state {self.state!r} is not one of {STATES}")
        if not isinstance(self.first_seen, date):
            raise ValueError("first_seen: expected a date")


@dataclass(frozen=True)
class InFlightVerdict:
    label: str  # HOLD, RELEASE or UNKNOWN
    rule_id: str
    until: date | None = None
    matched: int = 0  # reference matches that were credits
    possible: int = 0  # amount-only matches, reported and never held on
    scanned: int = 0  # every line read, so a caller can see the denominator
    matched_minor: int = 0  # sum of the reference-matched credits, in the invoice currency
    pending_only: bool = False  # True when every matched credit is still pending


def add_business_days(start: date, n: int) -> date:
    d = start
    left = n
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


_ID_EDGE = "A-Za-z0-9_:/#-"


def _mentions(text: str, invoice_no: str) -> bool:
    """True when the description carries the invoice number as a whole token.

    The boundary treats every character an invoice id may contain as part of the token, so
    ``INV-1001`` is not found inside ``INV-1001-2`` or ``INV-1001/A``. A full stop ends the
    token only when nothing alphanumeric follows it, so "ref INV-1001." still matches.
    """
    return (
        re.search(
            rf"(?<![{_ID_EDGE}.])" + re.escape(invoice_no) + rf"(?![{_ID_EDGE}]|\.[A-Za-z0-9])", text or "", re.I
        )
        is not None
    )


def _short_id(invoice_no: str) -> bool:
    """A short or all-digit id is a coincidence waiting to happen ("7", "1001"), so a mention
    alone is not enough: the credit must also carry the invoice amount."""
    return invoice_no.isdigit() or len(invoice_no) < 5


def assess(
    record: InvoiceRecord,
    observations: list[Observation],
    today: date,
    *,
    feed_alive: bool,
) -> InFlightVerdict:
    scanned = len(observations)
    if not feed_alive:
        return InFlightVerdict(UNKNOWN, "feed_unreachable", scanned=scanned)

    holds: list[date] = []
    matched = 0
    matched_minor = 0
    states: set[str] = set()
    possible = 0
    saw_currency_mismatch = False
    for ob in observations:
        t = ob.txn
        if t.amount_minor <= 0:
            continue
        if _mentions(t.description, record.invoice_no) and (
            not _short_id(record.invoice_no) or t.amount_minor == record.amount_minor
        ):
            if t.currency.lower() != record.currency:
                saw_currency_mismatch = True
                continue
            matched += 1
            matched_minor += t.amount_minor
            states.add(ob.state)
            days = PENDING_HOLD_BUSINESS_DAYS if ob.state == STATE_PENDING else BOOKED_HOLD_BUSINESS_DAYS
            base = ob.first_seen if ob.state == STATE_PENDING else t.booked_at.date()
            holds.append(add_business_days(base, days))
        elif t.currency.lower() == record.currency and t.amount_minor == record.amount_minor:
            possible += 1

    if saw_currency_mismatch and not holds:
        return InFlightVerdict(UNKNOWN, "currency_mismatch", scanned=scanned, possible=possible)
    if not holds:
        return InFlightVerdict(RELEASE, "nothing_in_flight", scanned=scanned, possible=possible)
    until = max(holds)
    if until < today:
        return InFlightVerdict(
            RELEASE, "hold_expired", until=until, matched=matched, possible=possible, scanned=scanned,
            matched_minor=matched_minor, pending_only=states == {STATE_PENDING},
        )
    if matched_minor < record.amount_minor:
        return InFlightVerdict(
            UNKNOWN, "partial_credit", until=until, matched=matched, possible=possible, scanned=scanned,
            matched_minor=matched_minor, pending_only=states == {STATE_PENDING},
        )
    return InFlightVerdict(
        HOLD, "credit_in_flight", until=until, matched=matched, possible=possible, scanned=scanned,
        matched_minor=matched_minor, pending_only=states == {STATE_PENDING},
    )


def observe(
    txns: tuple[Transaction, ...] | list[Transaction],
    seen: dict[str, date],
    today: date,
) -> list[Observation]:
    """Wrap feed lines as observations. ``seen`` maps transaction id to the date WE first saw it;
    an id never seen before is first seen today. The caller persists ``seen``, this does not."""
    return [Observation(t, t.status, seen.get(t.transaction_id, today)) for t in txns]
