#!/usr/bin/env python3
# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Read a mailbox, write the facts file ``invoice_rules_report.py --facts`` reads. Sends and modifies nothing.

    scripts/mailbox_facts_export.py --profile /abs/profile.toml --folder INBOX --folder "Sent" \\
        --ours billing@you.example --out /abs/facts.csv [--days 90] [--today YYYY-MM-DD]

Then: ``scripts/invoice_rules_report.py --profile /abs/profile.toml --facts /abs/facts.csv``.

The invoices come from the profile's CSV, so the mailbox only ever adds facts about invoices already
known. IMAP settings are the same ones the draft approval uses (PAYPILOT_IMAP_* / tenant config); a
Proton Bridge or Gmail app-password mailbox is just a host and port.

THREE EXIT STATES. 0: every folder was read and the facts file is written. 1: the file is written but
the read was INCOMPLETE (a folder failed or the cap was hit), so ``replies_received`` is left EMPTY for
every invoice: an incomplete read cannot prove zero replies. 2: nothing was read and no file exists.
Prints counts only, never an address, a name or message text. ``disputed`` is never written: only a
person or a reviewed model proposal sets it.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from invoice_age_report import SALT_ENV, _read  # noqa: E402
from invoice_rules_report import _load_profile  # noqa: E402

from app.invoice_csv import ImportRefused, import_invoices  # noqa: E402
from app.invoice_source import InvoiceError  # noqa: E402
from app.mailbox_facts import build_facts  # noqa: E402
from app.mailbox_imap import connect_from_config, read_messages  # noqa: E402


def main(argv: list[str], out=sys.stdout, connect=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", required=True)
    ap.add_argument("--folder", action="append", required=True)
    ap.add_argument("--ours", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--today")
    args = ap.parse_args(argv)
    try:
        if not Path(args.out).is_absolute():
            raise ValueError("out: give an absolute path")
        salt = os.environ.get(SALT_ENV, "")
        prof = _load_profile(args.profile)
        records = import_invoices(
            _read(prof["csv"]),
            column_map=prof.get("map", {}),
            salt=salt,
            default_currency=prof.get("default_currency"),
            status_map=prof.get("status", {}),
        ).records
        if not records:
            raise ValueError("invoices: none accepted, nothing to attach facts to")
        today = date.fromisoformat(args.today) if args.today else date.today()
        read = read_messages(
            connect or connect_from_config(), tuple(args.folder), today - timedelta(days=args.days)
        )
    except (ValueError, ImportRefused, InvoiceError, OSError, KeyError) as exc:
        print(f"nothing measured: {exc}", file=out)
        return 2
    if not read.folders_ok:
        print("nothing measured: no folder could be read", file=out)
        return 2
    facts, dropped = build_facts(read.messages, records, set(args.ours), today)
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["invoice_no", "chases_sent", "replies_received", "promised_by"])
    for no in sorted(facts):
        f = facts[no].facts
        replies = "" if not read.complete or f.replies_received is None else f.replies_received
        w.writerow([no, f.chases_sent, replies, f.promised_by.isoformat() if f.promised_by else ""])
    Path(args.out).write_text(buf.getvalue(), encoding="utf-8")
    print(
        f"messages read {len(read.messages)}, matched an invoice {len(read.messages) - dropped}, dropped {dropped}; "
        f"folders ok {len(read.folders_ok)} failed {len(read.folders_failed)}; invoices {len(facts)}; "
        f"read {'COMPLETE' if read.complete else 'INCOMPLETE, replies left untracked'}",
        file=out,
    )
    return 0 if read.complete else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
