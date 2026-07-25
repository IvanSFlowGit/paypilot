# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Shared helpers for the GDPR export and erasure scripts.

The recovery ledger (``app.store``) is keyed on ``customer_id`` and holds no
name or email by design (data minimization). The one place a subject's name and
email actually live is the operator's ``data/customers.json`` roster, which is
also how an ``--email`` request is resolved to the ledger ``customer_id`` it is
keyed under. These helpers do that resolution, and, for erasure, remove the
subject's record from the roster - so "delete all their data" covers the PII
file as well as the ledger.

Kept dependency-light so both scripts share exactly one definition of "which
records match this subject", rather than each guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.nodes import CUSTOMERS_PATH  # the single roster path constant

#: Roster keys that are PII and, when present, are part of a subject's data.
PII_ROSTER_KEYS = ("name", "email", "plan", "mrr", "currency", "payment_history")


def _customers_path(path: str | Path | None) -> Path:
    return Path(path) if path else CUSTOMERS_PATH


def load_raw_customers(path: str | Path | None = None) -> list[dict]:
    """Read the roster verbatim, tolerating a missing or malformed file.

    Deliberately NOT ``app.nodes.load_customer_records`` (which drops fields on
    read): erasure writes the roster back, so it must preserve every non-target
    record byte-for-byte in structure. A broken roster yields an empty list, so
    a request against it neither crashes nor pretends to have erased something.
    """
    p = _customers_path(path)
    try:
        records = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return []
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def resolve_targets(
    *,
    customer_id: str | None = None,
    email: str | None = None,
    path: str | Path | None = None,
) -> dict:
    """Resolve a request to the set of ledger customer ids and roster records.

    An ``--email`` request has no direct ledger key, so it is matched against the
    roster (case-insensitively) to recover the ``customer_id`` the ledger uses. A
    ``--customer-id`` request also pulls the roster record so its PII can be
    exported or erased. Returns ``{"customer_ids", "emails", "records"}``.
    """
    ids: set[str] = set()
    emails: set[str] = set()
    records: list[dict] = []
    roster = load_raw_customers(path)

    email_lc = email.strip().lower() if email else None
    if customer_id:
        ids.add(customer_id)
    for record in roster:
        rid = record.get("id")
        remail = str(record.get("email") or "").strip().lower()
        if (customer_id and rid == customer_id) or (email_lc and remail == email_lc):
            records.append(record)
            if rid:
                ids.add(rid)
            if remail:
                emails.add(remail)
    if email_lc:
        emails.add(email_lc)
    return {"customer_ids": sorted(ids), "emails": sorted(emails), "records": records}


def redact_customers_file(
    *,
    customer_ids: set[str] | list[str],
    emails: set[str] | list[str],
    path: str | Path | None = None,
) -> int:
    """Remove every roster record matching a target id or email; return the count.

    Rewrites the file only when something was removed, so a no-op request does
    not churn the file. Non-target records are written back unchanged.
    """
    p = _customers_path(path)
    id_set = {str(i) for i in customer_ids}
    email_set = {str(e).strip().lower() for e in emails}
    roster = load_raw_customers(path)
    kept, removed = [], 0
    for record in roster:
        rid = str(record.get("id") or "")
        remail = str(record.get("email") or "").strip().lower()
        if rid in id_set or (remail and remail in email_set):
            removed += 1
            continue
        kept.append(record)
    if removed:
        p.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    return removed
