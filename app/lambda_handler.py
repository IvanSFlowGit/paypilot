# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""AWS Lambda entry point for the decision slice (API Gateway HTTP API, payload 2.0).

Three routes, the same three the FastAPI app serves on Fly, with the same
status codes and bodies because both call :mod:`app.decision`:

* ``GET  /health``                  - no auth, no database.
* ``POST /decide``                  - bearer auth, decision, audit row in RDS.
* ``GET  /decisions/{invoice_id}``  - bearer auth, audit rows read back from RDS.

Deliberately imports nothing from the LangGraph side of the package, so the
deployment zip holds this file, app/decision.py, app/decision_audit.py and pg8000.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path

from app.decision import (
    DECISION_TOKEN_ENV,
    MAX_BODY_BYTES,
    check_bearer,
    handle_audit_lookup,
    handle_decision_request,
)
from app.decision_audit import PostgresDecisionAudit

log = logging.getLogger("paypilot.lambda")
log.setLevel(logging.INFO)

_SCHEMA_PATH = Path(__file__).resolve().parent / "decision_schema.sql"

_audit = None
_schema_applied = False


def _get_audit():
    """One audit backend per warm container; schema applied once per container."""
    global _audit, _schema_applied
    if _audit is None:
        _audit = PostgresDecisionAudit()
    if not _schema_applied:
        _audit.ensure_schema(_SCHEMA_PATH.read_text(encoding="utf-8"))
        _schema_applied = True
    return _audit


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, sort_keys=True),
    }


def _parse_body(event: dict) -> tuple[object, tuple[int, dict] | None]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None, (400, {"error": "invalid_body", "detail": "body is not valid base64 UTF-8"})
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return None, (413, {"error": "body_too_large", "detail": f"body over {MAX_BODY_BYTES} bytes"})
    try:
        return json.loads(raw), None
    except ValueError:
        return None, (400, {"error": "invalid_body", "detail": "body is not valid JSON"})


def route(event: dict, audit_factory=None) -> tuple[int, dict]:
    """Dispatch one HTTP API event. Separated from :func:`handler` for tests.

    ``audit_factory`` defaults to the module's ``_get_audit`` looked up at CALL
    time, not bound as a default argument at import, so a test that patches
    ``_get_audit`` patches what actually runs.
    """
    audit_factory = audit_factory or _get_audit
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "")
    path = event.get("rawPath", "")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}

    is_decide = method == "POST" and path == "/decide"
    is_lookup = method == "GET" and path.startswith("/decisions/")
    if not (is_decide or is_lookup):
        return 404, {"error": "not_found", "detail": "no such route"}

    refusal = check_bearer(headers.get("authorization"), os.environ.get(DECISION_TOKEN_ENV))
    if refusal is not None:
        if refusal[0] == 401:
            # Same alertable event name the FastAPI side emits. Never the token.
            log.error(json.dumps({
                "event": "decision_token_rejected",
                "path": event.get("routeKey") or path,
                "severity": "error",
            }))
        return refusal

    try:
        audit = audit_factory()
    except Exception:  # noqa: BLE001 - cannot reach the audit store: fail closed
        log.exception("audit store unavailable")
        return 503, {"error": "audit_unavailable", "detail": "audit store unreachable"}

    if is_decide:
        body, error = _parse_body(event)
        if error is not None:
            return error
        return handle_decision_request(body, audit)

    invoice_id = (event.get("pathParameters") or {}).get("invoice_id")
    if invoice_id is None:
        invoice_id = path[len("/decisions/"):]
    return handle_audit_lookup(invoice_id, audit)


def handler(event: dict, context) -> dict:
    """Lambda entry point. Logs one line per request, never the body or headers."""
    started = time.monotonic()
    status, body = route(event)
    http = event.get("requestContext", {}).get("http", {})
    log.info(json.dumps({
        "event": "request",
        "method": http.get("method"),
        "path": event.get("routeKey") or event.get("rawPath"),
        "status": status,
        "rule_fired": body.get("rule_fired"),
        "ms": round((time.monotonic() - started) * 1000, 1),
        "request_id": getattr(context, "aws_request_id", None),
    }))
    return _response(status, body)
