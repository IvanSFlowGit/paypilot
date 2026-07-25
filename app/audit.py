# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Structured audit logging for every LLM call.

One JSON event per LLM call, emitted *after* the full guard + re-hydrate +
re-check chain so it carries the final verdict (baseline item 7). The event is
written to stdout via a dedicated logger, which Fly captures.

No PII in the event: it records the node, model, a boundary-normalized prompt
hash, the guard verdict, and the injection/fallback flags - never the customer
name/email or the prompt text itself.

The prompt hash normalizes the per-request random fence boundary to a constant
before hashing, so the same template with the same data hashes identically
across requests (an un-normalized hash would be unique every time - the random
boundary changes each call - and so useless for comparison or dedup).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.pii import safe_log_fields

_audit_log = logging.getLogger("paypilot.audit")

# Make the app's own logs reach stdout in production. Uvicorn configures only its
# own loggers, and the root last-resort handler is WARNING-only, so without this
# the INFO audit events (and even the startup WARNINGs, which never had a stdout
# handler) are dropped and Fly never captures them. One INFO stdout handler on the
# shared "paypilot" PARENT logger covers audit events, the access log, and the
# startup warnings via propagation - emitted exactly once (only the parent carries
# a handler). ``propagate`` stays True so pytest's caplog still captures via root.
# Imported on every LLM path (nodes -> audit), so it is installed before app
# startup fires.
_app_log = logging.getLogger("paypilot")
_app_log.setLevel(logging.INFO)
if not any(getattr(h, "_paypilot_stdout", False) for h in _app_log.handlers):
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _handler._paypilot_stdout = True  # idempotency marker across reimports
    _app_log.addHandler(_handler)

# Constant that every per-request boundary is normalized to before hashing.
_BOUNDARY_CONST = "BOUNDARY"


def prompt_sha256(prompt: str, boundary: str | None) -> str:
    """sha256 of ``prompt`` with the random ``boundary`` normalized to a constant."""
    normalized = prompt.replace(boundary, _BOUNDARY_CONST) if boundary else prompt
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Append-only, queryable audit event log (SOC2-ready "who did what when")
# ---------------------------------------------------------------------------
#
# The stdout JSON lines above are the alert channel; they are not queryable after
# the fact. A SOC2 reviewer asks "show me every money- or auth-affecting event
# for this window" - that needs durable, ordered, immutable storage. This log is
# that artifact.
#
# It is SEPARATE from the recovery ledger (``app.store``) on purpose: a GDPR
# erasure deletes a customer's ledger rows, but the audit trail of WHAT WAS DONE
# (including the erasure request itself) must survive - so it is written to its
# own database and records only a SALTED HASH of any customer id, never a name or
# email. Immutability is enforced by the API surface: the class exposes append
# and query only, with no update or delete method.

_AUDIT_DB_ENV = "PAYPILOT_AUDIT_DB_PATH"

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,
    severity    TEXT NOT NULL,
    detail      TEXT,
    fields      TEXT NOT NULL,
    actor       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_events(event);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
"""


class AuditEventLog:
    """Append-only SQLite store for security/audit events.

    One row per event: a server-set UTC timestamp, an event name, a severity, a
    short human ``detail`` string, and a JSON ``fields`` blob that has already
    passed :func:`app.pii.safe_log_fields` (so no raw name/email can land here).
    There is deliberately no update or delete method - the only ways to change
    the table are append and read, which is what "immutable audit log" means at
    the code level.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path or os.getenv(_AUDIT_DB_ENV) or ":memory:")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_AUDIT_SCHEMA)
        self._conn.commit()

    def record(
        self,
        event: str,
        *,
        detail: str = "",
        severity: str = "info",
        actor: str | None = None,
        **fields,
    ) -> dict:
        """Append one event and return the stored record.

        ``fields`` is passed through :func:`safe_log_fields` first, so a caller
        that accidentally hands over ``name``/``email`` gets a salted hash, not
        the raw value, in the durable log.
        """
        safe = safe_log_fields(fields)
        ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit_events (ts, event, severity, detail, fields, actor) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, event, severity, detail, json.dumps(safe), actor),
            )
            self._conn.commit()
            row_id = int(cur.lastrowid)
        return {
            "id": row_id,
            "ts": ts,
            "event": event,
            "severity": severity,
            "detail": detail,
            "fields": safe,
            "actor": actor,
        }

    def query(
        self,
        *,
        event: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Return events, newest last, optionally filtered by name and time range.

        ``since``/``until`` compare against the ISO ``ts`` (lexical order matches
        chronological order for ISO 8601 in UTC).
        """
        clauses, params = [], []
        if event:
            clauses.append("event = ?")
            params.append(event)
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self._conn.execute(
            f"SELECT * FROM audit_events {where} ORDER BY id LIMIT ?", params
        ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["fields"] = json.loads(rec["fields"]) if rec["fields"] else {}
            out.append(rec)
        return out

    def count(self, *, event: str | None = None) -> int:
        if event:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM audit_events WHERE event = ?", (event,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_audit_event_log: AuditEventLog | None = None
_audit_event_lock = threading.Lock()


def get_audit_log() -> AuditEventLog | None:
    """Return the process-wide queryable audit log, or None when not configured.

    Env-gated (``PAYPILOT_AUDIT_DB_PATH``) in the same opt-in spirit as tracing:
    the demo/test paths run with it unset and pay nothing, while a real
    deployment points it at a durable, encrypted volume and gets the full trail.
    """
    global _audit_event_log
    with _audit_event_lock:
        if _audit_event_log is None and os.getenv(_AUDIT_DB_ENV):
            _audit_event_log = AuditEventLog()
        return _audit_event_log


def reset_audit_log(log: AuditEventLog | None = None) -> None:
    """Replace the singleton (test helper; also used to reopen a moved DB)."""
    global _audit_event_log
    with _audit_event_lock:
        _audit_event_log = log


def _persist(event: str, severity: str, detail: str, fields: dict) -> None:
    """Best-effort append to the durable log; never raise into the caller.

    A logging store that can take down a webhook is worse than no store, so a
    failure here is swallowed after emitting a stderr note. The stdout JSON line
    has already been written by the caller, so the event is not lost.
    """
    log = get_audit_log()
    if log is None:
        return
    try:
        log.record(event, detail=detail, severity=severity, **(fields or {}))
    except Exception as exc:  # noqa: BLE001 - audit must not break the hot path
        print(
            json.dumps({"event": "audit_log_write_failed", "error_type": type(exc).__name__}),
            file=sys.stderr,
        )


def audit_security_event(
    *,
    event: str,
    detail: str,
    severity: str = "warning",
    sink=None,
) -> dict:
    """Emit one security audit event (the alert channel for a rejected request).

    Used for webhook signature failures and for a missing signing secret in a
    production deployment. A rejected webhook is either a misconfiguration or
    someone forging events at a revenue system, and both deserve a log line
    loud enough to alert on rather than a silent 400.

    ``detail`` must never carry the payload, the signature, or the secret - only
    a description of what was rejected and why.
    """
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": event,
        "severity": severity,
        "detail": detail,
    }
    line = json.dumps(record)
    if sink is not None:
        sink(line)
    elif severity == "error":
        _audit_log.error(line)
    else:
        _audit_log.warning(line)
    # Also land in the durable, queryable trail (money/auth events are exactly
    # what a SOC2 reviewer queries). Best-effort, PII-safe, opt-in via env.
    _persist(event, severity, detail, {})
    return record


def audit_llm_call(
    *,
    node: str,
    model: str,
    prompt_template_id: str,
    prompt: str,
    boundary: str | None,
    guards_failed: list[str],
    injection_suspected: bool,
    fallback_used: bool,
    duration_ms: float,
    sink=None,
) -> dict:
    """Build, emit, and return one audit event for a single LLM call.

    ``guards_failed`` is the list of output-safety violations that tripped the
    fail-closed path (empty when the model's text shipped as-is). ``sink``, when
    given, receives the JSON line instead of the module logger (used by tests).
    """
    event = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": "llm_call",
        "node": node,
        "model": model,
        "prompt_template_id": prompt_template_id,
        "prompt_sha256": prompt_sha256(prompt, boundary),
        "boundary_id": boundary,
        "guards_passed": not guards_failed,
        "guards_failed": list(guards_failed),
        "injection_suspected": bool(injection_suspected),
        "fallback_used": bool(fallback_used),
        "duration_ms": round(float(duration_ms), 1),
    }
    line = json.dumps(event)
    if sink is not None:
        sink(line)
    else:
        _audit_log.info(line)
    _persist(
        "llm_call",
        "info",
        f"{node}/{model}",
        {
            "node": node,
            "model": model,
            "status": "fallback" if fallback_used else "ok",
        },
    )
    return event
