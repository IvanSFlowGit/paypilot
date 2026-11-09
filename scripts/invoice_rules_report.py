#!/usr/bin/env python3
# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Label unpaid invoices with the five-state rules. Reads files, prints, sends and writes nothing.

    scripts/invoice_rules_report.py --csv /abs/path/export.csv \\
        --map invoice_no="Number" --map customer="Client" --map amount="Total" \\
        --map due="Due date" --map status="State" --map currency="Cur" \\
        --status unpaid=open --status paid=paid \\
        [--facts /abs/path/facts.csv]

The optional facts file is a CSV with the header ``invoice_no`` and any of
``chases_sent``, ``replies_received``, ``disputed``, ``promised_by``. An invoice
with no facts row is read as: no chases, replies untracked, not disputed, no
promise. An empty ``replies_received`` cell means untracked, never zero, because
``no_reply`` needs a recorded zero.

The customer salt comes from ``PAYPILOT_CUSTOMER_SALT`` and there is no default.
Exit codes are three states: 0 every row was accepted, 1 the report printed but
some rows or facts were rejected, 2 the file could not be read and NOTHING was
measured. A report from exit 2 does not exist.

This script does not touch the decision table and has no send or write path.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from invoice_age_report import SALT_ENV, _pairs, _read  # noqa: E402

from app.invoice_csv import ImportRefused, import_invoices  # noqa: E402
from app.invoice_rules import LABELS, InvoiceFacts, classify  # noqa: E402
from app.invoice_source import InvoiceError, parse_date  # noqa: E402

FACT_COLUMNS = ("chases_sent", "replies_received", "disputed", "promised_by")
_TRUE = {"1", "true", "yes", "y"}
_FALSE = {"0", "false", "no", "n", ""}


def _parse_facts(text: str) -> tuple[dict[str, InvoiceFacts], list[str], int]:
    """Return (facts by invoice_no, reject reasons by line, total data rows)."""
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        raise ValueError("facts: empty file") from None
    if "invoice_no" not in header:
        raise ValueError("facts: header must contain invoice_no")
    unknown = [h for h in header if h != "invoice_no" and h not in FACT_COLUMNS]
    if unknown:
        raise ValueError("facts: unknown column(s): " + ", ".join(unknown))
    pos = {h: i for i, h in enumerate(header)}
    facts: dict[str, InvoiceFacts] = {}
    rejects: list[str] = []
    total = 0

    def cell(row, name):
        i = pos.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    for offset, row in enumerate(reader):
        if not any(c.strip() for c in row):
            continue
        total += 1
        line = offset + 2
        try:
            inv_no = cell(row, "invoice_no")
            if not inv_no:
                raise ValueError("invoice_no: empty")
            if inv_no in facts:
                raise ValueError("invoice_no: duplicate facts row")
            chases = cell(row, "chases_sent")
            replies = cell(row, "replies_received")
            flag = cell(row, "disputed").lower()
            if flag not in _TRUE | _FALSE:
                raise ValueError("disputed: expected yes/no/1/0/true/false or empty")
            facts[inv_no] = InvoiceFacts(
                chases_sent=int(chases) if chases else 0,
                replies_received=int(replies) if replies else None,
                disputed=flag in _TRUE,
                promised_by=parse_date(cell(row, "promised_by")),
            )
        except (ValueError, InvoiceError) as exc:
            rejects.append(f"facts line {line}: {exc}")
    return facts, rejects, total


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--csv", required=True)
    ap.add_argument("--facts")
    ap.add_argument("--map", action="append", default=[])
    ap.add_argument("--status", action="append", default=[])
    ap.add_argument("--default-currency")
    ap.add_argument("--today")
    return ap


def main(argv: list[str] | None = None, *, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        salt = os.environ.get(SALT_ENV, "")
        if not salt:
            raise ValueError(f"salt: set {SALT_ENV}, there is no default")
        today = parse_date(args.today) if args.today else date.today()
        res = import_invoices(
            _read(args.csv),
            column_map=_pairs(args.map, "--map"),
            salt=salt,
            default_currency=args.default_currency,
            status_map=_pairs(args.status, "--status"),
        )
        facts, fact_rejects, fact_total = (
            _parse_facts(_read(args.facts)) if args.facts else ({}, [], 0)
        )
    except (ValueError, ImportRefused, InvoiceError, OSError) as exc:
        print(f"NOT RUN: {exc}", file=out)
        print("Nothing was measured.", file=out)
        return 2

    known = {r.invoice_no for r in res.records}
    orphans = sorted(set(facts) - known)
    if len(facts) + len(fact_rejects) != fact_total:
        raise AssertionError("facts row count does not conserve")

    labels: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    rows = []
    for rec in res.records:
        v = classify(rec, facts.get(rec.invoice_no, InvoiceFacts()), today)
        labels[v.label] += 1
        if v.label == "UNKNOWN":
            reasons[v.rule_id] += 1
        rows.append((rec.invoice_no, v.label, v.rule_id))
    if sum(labels.values()) != len(res.records):
        raise AssertionError("verdict count does not conserve")

    print(f"subject: {Path(args.csv).resolve()}", file=out)
    print(f"as of: {today.isoformat()}", file=out)
    print(f"rows: {res.total} read, {len(res.records)} accepted, {len(res.rejects)} rejected", file=out)
    if args.facts:
        used = len(facts) - len(orphans)
        print(
            f"facts: {fact_total} read, {used} matched, {len(orphans)} matched no invoice, "
            f"{len(fact_rejects)} rejected; {len(res.records) - used} invoice(s) had no facts row",
            file=out,
        )
    else:
        print("facts: none given; replies untracked, no chases, no disputes, no promises assumed", file=out)
    for label in LABELS:
        print(f"  {label:<20}{labels[label]:>6}", file=out)
    for reason, n in sorted(reasons.items()):
        print(f"    unknown because {reason}: {n}", file=out)
    for inv_no, label, rule in rows:
        print(f"  {inv_no:<24}{label:<20}{rule}", file=out)
    for rj in res.rejects[:20]:
        print(f"  reject line {rj.line}: {rj.reason}", file=out)
    for msg in fact_rejects[:20]:
        print(f"  reject {msg}", file=out)
    for inv_no in orphans[:20]:
        print(f"  facts row matches no invoice: {inv_no}", file=out)
    print("dry run: nothing was sent or written.", file=out)
    print("labels are a worklist for a person; the decision table is not touched.", file=out)
    return 0 if not res.rejects and not fact_rejects and not orphans else 1


if __name__ == "__main__":
    raise SystemExit(main())
