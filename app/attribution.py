# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
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

* the same invoice always lands in the same arm for a FIXED percentage and
  seed, even across redeliveries and restarts. Changing either reshuffles
  every future assignment, which is why the arm is read back from the
  ledger once recorded rather than recomputed (see app/loop.py),
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

from app.tenant_config import DEFAULT_HOLDOUT_SEED  # noqa: E402

DEFAULT_SEED = DEFAULT_HOLDOUT_SEED


def holdout_pct(config=None) -> int:
    """This tenant's holdout share, 0-100, parsed once in app/tenant_config.py."""
    from app.tenant_config import current_config

    return (config or current_config()).holdout_pct


def holdout_seed(config=None) -> str:
    """This tenant's holdout seed."""
    from app.tenant_config import current_config

    return (config or current_config()).holdout_seed


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
