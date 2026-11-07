# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""A durable tally of who called which route, so "has anyone used the public API" has an answer.

Before this, the answer was nowhere. The access log goes to stdout and rotates with the
machine, and the in-process counters behind ``/metrics`` reset every time Fly stops the
machine, which this app does on purpose when idle. Neither says which ROUTE was called, so
"someone hit /decide" and "a scanner hit /wp-login.php" were the same number.

One row per (day, method, route, status, client prefix), incremented in place. Nothing else
is stored: no body, no headers, no query string, no full address. The prefix is the same
coarse /24 (or /48) the access log already writes, so this adds no personal data the log does
not hold. Rows older than ``RETENTION_DAYS`` are deleted.

The route is the TEMPLATE (``/decisions/{invoice_id}``), never the concrete path, so an
invoice id cannot enter the table and cardinality stays bounded. A request that matched no
route is counted under ``(unmatched)``, which is where scanners land.

Recording never fails a request. If the store cannot be opened or written, the call is
swallowed and ``write_failures`` rises, and the summary reports that number, because a tally
that silently stopped writing reads exactly like a quiet week.

``PAYPILOT_DB_PATH`` decides where it lives (``usage.db`` beside it, on the Fly volume). With
no such path set the tally is OFF and says so: an unset path must not quietly become a file in
the working directory.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

RETENTION_DAYS = 90
#: Not worth a row: liveness probes and static assets would drown the signal.
SKIP_PREFIXES = ("/static/",)
SKIP_PATHS = frozenset({"/health", "/usage", "/favicon.ico"})
UNMATCHED = "(unmatched)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_usage (
    day           TEXT NOT NULL,
    method        TEXT NOT NULL,
    route         TEXT NOT NULL,
    status        INTEGER NOT NULL,
    client_prefix TEXT NOT NULL,
    n             INTEGER NOT NULL,
    first_at      TEXT NOT NULL,
    last_at       TEXT NOT NULL,
    PRIMARY KEY (day, method, route, status, client_prefix)
);
"""

_lock = threading.Lock()
_write_failures = 0
_last_prune_day = ""


def db_path() -> Path | None:
    base = os.getenv("PAYPILOT_DB_PATH")
    return Path(base).parent / "usage.db" if base else None


def enabled() -> bool:
    return db_path() is not None


def should_record(path: str) -> bool:
    return path not in SKIP_PATHS and not path.startswith(SKIP_PREFIXES)


def _now() -> datetime:
    return datetime.now(UTC)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute(_SCHEMA)
    return conn


def record(method: str, route: str | None, status: int, client_prefix: str, now: datetime | None = None) -> bool:
    """Count one request. Returns False when it was not written; never raises."""
    global _write_failures, _last_prune_day
    path = db_path()
    if path is None:
        return False
    stamp = (now or _now()).replace(microsecond=0)
    day, iso = stamp.strftime("%Y-%m-%d"), stamp.isoformat()
    try:
        with _lock:
            conn = _connect(path)
            try:
                conn.execute(
                    "INSERT INTO api_usage (day, method, route, status, client_prefix, n, first_at, last_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT (day, method, route, status, client_prefix) "
                    "DO UPDATE SET n = n + 1, last_at = excluded.last_at",
                    (day, method.upper(), route or UNMATCHED, int(status), client_prefix or "unknown", iso, iso),
                )
                if day != _last_prune_day:
                    cutoff = (stamp - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
                    conn.execute("DELETE FROM api_usage WHERE day < ?", (cutoff,))
                    _last_prune_day = day
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception:  # noqa: BLE001 - a tally write never fails a request
        _write_failures += 1
        return False


def summary(days: int = 7, exclude: tuple[str, ...] = (), now: datetime | None = None) -> dict:
    """Per-route totals over the last ``days`` days, optionally leaving out given prefixes.

    ``exclude`` is how the owner removes their own traffic: pass the prefixes the access log
    shows for your own machine and what remains is everyone else.
    """
    path = db_path()
    if path is None:
        return {"enabled": False, "reason": "PAYPILOT_DB_PATH is not set, so there is no volume to keep a tally on"}
    days = max(1, min(int(days), RETENTION_DAYS))
    since = ((now or _now()) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    try:
        with _lock:
            conn = _connect(path)
            try:
                rows = conn.execute(
                    "SELECT method, route, status, client_prefix, n, first_at, last_at FROM api_usage WHERE day >= ?",
                    (since,),
                ).fetchall()
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001 - say it cannot be read; never pretend the week was quiet
        return {"enabled": True, "readable": False, "error": type(exc).__name__,
                "write_failures_since_boot": _write_failures}
    skip = set(exclude)
    routes: dict[tuple[str, str], dict] = {}
    for method, route, status, prefix, n, first_at, last_at in rows:
        if prefix in skip:
            continue
        r = routes.setdefault(
            (method, route),
            {"method": method, "route": route, "calls": 0, "ok_2xx": 0, "rejected_4xx": 0,
             "errors_5xx": 0, "prefixes": {}, "first_seen": first_at, "last_seen": last_at},
        )
        r["calls"] += n
        r["ok_2xx" if status < 300 else "rejected_4xx" if status < 500 else "errors_5xx"] += n
        r["prefixes"][prefix] = r["prefixes"].get(prefix, 0) + n
        r["first_seen"] = min(r["first_seen"], first_at)
        r["last_seen"] = max(r["last_seen"], last_at)
    out = []
    for r in routes.values():
        top = sorted(r["prefixes"].items(), key=lambda kv: (-kv[1], kv[0]))
        r["distinct_prefixes"] = len(r["prefixes"])
        r["top_prefixes"] = [{"prefix": p, "calls": c} for p, c in top[:5]]
        del r["prefixes"]
        out.append(r)
    out.sort(key=lambda r: (-r["calls"], r["route"]))
    return {
        "enabled": True,
        "readable": True,
        "window_days": days,
        "since_day": since,
        "excluded_prefixes": sorted(skip),
        "total_calls": sum(r["calls"] for r in out),
        "write_failures_since_boot": _write_failures,
        "routes": out,
    }


def reset_for_tests() -> None:
    global _write_failures, _last_prune_day
    _write_failures = 0
    _last_prune_day = ""
