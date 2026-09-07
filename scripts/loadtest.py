# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Load the recovery endpoint and check the agent is still legible afterwards.

Two questions, measured together, because the second one is the interesting one:

1. **Does it hold up.** Throughput, p50/p95/p99, error rate, and where the knee
   in the curve is as concurrency climbs.
2. **Can you still see what the agent did.** After every step this reads the
   ledger back and asks whether the record is intact: does every 200 have a
   ``failures`` row, does every failure row carry at least one state transition,
   and did anything land in a state nothing accounts for.

A perf tool answers the first. Nobody instruments the second, and it is the one
that matters for an agent, because a system that stays fast while quietly losing
its audit trail has failed in the way that costs you an argument with an auditor
rather than an argument with a user.

Deliberately NOT measured here: the model provider. With no ``OPENAI_API_KEY``
the drafting node takes the template path, so these numbers are PayPilot's own
machinery and say nothing about OpenAI's latency. That is the point - a real key
would put a network call in the hot loop that dominates everything else and
masks the thing we are looking for. State it in any write-up.

Usage::

    # terminal 1 - the app under test, with the limiter raised out of the way
    RATE_LIMIT_MAX=100000 RATE_LIMIT_GLOBAL_MAX=100000 \
      PAYPILOT_DB_PATH=data/loadtest.db \
      .venv/bin/uvicorn app.api:app --port 8000

    # terminal 2
    PAYPILOT_DB_PATH=data/loadtest.db .venv/bin/python scripts/loadtest.py

Every number it prints carries the command and the config that produced it, in
the JSON it writes out, so a figure lifted from here cannot go stale silently.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402

import httpx  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8000/webhooks/stripe"
DEFAULT_LADDER = "1,2,4,8,16,32,64"
DEFAULT_REQUESTS = 200

# Cycled deterministically, never drawn at random: two runs of the same ladder
# send byte-identical request sequences, so a difference between runs is a
# difference in the system and not in the input.
CUSTOMER_IDS = ["cust_001", "cust_002", "cust_003", "cust_004", "cust_005", "cust_006"]
DECLINE_CODES = ["expired_card", "insufficient_funds", "generic_decline"]

LEDGER_TABLES = ("failures", "events", "messages", "transitions")

# Why /webhooks/stripe and not /payment-failed. The demo route runs the graph and
# returns a draft; it never touches the ledger. The Stripe route is the one that
# writes - record_failure, a state transition, an event row - so it is both the
# money path and the only path where the audit-trail question can be asked at
# all. It also skips the shared rate limiter once a signature verifies, so the
# curve measures the system rather than the limiter.


def build_event(i: int) -> dict[str, Any]:
    """The i-th Stripe event. Pure function of i, so the sequence is replayable.

    The event id and the invoice id both vary with ``i`` on purpose. The route
    replays a cached result for a repeated event id, and the ledger keys state on
    the invoice id, so reusing either would measure the idempotency cache and
    report it as throughput. Shape mirrors ``tests/test_stripe.py::_stripe_event``
    rather than being invented here.
    """
    return {
        "id": f"evt_load_{i}",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "object": "invoice",
                "id": f"in_load_{i}",
                "customer": "cus_LOAD",
                "subscription": "sub_LOAD",
                "amount_due": 10000 + (i % 50) * 100,
                "currency": "usd",
                "attempt_count": (i % 3) + 1,
                "payment_intent": {
                    "last_payment_error": {"decline_code": DECLINE_CODES[i % len(DECLINE_CODES)]}
                },
                "metadata": {"paypilot_customer_id": CUSTOMER_IDS[i % len(CUSTOMER_IDS)]},
            }
        },
    }


def encode(event: dict[str, Any]) -> bytes:
    """Serialise exactly once, so the bytes we sign are the bytes we send.

    Signing a re-serialised copy is the classic way to produce a valid-looking
    request the server rejects: any difference in separators or key order and
    the HMAC is over different bytes than the body.
    """
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")


def stripe_signature(body: bytes, secret: str, timestamp: int) -> str:
    """Stripe's scheme: HMAC-SHA256 over ``{t}.{payload}``, sent as ``t=..,v1=..``.

    Built here rather than imported because the app only has the VERIFIER. The
    test signs with this and checks the app's own ``verify_stripe_signature``
    accepts it, which is the only assertion that proves the two agree.
    """
    signed = f"{timestamp}.".encode() + body
    v1 = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={v1}"


def headers_for(body: bytes, secret: str | None, timestamp: int | None = None) -> dict[str, str]:
    h = {"content-type": "application/json"}
    if secret:
        # The verifier enforces a 300s freshness window, so the timestamp is
        # taken per request rather than once at startup: a long ladder would
        # otherwise start failing partway through and look like a system fault.
        h["stripe-signature"] = stripe_signature(body, secret, timestamp or int(time.time()))
    return h


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile: the ceil(pct/100 * N)-th smallest value.

    Sorted explicitly rather than trusting arrival order, because arrival order
    under concurrency is exactly the thing that is not stable.

    Two things here are deliberate, and the first version of this function got
    both wrong. ``round(x + 0.5)`` is not ceiling - Python rounds halves to even,
    so p50 of 1..10 came back 6 instead of 5. And the multiplication is written
    ``pct * n / 100`` rather than ``pct / 100 * n`` because the second order
    turns 90th-of-10 into 9.000000000000002, which ceilings to 10 and quietly
    reports the wrong percentile. The epsilon absorbs the same class of error at
    other lengths. Percentiles have no obvious sanity check, which is why this is
    pinned by a test over 1..10 that a human can verify by counting.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    n = len(ordered)
    rank = math.ceil(pct * n / 100.0 - 1e-9)
    k = max(0, min(n - 1, rank - 1))
    return ordered[k]


def ledger_snapshot(db_path: str) -> dict[str, Any]:
    """Row counts plus the integrity questions, read straight off the ledger.

    Returns ``{"unavailable": reason}`` rather than raising or returning zeros:
    a missing database has to be distinguishable from an empty one, or a broken
    probe reads as a clean result.
    """
    if not Path(db_path).exists():
        return {"unavailable": f"no database at {db_path}"}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        present = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        snap: dict[str, Any] = {"tables_present": sorted(present)}
        for table in LEDGER_TABLES:
            snap[table] = (
                con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if table in present
                else None
            )
        # REPORTED, NOT ASSERTED, and the difference matters. The first version
        # of this probe treated "a failure with no transition row" as a lost
        # audit trail. Reading store.record_failure settles it: transitions are
        # written on state CHANGE only, and the opening state is not a change, so
        # a freshly ingested failure legitimately has none. Measuring the trivial
        # case at concurrency 1 is what caught it - the probe reported 5 orphans
        # out of 5 and every one was correct behaviour. A number nobody can
        # explain is a coin flip with a green tick on it: watch it, derive the
        # mechanism, then assert. It stays here because it should MOVE once
        # recovery and churn events start closing invoices.
        if {"failures", "transitions"} <= present:
            snap["failures_without_transition_REPORTED"] = con.execute(
                "SELECT count(*) FROM failures f "
                "WHERE NOT EXISTS (SELECT 1 FROM transitions t WHERE t.invoice_id = f.invoice_id)"
            ).fetchone()[0]
        # These ARE assertions, because the mechanism is derived: record_failure
        # writes state and failure_code on every insert, so either being empty
        # means a decision was recorded that cannot say what it was or why.
        if "failures" in present:
            cols = {r[1] for r in con.execute("PRAGMA table_info(failures)")}
            if "state" in cols:
                snap["failures_without_state"] = con.execute(
                    "SELECT count(*) FROM failures WHERE state IS NULL OR state = ''"
                ).fetchone()[0]
            if "failure_code" in cols:
                snap["failures_without_reason"] = con.execute(
                    "SELECT count(*) FROM failures WHERE failure_code IS NULL OR failure_code = ''"
                ).fetchone()[0]
        if {"failures", "messages"} <= present:
            snap["messages_without_failure"] = con.execute(
                "SELECT count(*) FROM messages m "
                "WHERE NOT EXISTS (SELECT 1 FROM failures f WHERE f.invoice_id = m.invoice_id)"
            ).fetchone()[0]
        return snap
    finally:
        con.close()


async def fire(
    client: httpx.AsyncClient, url: str, body: bytes, secret: str | None
) -> tuple[int, float]:
    started = time.perf_counter()
    try:
        r = await client.post(url, content=body, headers=headers_for(body, secret))
        return r.status_code, (time.perf_counter() - started) * 1000.0
    except Exception:
        # A transport failure is a result, not an exception to swallow: it gets
        # its own status so the error rate counts it instead of losing it.
        return 0, (time.perf_counter() - started) * 1000.0


async def run_step(
    url: str, secret: str | None, concurrency: int, n: int, offset: int, timeout: float
) -> dict[str, Any]:
    sem = asyncio.Semaphore(concurrency)
    results: list[tuple[int, float]] = []

    async def one(i: int) -> None:
        async with sem:
            results.append(await fire(client, url, encode(build_event(i)), secret))

    limits = httpx.Limits(max_connections=concurrency + 8, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        wall_start = time.perf_counter()
        await asyncio.gather(*(one(offset + i) for i in range(n)))
        wall = time.perf_counter() - wall_start

    codes: dict[int, int] = {}
    for code, _ in results:
        codes[code] = codes.get(code, 0) + 1
    ok = [ms for code, ms in results if code == 200]
    return {
        "concurrency": concurrency,
        "requests": n,
        "wall_seconds": round(wall, 3),
        "throughput_rps": round(n / wall, 1) if wall > 0 else None,
        "status_counts": dict(sorted(codes.items())),
        "ok_count": len(ok),
        "error_rate_pct": round(100.0 * (n - len(ok)) / n, 2) if n else None,
        "p50_ms": round(percentile(ok, 50), 1),
        "p95_ms": round(percentile(ok, 95), 1),
        "p99_ms": round(percentile(ok, 99), 1),
        "max_ms": round(max(ok), 1) if ok else None,
        "mean_ms": round(statistics.fmean(ok), 1) if ok else None,
    }


async def main_async(args: argparse.Namespace) -> int:
    secret = os.getenv("STRIPE_WEBHOOK_SECRET") or None
    db_path = os.getenv("PAYPILOT_DB_PATH") or "data/paypilot.db"
    ladder = [int(x) for x in args.ladder.split(",") if x.strip()]

    # Origin, not "the url minus its last segment". Stripping one segment happens
    # to work for /payment-failed and silently 404s for /webhooks/stripe, which
    # reads as "the app is not running" when the app is running fine.
    parts = urlsplit(args.url)
    health_url = urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            health = await c.get(health_url)
        if health.status_code != 200:
            print(f"health check at {health_url} returned {health.status_code}; is the app running?")
            return 2
    except Exception as exc:
        print(f"cannot reach {health_url}: {exc}\nStart the app first (see the module docstring).")
        return 2

    run: dict[str, Any] = {
        "target": args.url,
        "signed": bool(secret),
        "db_path": db_path,
        "requests_per_step": args.requests,
        "ladder": ladder,
        "env": {
            k: os.getenv(k)
            for k in (
                "RATE_LIMIT_MAX",
                "RATE_LIMIT_GLOBAL_MAX",
                "RATE_LIMIT_WINDOW_SECONDS",
                "OPENAI_API_KEY",
                "LANGFUSE_PUBLIC_KEY",
                "PAYPILOT_SEND_EMAIL",
                "PAYPILOT_DB_PATH",
            )
        },
        "llm_path": "template (no OPENAI_API_KEY)" if not os.getenv("OPENAI_API_KEY") else "live model",
        "steps": [],
    }
    # Never print the key itself, only whether one is set.
    # Never print a credential, only whether one is set. These two lines are the
    # difference between a reproducible config record and a leaked key.
    run["env"]["OPENAI_API_KEY"] = "set" if run["env"]["OPENAI_API_KEY"] else None
    run["env"]["LANGFUSE_PUBLIC_KEY"] = "set" if run["env"]["LANGFUSE_PUBLIC_KEY"] else None
    run["tracing"] = "langfuse ON" if run["env"]["LANGFUSE_PUBLIC_KEY"] else "langfuse off"

    print(f"target      {args.url}   signed={bool(secret)}   llm={run['llm_path']}")
    print(f"ledger      {db_path}")
    print(f"{'conc':>5} {'rps':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'err%':>6}  ledger delta")

    offset = 0
    before_all = ledger_snapshot(db_path)
    for c in ladder:
        before = ledger_snapshot(db_path)
        step = await run_step(args.url, secret, c, args.requests, offset, args.timeout)
        offset += args.requests
        after = ledger_snapshot(db_path)
        step["ledger_before"] = before
        step["ledger_after"] = after
        step["ledger_delta"] = {
            t: (after.get(t) - before.get(t))
            for t in LEDGER_TABLES
            if isinstance(after.get(t), int) and isinstance(before.get(t), int)
        }
        # The claim under test: every request the app said it handled left a row.
        fd = step["ledger_delta"].get("failures")
        step["failures_match_200s"] = (fd == step["ok_count"]) if fd is not None else None
        run["steps"].append(step)
        delta = step["ledger_delta"].get("failures")
        print(
            f"{c:>5} {step['throughput_rps'] or 0:>8} {step['p50_ms']:>8} {step['p95_ms']:>8} "
            f"{step['p99_ms']:>8} {step['error_rate_pct']:>6}  "
            f"failures+{delta} vs {step['ok_count']} ok"
            # Only an explicit False is a mismatch. None means the delta could
            # not be computed (no database before the first step), and printing
            # a warning for "unknown" is how a gate teaches everyone to skim it.
            f"{'   <-- MISMATCH' if step['failures_match_200s'] is False else ''}"
        )
        await asyncio.sleep(args.settle)

    run["ledger_start"] = before_all
    run["ledger_end"] = ledger_snapshot(db_path)
    Path(args.out).write_text(json.dumps(run, indent=2))
    print(f"\nwrote {args.out}")

    end = run["ledger_end"]
    print(f"failures with no state:               {end.get('failures_without_state')}   (assert 0)")
    print(f"failures with no reason code:         {end.get('failures_without_reason')}   (assert 0)")
    print(f"messages with no failure row:         {end.get('messages_without_failure')}   (assert 0)")
    print(
        f"failures with no transition yet:      "
        f"{end.get('failures_without_transition_REPORTED')}   "
        f"(reported, not asserted: transitions are change-only)"
    )
    mismatches = [s["concurrency"] for s in run["steps"] if s["failures_match_200s"] is False]
    print(f"steps where rows written != 200s:     {mismatches or 'none'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--ladder", default=DEFAULT_LADDER, help="comma-separated concurrency steps")
    p.add_argument("--requests", type=int, default=DEFAULT_REQUESTS, help="requests per step")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--settle", type=float, default=1.0, help="seconds between steps")
    p.add_argument("--out", default="data/loadtest-results.json")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
