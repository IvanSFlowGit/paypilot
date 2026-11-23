# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Translate one invoice object from Xero, QuickBooks Online or Sage into an InvoiceRecord.

Pure and offline: parsed JSON in, a record or :class:`Unmapped` out. No network, no clock,
no credential, no write. Field names and status values come from each vendor's own pages as
read on 2026-11-23 (``docs/claims.md``). Where a page did not state a thing, this module takes
it from the caller and REFUSES without it, as ``mercury_map`` does, rather than guessing:

* Xero: ``InvoiceNumber``, ``Type``, ``Status``, ``Date``, ``DueDate``, ``Total``,
  ``CurrencyCode``, ``AmountDue``, ``Contact`` are on the Invoices page. Statuses read there:
  DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED, DELETED. Only an ACCREC (sales) invoice is a
  debt owed to the client; ACCPAY is a bill they owe, and is refused.
* QuickBooks: ``DocNumber``, ``TxnDate``, ``DueDate``, ``TotalAmt``, ``Balance``,
  ``CustomerRef``, ``CurrencyRef`` are on the Invoice page. THERE IS NO STATUS FIELD. Paid
  is ``Balance`` of zero; a void zeroes ``TotalAmt`` and writes ``Voided`` into ``PrivateNote``.
  ``CurrencyRef`` is present only for a multicurrency company, so a home-currency invoice
  needs a caller-supplied default.
* Sage: ``reference``, ``date``, ``outstanding_amount``, ``currency``, ``status`` (an object
  with ``id``), ``void_reason`` and ``contact`` are on the sales invoice page. THE PAGE READ
  DID NOT STATE the invoice-number key, the due-date key, the total key, or the status ids, so
  the caller names them and supplies the status table.

Numbers: vendor JSON carries amounts as JSON numbers, and a float cannot hold 0.07. Parse with
:func:`loads_exact`, which turns every number into a decimal string, and this module refuses
a float or bare integer outright.

Not handled, stated rather than implied: partial payment is read as ``open`` with the invoice
total as the amount, so the bank-feed amount-only count in ``in_flight`` will not see a payment
equal to the remainder. A status or shape not taught here is Unmapped, never guessed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from app.invoice_source import (
    InvoiceError,
    InvoiceRecord,
    make_customer_ref,
    parse_date,
    parse_minor,
)

XERO = "xero"
QBO = "qbo"
SAGE = "sage"

#: Xero's six statuses as read on the Invoices page, each mapped to the closed set or to None,
#: which means NOT AN ISSUED DEBT and is Unmapped with a reason (a draft is not chased).
XERO_STATUSES: dict[str, str | None] = {
    "DRAFT": None,
    "SUBMITTED": None,
    "AUTHORISED": "open",
    "PAID": "paid",
    "VOIDED": "cancelled",
    "DELETED": "cancelled",
}

_ISO_DT = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00:00(\.0+)?$")
_SLASH = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")


@dataclass(frozen=True)
class Unmapped:
    """The invoice could not be translated. ``reason`` names a key or a rule, never a value."""

    reason: str


def loads_exact(body: bytes | str) -> object:
    """``json.loads`` with every number kept as the decimal string the vendor sent."""
    return json.loads(body, parse_float=str, parse_int=str)


def _date(value: object, *, alt: object = None):
    """ISO date, a midnight ISO datetime, or the slash form QuickBooks documents. Else refused."""
    for raw in (alt, value):
        if isinstance(raw, str):
            m = _ISO_DT.match(raw.strip())
            if m:
                return parse_date(m.group(1))
            s = _SLASH.match(raw.strip())
            if s:
                return parse_date("-".join(s.groups()))
    if isinstance(value, str) and value.startswith("/Date("):
        raise InvoiceError("date: Microsoft /Date()/ form is not read, send ISO dates")
    return parse_date(value)


def _cur(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("id") or value.get("value")
    cur = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z]{3}", cur):
        raise InvoiceError("currency: expected a three letter code")
    return cur


def _need(payload: dict, key: str, label: str | None = None) -> object:
    v = payload.get(key)
    if v in (None, ""):
        raise InvoiceError(f"invoice: key absent: {label or key}")
    return v


def map_xero_invoice(payload: object, *, salt: str) -> InvoiceRecord | Unmapped:
    if not isinstance(payload, dict):
        return Unmapped("payload: expected an object")
    try:
        if payload.get("Type") != "ACCREC":
            return Unmapped("type: not ACCREC, a bill the client owes is not a debt to chase")
        raw = payload.get("Status")
        if not isinstance(raw, str) or raw not in XERO_STATUSES:
            return Unmapped("status: not one of the six published values")
        status = XERO_STATUSES[raw]
        if status is None:
            return Unmapped(f"status: {raw} is not an issued invoice")
        contact = payload.get("Contact")
        if not isinstance(contact, dict) or not contact.get("ContactID"):
            raise InvoiceError("invoice: key absent: Contact.ContactID")
        cur = _cur(_need(payload, "CurrencyCode"))
        if status == "open":
            due = payload.get("AmountDue")
            if due is None:
                raise InvoiceError("invoice: key absent: AmountDue")
            # AUTHORISED with nothing due contradicts itself: refuse, never read as open.
            if isinstance(due, str) and re.fullmatch(r"0+(\.0+)?", due.strip()):
                return Unmapped("status: AUTHORISED but AmountDue is zero")
        sid = payload.get("InvoiceID")
        return InvoiceRecord(
            source=XERO,
            source_id=sid if isinstance(sid, str) and sid else str(_need(payload, "InvoiceNumber")),
            invoice_no=str(_need(payload, "InvoiceNumber")),
            customer_ref=make_customer_ref(str(contact["ContactID"]), salt),
            amount_minor=parse_minor(_need(payload, "Total"), cur),
            currency=cur,
            status=status,
            issued=_date(payload.get("Date"), alt=payload.get("DateString")),
            due=_date(payload.get("DueDate"), alt=payload.get("DueDateString")),
        )
    except InvoiceError as exc:
        return Unmapped(str(exc))


def map_qbo_invoice(
    payload: object, *, salt: str, default_currency: str | None
) -> InvoiceRecord | Unmapped:
    if not isinstance(payload, dict):
        return Unmapped("payload: expected an object")
    try:
        total = _need(payload, "TotalAmt")
        balance = payload.get("Balance")
        if balance is None:
            raise InvoiceError("invoice: key absent: Balance")
        note = payload.get("PrivateNote")
        zero = re.compile(r"0+(\.0+)?")
        if isinstance(total, str) and zero.fullmatch(total.strip()):
            if isinstance(note, str) and note.lstrip().lower().startswith("voided"):
                return Unmapped("status: voided, TotalAmt is zero")
            return Unmapped("amount: TotalAmt is zero and the invoice is not marked Voided")
        if not isinstance(balance, str):
            raise InvoiceError("Balance: expected a decimal string, parse with loads_exact")
        if zero.fullmatch(balance.strip()):
            status = "paid"
        else:
            num = re.compile(r"[0-9]+(\.[0-9]+)?")
            if not (isinstance(total, str) and num.fullmatch(total.strip()) and num.fullmatch(balance.strip())):
                raise InvoiceError("amount: expected decimal strings for TotalAmt and Balance")
            if Decimal(balance.strip()) > Decimal(total.strip()):
                return Unmapped("amount: Balance exceeds TotalAmt")
            status = "open"
        ref = payload.get("CurrencyRef")
        cur = _cur(ref if ref else default_currency)
        cust = payload.get("CustomerRef")
        if not isinstance(cust, dict) or not cust.get("value"):
            raise InvoiceError("invoice: key absent: CustomerRef.value")
        doc = payload.get("DocNumber")
        if doc in (None, ""):
            return Unmapped("invoice: key absent: DocNumber, nothing to match a bank line on")
        return InvoiceRecord(
            source=QBO,
            source_id=str(_need(payload, "Id")),
            invoice_no=str(doc),
            customer_ref=make_customer_ref(str(cust["value"]), salt),
            amount_minor=parse_minor(total, cur),
            currency=cur,
            status=status,
            issued=_date(payload.get("TxnDate")),
            due=_date(payload.get("DueDate")),
        )
    except InvoiceError as exc:
        return Unmapped(str(exc))


def map_sage_invoice(
    payload: object, *, salt: str, keys: dict[str, str], status_map: dict[str, str]
) -> InvoiceRecord | Unmapped:
    """``keys`` names ``invoice_no``, ``due`` and ``amount`` (unread on the page); ``status_map`` is
    Sage status id to one of open, processing, paid, cancelled."""
    if not isinstance(payload, dict):
        return Unmapped("payload: expected an object")
    missing = [k for k in ("invoice_no", "due", "amount") if not keys.get(k)]
    if missing:
        return Unmapped("keys: not supplied: " + ", ".join(missing))
    if not status_map:
        return Unmapped("status_map: not supplied, Sage status ids were not read")
    try:
        if payload.get("void_reason"):
            return Unmapped("status: voided, void_reason is set")
        st = payload.get("status")
        sid = st.get("id") if isinstance(st, dict) else None
        if not isinstance(sid, str) or sid not in status_map:
            return Unmapped("status: id not in the supplied status_map")
        status = status_map[sid]
        contact = payload.get("contact")
        cid = contact.get("id") if isinstance(contact, dict) else None
        if not cid:
            raise InvoiceError("invoice: key absent: contact.id")
        cur = _cur(_need(payload, "currency"))
        return InvoiceRecord(
            source=SAGE,
            source_id=str(payload.get("id") or _need(payload, keys["invoice_no"])),
            invoice_no=str(_need(payload, keys["invoice_no"])),
            customer_ref=make_customer_ref(str(cid), salt),
            amount_minor=parse_minor(_need(payload, keys["amount"]), cur),
            currency=cur,
            status=status,
            issued=_date(payload.get("date")),
            due=_date(payload.get(keys["due"])),
        )
    except InvoiceError as exc:
        return Unmapped(str(exc))


def map_many(items: object, mapper) -> tuple[list[InvoiceRecord], list[str]]:
    """Map a list, never dropping a row: ``accepted + rejected == total`` is asserted."""
    if not isinstance(items, list):
        raise InvoiceError("items: expected a list of invoice objects")
    ok: list[InvoiceRecord] = []
    bad: list[str] = []
    for i, it in enumerate(items):
        r = mapper(it)
        if isinstance(r, Unmapped):
            bad.append(f"item {i}: {r.reason}")
        else:
            ok.append(r)
    if len(ok) + len(bad) != len(items):
        raise AssertionError("row count does not conserve")
    return ok, bad
