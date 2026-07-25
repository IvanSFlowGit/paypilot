# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""GDPR right of access: export everything PayPilot holds for one subject.

Given a ``--customer-id`` or an ``--email``, gather the subject's roster record
(name/email/plan, the only place their PII lives) and every ledger row keyed to
them (failures, messages, transitions, idempotency events), and print it as one
JSON document. Read-only: it never mutates the ledger or the roster.

The access request is itself recorded to the append-only audit log (hashed id,
never the raw value), because "who asked for what, when" is a control a reviewer
will want to see.

Usage:
    python -m scripts.gdpr_export --customer-id cust_001
    python -m scripts.gdpr_export --email billing@acmerobotics.io
    python -m scripts.gdpr_export --customer-id cust_001 --out export.json
"""

from __future__ import annotations

import argparse
import json
import sys

from app.audit import audit_security_event
from app.pii import hash_pii
from app.store import get_store
from scripts.gdpr_common import resolve_targets


def export_subject(
    *,
    customer_id: str | None = None,
    email: str | None = None,
    customers_path=None,
    store=None,
) -> dict:
    """Return every record held for a subject across the roster and the ledger."""
    store = store or get_store()
    targets = resolve_targets(
        customer_id=customer_id, email=email, path=customers_path
    )
    ledger = [store.export_customer(cid) for cid in targets["customer_ids"]]
    return {
        "request": {"customer_id": customer_id, "email": email},
        "resolved_customer_ids": targets["customer_ids"],
        "roster_records": targets["records"],
        "ledger": ledger,
    }


def _audit_access(customer_id: str | None, email: str | None) -> None:
    subject = customer_id or email or "unknown"
    audit_security_event(
        event="access_request",
        detail=f"gdpr subject access export for hash={hash_pii(subject)}",
        severity="warning",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GDPR subject-access export.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--customer-id", help="ledger customer id to export")
    group.add_argument("--email", help="subject email to resolve then export")
    parser.add_argument("--out", help="write JSON here instead of stdout")
    args = parser.parse_args(argv)

    result = export_subject(customer_id=args.customer_id, email=args.email)
    _audit_access(args.customer_id, args.email)

    payload = json.dumps(result, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print(f"wrote export for {len(result['resolved_customer_ids'])} customer id(s) to {args.out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
