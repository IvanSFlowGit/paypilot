#!/usr/bin/env python3
# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Age a set of invoices. Reads a file, prints a report, sends and writes nothing.

    scripts/invoice_age_report.py --csv /abs/path/export.csv \\
        --map invoice_no="Number" --map customer="Client" --map amount="Total" \\
        --map due="Due date" --map status="State" --map currency="Cur" \\
        --status unpaid=open --status paid=paid

    scripts/invoice_age_report.py --mercury-json /abs/path/invoices.json \\
        --key amount=<name> --key due=<name> --key currency=<name> [--items-key invoices]

The customer salt comes from ``PAYPILOT_CUSTOMER_SALT`` and there is no default.
Exit codes are three states, never two: 0 every row was accepted, 1 the report
printed but some rows were rejected or unmapped, 2 the file could not be read
and NOTHING was measured. A report from exit 2 does not exist.

This script does not touch the decision table. It prints, last, that no
strategy rule maps an unpaid invoice, so a reader never mistakes an ageing
count for a chasing plan.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.invoice_csv import ImportRefused, import_invoices  # noqa: E402
from app.invoice_source import BUCKETS, InvoiceError, bucket, parse_date  # noqa: E402
from app.mercury_map import Unmapped, mercury_invoice_to_record  # noqa: E402
from app.money import exponent  # noqa: E402

SALT_ENV = "PAYPILOT_CUSTOMER_SALT"
MAX_BYTES = 5_000_000


def _pairs(items: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"{what}: expected name=value")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _fmt(minor: int, currency: str) -> str:
    places = exponent(currency)
    if places == 0:
        return f"{minor} {currency.upper()}"
    whole, frac = divmod(minor, 10**places)
    return f"{whole}.{frac:0{places}d} {currency.upper()}"


def _read(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("path: give an absolute path")
    if not p.is_file():
        raise ValueError("path: not a file")
    if p.stat().st_size > MAX_BYTES:
        raise ValueError("path: larger than the cap")
    return p.read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--mercury-json")
    ap.add_argument("--map", action="append", default=[])
    ap.add_argument("--status", action="append", default=[])
    ap.add_argument("--key", action="append", default=[])
    ap.add_argument("--items-key")
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
        subject = args.csv or args.mercury_json
        text = _read(subject)
        records, rejects, total, unmapped = [], [], 0, 0
        if args.csv:
            res = import_invoices(
                text,
                column_map=_pairs(args.map, "--map"),
                salt=salt,
                default_currency=args.default_currency,
                status_map=_pairs(args.status, "--status"),
            )
            records, rejects, total = res.records, res.rejects, res.total
        else:
            data = json.loads(text)
            items = data.get(args.items_key) if args.items_key else data
            if not isinstance(items, list):
                raise ValueError("mercury-json: not the expected array shape")
            keys = _pairs(args.key, "--key")
            total = len(items)
            for payload in items:
                r = mercury_invoice_to_record(payload, keys=keys, salt=salt)
                if isinstance(r, Unmapped):
                    unmapped += 1
                else:
                    records.append(r)
            if len(records) + unmapped != total:
                raise AssertionError("row count does not conserve")
    except (ValueError, ImportRefused, InvoiceError, OSError) as exc:
        print(f"NOT RUN: {exc}", file=out)
        print("Nothing was measured.", file=out)
        return 2

    counts: dict[str, int] = {b: 0 for b in BUCKETS}
    sums: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for rec in records:
        b = bucket(rec, today)
        counts[b] += 1
        sums[b][rec.currency] += rec.amount_minor

    print(f"subject: {Path(subject).resolve()}", file=out)
    print(f"as of: {today.isoformat()}", file=out)
    print(f"rows: {total} read, {len(records)} accepted, {len(rejects)} rejected, {unmapped} unmapped", file=out)
    for b in BUCKETS:
        money = ", ".join(_fmt(sums[b][c], c) for c in sorted(sums[b])) or "-"
        print(f"  {b:<16}{counts[b]:>6}   {money}", file=out)
    for rj in rejects[:20]:
        print(f"  reject line {rj.line}: {rj.reason}", file=out)
    if len(rejects) > 20:
        print(f"  ... {len(rejects) - 20} more rejects not shown", file=out)
    print("dry run: nothing was sent or written.", file=out)
    print("no strategy rule maps an unpaid invoice yet; this is an ageing count, not a chasing plan.", file=out)
    return 0 if not rejects and not unmapped else 1


if __name__ == "__main__":
    raise SystemExit(main())
