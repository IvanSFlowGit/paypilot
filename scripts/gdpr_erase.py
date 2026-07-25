# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""GDPR right to erasure: delete everything PayPilot holds for one subject.

Given a ``--customer-id`` or an ``--email``, delete the subject end to end:

* every ledger row keyed to them - failures, and their child messages,
  transitions and idempotency events (``app.store.erase_customer``);
* their record in the operator roster ``data/customers.json``, the one place
  their name/email lives.

The erasure is recorded to the append-only audit log (hashed id, never the raw
value) BEFORE the delete, so the request survives even though the subject's data
does not - that record is what a reviewer or the subject's own follow-up relies
on. ``--dry-run`` reports what would be removed and changes nothing.

Usage:
    python -m scripts.gdpr_erase --customer-id cust_001
    python -m scripts.gdpr_erase --email billing@acmerobotics.io
    python -m scripts.gdpr_erase --customer-id cust_001 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys

from app.audit import audit_security_event
from app.pii import hash_pii
from app.store import get_store
from scripts.gdpr_common import redact_customers_file, resolve_targets


def erase_subject(
    *,
    customer_id: str | None = None,
    email: str | None = None,
    customers_path=None,
    store=None,
    dry_run: bool = False,
) -> dict:
    """Erase a subject across the ledger and the roster; return what was removed.

    On ``dry_run`` nothing is deleted or audited; the returned counts describe
    what an actual run would remove (the ledger rows currently held; the roster
    removal count is reported as the number of matching records).
    """
    store = store or get_store()
    targets = resolve_targets(
        customer_id=customer_id, email=email, path=customers_path
    )
    ids = targets["customer_ids"]

    if dry_run:
        would = {"failures": 0, "messages": 0, "transitions": 0, "events": 0}
        for cid in ids:
            held = store.export_customer(cid)
            would["failures"] += len(held["failures"])
            would["messages"] += len(held["messages"])
            would["transitions"] += len(held["transitions"])
            would["events"] += len(held["events"])
        return {
            "dry_run": True,
            "resolved_customer_ids": ids,
            "ledger_would_delete": would,
            "roster_would_remove": len(targets["records"]),
        }

    # Record the request first: the audit trail of the erasure must outlive the
    # data it erases, and must never itself carry the raw identifier.
    subject = customer_id or email or "unknown"
    audit_security_event(
        event="erasure_request",
        detail=f"gdpr erasure for hash={hash_pii(subject)} across {len(ids)} customer id(s)",
        severity="warning",
    )

    ledger_deleted = {"failures": 0, "messages": 0, "transitions": 0, "events": 0}
    for cid in ids:
        counts = store.erase_customer(cid)
        for k in ledger_deleted:
            ledger_deleted[k] += counts[k]

    roster_removed = redact_customers_file(
        customer_ids=set(ids),
        emails=set(targets["emails"]),
        path=customers_path,
    )
    return {
        "dry_run": False,
        "resolved_customer_ids": ids,
        "ledger_deleted": ledger_deleted,
        "roster_removed": roster_removed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GDPR subject erasure.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--customer-id", help="ledger customer id to erase")
    group.add_argument("--email", help="subject email to resolve then erase")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be removed; change nothing"
    )
    args = parser.parse_args(argv)

    result = erase_subject(
        customer_id=args.customer_id, email=args.email, dry_run=args.dry_run
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
