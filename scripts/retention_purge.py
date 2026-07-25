# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Retention purge: expire closed recovery records after a configurable window.

GDPR storage-limitation: PayPilot keeps a failed invoice's ledger row only as
long as the recovery flow needs it. Once an invoice is CLOSED (recovered,
churned, or exhausted) it is retained for a window and then deleted, along with
its child messages, transitions and idempotency events. Open invoices are never
purged - they are still being worked.

The window is ``PAYPILOT_RETENTION_MONTHS`` (default 12 months). Set it per
deployment; a shorter window is stricter minimization, a longer one may be
required by a client's own finance retention rules - so it is config, not a
constant. The run is recorded to the append-only audit log.

Usage:
    python -m scripts.retention_purge
    python -m scripts.retention_purge --months 6
    python -m scripts.retention_purge --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from app.audit import audit_security_event
from app.store import get_store

#: Env var and default for the retention window, in calendar months.
RETENTION_MONTHS_ENV = "PAYPILOT_RETENTION_MONTHS"
DEFAULT_RETENTION_MONTHS = 12


def retention_months(override: int | None = None) -> int:
    """The active retention window: an explicit override, then env, then default."""
    if override is not None:
        return int(override)
    raw = (os.getenv(RETENTION_MONTHS_ENV) or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_RETENTION_MONTHS


def _months_ago(now: datetime, months: int) -> datetime:
    """Calendar-month subtraction, clamped to the end of a shorter month."""
    total = (now.year * 12 + (now.month - 1)) - months
    year, month = divmod(total, 12)
    month += 1
    # Clamp day so e.g. 31 Jan minus 1 month lands on a valid February day.
    day = min(now.day, [31, 29 if year % 4 == 0 and (year % 100 or not year % 400) else 28,
                        31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return now.replace(year=year, month=month, day=day)


def cutoff_iso(months: int, now: datetime | None = None) -> str:
    """ISO instant that a terminal row's ``updated_at`` must predate to be purged."""
    now = now or datetime.now(UTC)
    return _months_ago(now, months).isoformat(timespec="seconds")


def purge(*, months: int | None = None, now: datetime | None = None, dry_run: bool = False,
          store=None) -> dict:
    """Delete closed records older than the retention window; return the counts.

    On ``dry_run`` nothing is deleted or audited; the returned counts describe
    what an actual run would remove.
    """
    store = store or get_store()
    window = retention_months(months)
    before = cutoff_iso(window, now)

    if dry_run:
        return {
            "dry_run": True,
            "retention_months": window,
            "cutoff": before,
            "failures_would_delete": len(store.expired_invoice_ids(before)),
        }

    deleted = store.purge_expired(before_iso=before)
    audit_security_event(
        event="retention_purge",
        detail=(
            f"purged {deleted['failures']} closed record(s) older than {window} "
            f"month(s) (cutoff {before})"
        ),
        severity="warning",
    )
    return {"dry_run": False, "retention_months": window, "cutoff": before, "deleted": deleted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge closed records past retention.")
    parser.add_argument("--months", type=int, help="override the retention window (months)")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be purged; change nothing"
    )
    args = parser.parse_args(argv)
    result = purge(months=args.months, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
