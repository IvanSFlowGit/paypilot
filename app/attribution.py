"""Holdout assignment: the control group that keeps the numbers honest.

A dunning tool's headline claim is "we recovered X". The uncomfortable part is
that some of those invoices would have been paid anyway - Stripe retries failed
charges on its own, and plenty of customers fix their card unprompted. Without
a control group, every one of those self-healing invoices gets counted as our
win, and the number is unfalsifiable.

So a configurable share of failures is assigned to a holdout: recorded in full,
never messaged. The difference between the two arms is the part we can actually
claim.

Assignment is deterministic - a hash of the invoice id and a seed, not a random
draw. That matters for three reasons:

* the same invoice always lands in the same arm, even if the event is
  redelivered or the process restarts mid-run,
* no assignment state has to be stored or kept in sync, and
* the split is reproducible, so a disputed result can be recomputed from the
  invoice ids alone.

**Default is 0%.** Nobody is silently denied a recovery attempt because a
config value was left at a demo setting. Turning on a holdout means deciding to
withhold dunning from real paying customers, and that has to be a deliberate,
written act in the deployment config.
"""

from __future__ import annotations

import hashlib
import os

DEFAULT_SEED = "paypilot-holdout-v1"


def holdout_pct() -> int:
    """Percentage of failures to withhold dunning from, 0-100.

    Clamped rather than rejected: a typo that would otherwise withhold dunning
    from everyone should degrade to a sane bound, and the value is reported on
    the dashboard so a wrong setting is visible rather than silent.
    """
    raw = (os.getenv("PAYPILOT_HOLDOUT_PCT") or "0").strip()
    try:
        value = int(float(raw))
    except (ValueError, OverflowError):
        # OverflowError, not just ValueError: float("inf") parses fine and then
        # explodes on int(). Escaping here would 500 every failed-payment event
        # and stop all dunning, which is the opposite of a clamp.
        return 0
    return max(0, min(100, value))


def holdout_seed() -> str:
    """Seed for the assignment hash.

    Changing it reshuffles every future assignment, so it should stay fixed for
    the life of an experiment. It is deliberately not derived from anything
    time-based.
    """
    return (os.getenv("PAYPILOT_HOLDOUT_SEED") or DEFAULT_SEED).strip() or DEFAULT_SEED


def bucket_of(invoice_id: str, seed: str | None = None) -> int:
    """Stable bucket 0-99 for an invoice id.

    SHA-256 rather than Python's ``hash()``: the builtin is salted per process,
    so it would reassign every invoice on restart.
    """
    digest = hashlib.sha256(f"{seed or holdout_seed()}:{invoice_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def is_holdout(invoice_id: str, pct: int | None = None, seed: str | None = None) -> bool:
    """Whether this invoice is in the no-dunning control arm."""
    if not invoice_id:
        return False
    threshold = holdout_pct() if pct is None else max(0, min(100, pct))
    if threshold <= 0:
        return False
    return bucket_of(invoice_id, seed) < threshold
