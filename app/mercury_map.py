# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Speak Mercury: verify a webhook and translate a receivables invoice.

Mercury is a business bank whose API exposes accounts receivable. It is not a
card processor, so there is no decline code: an unpaid invoice is a status, a
due date and sometimes silence. This module only reads and translates; it never
decides, sends, or calls Mercury.

Everything below is taken from Mercury's own documentation as read on
2026-10-25 through an extractor, and is recorded as RELAYED or NEVER CHECKED in
``docs/claims.md`` rows 41 to 49. Where the documentation names a thing this
module uses it. Where it does not, this module REFUSES rather than guesses:

* The invoicing page names ``customerId``, ``invoiceNumber`` and ``status`` but
  no amount field, no due-date field and no currency field (claims row 45).
  :func:`mercury_invoice_to_record` therefore takes those three key names from
  the caller, and an invoice that does not carry them comes back as
  :class:`Unmapped`, never as a record built from a plausible-looking key.
* The signature scheme is HMAC-SHA256 over ``<timestamp>.<raw body>`` in a
  ``Mercury-Signature: t=<timestamp>,v1=<signature>`` header. The page does not
  state the encoding of ``v1``; hex is assumed and recorded as NEVER CHECKED
  (claims row 49), so a real delivery is the control that settles it.
* Mercury has no invoice webhook event. A payment is detected from
  ``transaction.created``, and that is only a HINT to look: it never marks an
  invoice paid, because Mercury matches on exact amount and a hint is not a
  match. The payload field list names no event-type key, so the caller supplies
  the fields that identify a created transaction (claims row 49).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from app.invoice_source import (
    InvoiceError,
    InvoiceRecord,
    make_customer_ref,
    parse_date,
    parse_minor,
)

#: Mercury's four published invoice statuses, mapped to the closed set. Kept as
#: data so a test asserts each has a decision. A status not in this table is
#: UNMAPPED, not "unknown": the caller learns the vocabulary changed.
PUBLISHED_MERCURY_STATUSES: dict[str, str] = {
    "Unpaid": "open",
    "Processing": "processing",
    "Paid": "paid",
    "Cancelled": "cancelled",
}

#: Keys the invoicing page states outright.
CONFIRMED_KEYS = {"invoice_no": "invoiceNumber", "customer": "customerId", "status": "status"}

#: Keys the page does not state. The caller must name them (from a real invoice
#: read with a Read Only token) before any invoice can be translated.
UNCONFIRMED = ("amount", "due", "currency")

DEFAULT_TOLERANCE_SECONDS = 300

_HEADER_RE = re.compile(r"^t=(\d{1,12}),v1=([0-9a-fA-F]{64})$")


@dataclass(frozen=True)
class Unmapped:
    """The invoice could not be translated. ``reason`` names a key, never a value."""

    reason: str


@dataclass(frozen=True)
class PaymentHint:
    """A transaction was created. Look for a matching invoice; this is not a match."""

    event_id: str
    resource_id: str


def verify_mercury_signature(
    secret: str,
    header: str,
    raw_body: bytes,
    *,
    now: int,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """True only for a well formed, fresh, correctly signed delivery.

    ``raw_body`` must be the bytes exactly as received: re-serialised JSON signs
    differently. ``now`` is passed in so the replay window is testable and no
    clock is read here. Every failure returns False; none raises, and none says
    which check failed, because a verifier that explains itself helps a forger.
    """
    if not secret or not isinstance(raw_body, (bytes, bytearray)) or not isinstance(header, str):
        return False
    m = _HEADER_RE.match(header.strip())
    if not m:
        return False
    timestamp, received = int(m.group(1)), m.group(2).lower()
    if abs(now - timestamp) > tolerance:
        return False
    signed = str(timestamp).encode("ascii") + b"." + bytes(raw_body)
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def mercury_event_to_hint(raw_body: bytes, *, match: dict[str, str]) -> PaymentHint | None:
    """A payment hint when the event carries every ``match`` key and value, else None.

    The documentation lists the event payload fields (``id``, ``resourceType``,
    ``resourceId``, ``operationType`` and others) but names no event-type key and
    no values for these, so which fields identify a created transaction is read
    from a real delivery and passed in. An empty ``match`` matches nothing: an
    empty filter that matched everything would turn every balance update into a
    payment hint. A body that is not a JSON object, or an event missing its
    ``id`` or ``resourceId``, returns None. The caller dedupes on ``event_id``
    because delivery is at-least-once.
    """
    if not match:
        return None
    try:
        event = json.loads(raw_body)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    if any(event.get(k) != v for k, v in match.items()):
        return None
    event_id, resource_id = event.get("id"), event.get("resourceId")
    if not isinstance(event_id, str) or not isinstance(resource_id, str):
        return None
    return PaymentHint(event_id=event_id, resource_id=resource_id)


def mercury_invoice_to_record(
    payload: object,
    *,
    keys: dict[str, str],
    salt: str,
) -> InvoiceRecord | Unmapped:
    """Translate one Mercury invoice object.

    ``keys`` supplies the three names the documentation does not state
    (``amount``, ``due``, ``currency``). Every failure is an :class:`Unmapped`
    whose reason names a key or a field, never a value.
    """
    if not isinstance(payload, dict):
        return Unmapped("payload: expected an object")
    missing = [k for k in UNCONFIRMED if not keys.get(k)]
    if missing:
        return Unmapped("keys: not supplied: " + ", ".join(missing))
    names = {**CONFIRMED_KEYS, **{k: keys[k] for k in UNCONFIRMED}}
    for logical, key in names.items():
        if logical == "due":
            continue
        if payload.get(key) in (None, ""):
            return Unmapped(f"invoice: key absent: {key}")
    raw_status = payload.get(names["status"])
    status = PUBLISHED_MERCURY_STATUSES.get(raw_status) if isinstance(raw_status, str) else None
    if status is None:
        return Unmapped("status: not one of the four published values")
    source_id = payload.get("id")
    try:
        currency = str(payload[names["currency"]]).strip().lower()
        return InvoiceRecord(
            source="mercury",
            source_id=source_id if isinstance(source_id, str) and source_id else str(payload[names["invoice_no"]]),
            invoice_no=str(payload[names["invoice_no"]]),
            customer_ref=make_customer_ref(str(payload[names["customer"]]), salt),
            amount_minor=parse_minor(payload[names["amount"]], currency),
            currency=currency,
            status=status,
            due=parse_date(payload.get(names["due"])),
        )
    except InvoiceError as exc:
        return Unmapped(str(exc))

