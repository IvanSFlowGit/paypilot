# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Import an invoice export into :class:`InvoiceRecord` rows.

Every invoicing and accounting tool can export a CSV, and a CSV needs no
credential, no API key and no integration, so it is the smallest ask of a
client. The importer is deliberately plain:

* The caller supplies the column map. There are no presets, because a preset is
  a claim about another vendor's export that nobody here has measured.
* Each row is accepted or rejected on its own, with the line number and the
  field, never the cell value. A file that is mostly bad is still reported
  row by row.
* A whole-file assertion holds on every return: ``total == accepted + rejected``.
  A counter that can drift from its parts is how rows vanish unreported.
* A missing required column refuses the whole file. Silently importing nothing
  from a file whose header changed would read as a clean empty import.
* Size is capped before parsing, so a wrong file cannot exhaust memory.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from app.invoice_source import (
    InvoiceError,
    InvoiceRecord,
    make_customer_ref,
    parse_date,
    parse_minor,
)

#: Largest file accepted, in characters, and most data rows. Past either the
#: file is the wrong file or belongs in a pipeline, not a request.
MAX_CHARS = 5_000_000
MAX_ROWS = 100_000

#: The logical columns. ``REQUIRED`` must be mapped to a header; the rest are
#: used when mapped and defaulted otherwise.
REQUIRED = ("invoice_no", "customer", "amount", "due")
OPTIONAL = ("currency", "status", "issued", "paid_at", "source_id")


class ImportRefused(ValueError):
    """The whole file was refused. The message names columns, never cell values."""


@dataclass(frozen=True)
class Reject:
    line: int
    reason: str


@dataclass
class ImportResult:
    records: list[InvoiceRecord] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)
    total: int = 0

    def check(self) -> None:
        """Whole-file assertion: nothing was dropped between reading and reporting."""
        if self.total != len(self.records) + len(self.rejects):
            raise AssertionError("row count does not conserve")


def import_invoices(
    text: str,
    *,
    column_map: dict[str, str],
    salt: str,
    source: str = "csv",
    default_currency: str | None = None,
    status_map: dict[str, str] | None = None,
) -> ImportResult:
    """Parse ``text`` (the decoded file) into records.

    ``column_map`` maps logical names to header names, compared case-insensitively
    after trimming. ``status_map`` maps a source status string (lowercased) to a
    member of the closed status set; a status it does not name becomes
    ``unknown`` rather than being guessed.
    """
    if not isinstance(text, str):
        raise ImportRefused("file: expected decoded text")
    if len(text) > MAX_CHARS:
        raise ImportRefused("file: larger than the import cap")
    unknown_logical = set(column_map) - set(REQUIRED) - set(OPTIONAL)
    if unknown_logical:
        raise ImportRefused("column_map: names a logical column that does not exist")
    missing_map = [c for c in REQUIRED if c not in column_map]
    if missing_map:
        raise ImportRefused("column_map: required column not mapped: " + ", ".join(missing_map))
    if "currency" not in column_map and not default_currency:
        raise ImportRefused("currency: no column mapped and no default given")

    body = text[1:] if text.startswith("﻿") else text
    reader = csv.reader(io.StringIO(body, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportRefused("file: empty") from None
    index = {h.strip().lower(): i for i, h in enumerate(header)}
    cols: dict[str, int] = {}
    absent = []
    for logical, header_name in column_map.items():
        pos = index.get(header_name.strip().lower())
        if pos is None:
            absent.append(logical)
        else:
            cols[logical] = pos
    if absent:
        raise ImportRefused("header: mapped column not found for: " + ", ".join(sorted(absent)))

    smap = {k.strip().lower(): v for k, v in (status_map or {}).items()}
    result = ImportResult()
    seen: set[str] = set()
    start_line = 2
    for offset, row in enumerate(reader):
        if not any(cell.strip() for cell in row):
            continue
        result.total += 1
        line = start_line + offset
        if result.total > MAX_ROWS:
            raise ImportRefused("file: more rows than the import cap")
        try:
            record = _row_to_record(row, cols, source, salt, default_currency, smap)
            if record.invoice_no in seen:
                raise InvoiceError("invoice_no: duplicate within the file")
            seen.add(record.invoice_no)
            result.records.append(record)
        except InvoiceError as exc:
            result.rejects.append(Reject(line=line, reason=str(exc)))
    result.check()
    return result


def _cell(row: list[str], cols: dict[str, int], name: str) -> str:
    pos = cols.get(name)
    if pos is None or pos >= len(row):
        return ""
    return row[pos].strip()


def _row_to_record(
    row: list[str],
    cols: dict[str, int],
    source: str,
    salt: str,
    default_currency: str | None,
    smap: dict[str, str],
) -> InvoiceRecord:
    currency = (_cell(row, cols, "currency") or default_currency or "").lower()
    invoice_no = _cell(row, cols, "invoice_no")
    raw_status = _cell(row, cols, "status").lower()
    status = smap.get(raw_status, "unknown") if raw_status else "unknown"
    due: date | None = parse_date(_cell(row, cols, "due"))
    return InvoiceRecord(
        source=source,
        source_id=_cell(row, cols, "source_id") or invoice_no,
        invoice_no=invoice_no,
        customer_ref=make_customer_ref(_cell(row, cols, "customer"), salt),
        amount_minor=parse_minor(_cell(row, cols, "amount"), currency),
        currency=currency,
        status=status,
        issued=parse_date(_cell(row, cols, "issued")),
        due=due,
        paid_at=parse_date(_cell(row, cols, "paid_at")),
    )
