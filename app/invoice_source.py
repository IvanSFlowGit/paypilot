# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""One shape for an unpaid-invoice source, whatever system it came from.

A card decline arrives as a code. An invoice arrives as a status, a due date and
sometimes silence, so every source (a CSV export, a bank's receivables API, an
accounting tool) is normalised into :class:`InvoiceRecord` before anything
reasons about it. Nothing here decides, sends or writes: this module only
validates and ages. The decision table stays untouched.

Rules the tests pin:

* Money is an integer in minor units, parsed from a string with :mod:`decimal`.
  A float is refused, because ``0.1 + 0.2`` is how an invoice total drifts.
* The customer is a salted hash, never a name or an address, so a record can be
  logged and moved without carrying personal data. The salt is passed in; there
  is no default, because a default salt is a shared salt.
* A missing due date is its own bucket, ``no_due_date``. It is never read as
  zero days overdue, because a missing value that reads as "on time" is
  confidently wrong in the flattering direction.
* Errors name the field and never echo the rejected value.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.money import exponent

#: The closed set of states a record can be in. ``unknown`` exists so a source
#: status this module has not been taught is carried as unknown and never
#: guessed into one of the others.
STATUSES = frozenset({"open", "processing", "paid", "cancelled", "unknown"})

#: Upper bound on an amount in minor units (one trillion major units at two
#: places is far past any real invoice). A parse that lands above it is a unit
#: error upstream, not a large invoice.
MAX_MINOR = 10**14

_CURRENCY_RE = re.compile(r"^[a-z]{3}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:/#-]{1,128}$")
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Ageing buckets in the order they print.
BUCKETS = (
    "no_due_date",
    "closed",
    "processing",
    "not_due",
    "overdue_1_7",
    "overdue_8_30",
    "overdue_31_60",
    "overdue_61_plus",
)


class InvoiceError(ValueError):
    """A record failed validation. The message names the field, never the value."""


@dataclass(frozen=True)
class InvoiceRecord:
    source: str
    source_id: str
    invoice_no: str
    customer_ref: str
    amount_minor: int
    currency: str
    status: str
    issued: date | None = None
    due: date | None = None
    paid_at: date | None = None

    def __post_init__(self) -> None:
        if not _SOURCE_RE.match(self.source or ""):
            raise InvoiceError("source: expected lowercase letters, digits, underscore")
        if not _ID_RE.match(self.source_id or ""):
            raise InvoiceError("source_id: expected 1 to 128 id characters")
        if not _ID_RE.match(self.invoice_no or ""):
            raise InvoiceError("invoice_no: expected 1 to 128 id characters")
        if not re.fullmatch(r"[0-9a-f]{16}", self.customer_ref or ""):
            raise InvoiceError("customer_ref: expected the 16 hex characters make_customer_ref returns")
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise InvoiceError("amount_minor: expected an integer")
        if not 0 < self.amount_minor <= MAX_MINOR:
            raise InvoiceError("amount_minor: out of range")
        if not _CURRENCY_RE.match(self.currency or ""):
            raise InvoiceError("currency: expected a three letter lowercase code")
        if self.status not in STATUSES:
            raise InvoiceError("status: not in the closed set")
        for name in ("issued", "due", "paid_at"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, date):
                raise InvoiceError(f"{name}: expected a date or None")


def make_customer_ref(raw: str, salt: str) -> str:
    """Salted, truncated hash of a customer identifier.

    ``salt`` is required and must be non-empty. Truncation to 16 hex characters
    keeps it short in reports and is not a security claim: the salt is what
    stops a reader re-deriving a name from a list of common ones.
    """
    if not isinstance(salt, str) or len(salt) < 8:
        raise InvoiceError("salt: expected a string of at least 8 characters")
    cleaned = (raw or "").strip().lower()
    if not cleaned:
        raise InvoiceError("customer: empty")
    return hashlib.sha256((salt + "\x1f" + cleaned).encode("utf-8")).hexdigest()[:16]


def parse_minor(text: object, currency: str) -> int:
    """Parse a decimal amount string into integer minor units.

    Refuses a float, a bool, NaN, infinity, a thousands separator, more decimal
    places than the currency has, and a non-positive result. A source that
    sends ``12,50`` or ``1.234,50`` gets a refusal, not a guess, because the
    comma is a decimal mark in half of Europe and a thousands mark in the rest.
    """
    if isinstance(text, bool) or isinstance(text, float):
        raise InvoiceError("amount: a float is refused, send a decimal string")
    if isinstance(text, int):
        raise InvoiceError("amount: a bare integer is ambiguous between major and minor units")
    if not isinstance(text, str):
        raise InvoiceError("amount: expected a decimal string")
    raw = text.strip()
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", raw):
        raise InvoiceError("amount: expected digits with an optional decimal point")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - the regex already refused these
        raise InvoiceError("amount: not a number") from exc
    places = exponent(currency)
    scaled = value * (10**places)
    if scaled != scaled.to_integral_value():
        raise InvoiceError("amount: more decimal places than the currency has")
    minor = int(scaled)
    if not 0 < minor <= MAX_MINOR:
        raise InvoiceError("amount: out of range")
    return minor


def parse_date(text: object) -> date | None:
    """ISO ``YYYY-MM-DD`` only. Empty or None is None; anything else is refused."""
    if text is None:
        return None
    if isinstance(text, date):
        return text
    if not isinstance(text, str):
        raise InvoiceError("date: expected an ISO date string")
    raw = text.strip()
    if not raw:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise InvoiceError("date: expected YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvoiceError("date: not a real calendar date") from exc


def days_overdue(record: InvoiceRecord, today: date) -> int | None:
    """Days past due as of ``today``. Negative means not yet due. None means no due date."""
    if record.due is None:
        return None
    return (today - record.due).days


def bucket(record: InvoiceRecord, today: date) -> str:
    """The ageing bucket for ``record`` as of ``today``. ``today`` is always passed in."""
    if record.status in ("paid", "cancelled"):
        return "closed"
    if record.status == "processing":
        return "processing"
    overdue = days_overdue(record, today)
    if overdue is None:
        return "no_due_date"
    if overdue <= 0:
        return "not_due"
    if overdue <= 7:
        return "overdue_1_7"
    if overdue <= 30:
        return "overdue_8_30"
    if overdue <= 60:
        return "overdue_31_60"
    return "overdue_61_plus"
