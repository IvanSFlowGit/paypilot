"""Langfuse tracing for PayPilot.

Wires the LangGraph recovery flow to Langfuse through the LangChain
``CallbackHandler`` (the framework integration, which captures model name, token
usage and observation types automatically, so we do not hand-instrument them).

Tracing is OPT-IN: it activates only when ``LANGFUSE_PUBLIC_KEY`` and
``LANGFUSE_SECRET_KEY`` are set. With no keys (tests, and the credential-free
demo path), :func:`trace_config` returns an empty config and ``run_recovery`` is
completely unchanged, matching PayPilot's fail-open-in-demo ethos.

PII is masked before any trace data leaves the process (defence in depth on top
of the placeholder masking the nodes already apply before the LLM sees data):
card-shaped digit runs and email addresses are redacted from every string in the
payload, and the customer id is hashed, never sent raw.
"""
from __future__ import annotations

import atexit
import os
import re
from typing import Any

from app.pii import hash_pii, scrub_freeform

# Emails can appear in rehydrated output that a callback captures; redact them.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_client = None
_handler = None


def _enabled() -> bool:
    # Keys present, and not under pytest (so the suite never ships test traces).
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _mask_value(value: Any) -> Any:
    """Recursively redact card- and email-shaped data from a trace payload."""
    if isinstance(value, str):
        return _EMAIL_RE.sub("{{EMAIL}}", scrub_freeform(value))
    if isinstance(value, dict):
        return {k: _mask_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(v) for v in value]
    return value


def _mask(data: Any = None, **_kwargs: Any) -> Any:
    """Langfuse mask hook. Tolerates positional or keyword ``data`` across SDKs."""
    return _mask_value(data)


def _init() -> None:
    """Lazily create the Langfuse client + LangChain handler once, if enabled."""
    global _client, _handler
    if _client is not None or not _enabled():
        return
    # The Langfuse SDK reads LANGFUSE_HOST; accept the LANGFUSE_BASE_URL the user
    # set and mirror it so either name works.
    base = os.getenv("LANGFUSE_BASE_URL")
    if base and not os.getenv("LANGFUSE_HOST"):
        os.environ["LANGFUSE_HOST"] = base
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    _client = Langfuse(mask=_mask)
    _handler = CallbackHandler()
    atexit.register(_client.flush)


def trace_config(event: dict) -> dict:
    """Return the LangChain config that traces one recovery run to Langfuse.

    Empty config (a no-op) when tracing is disabled, so callers stay unchanged.
    """
    _init()
    if _handler is None:
        return {}
    ev = event or {}
    customer_id = str(ev.get("customer_id") or "unknown")
    return {
        "callbacks": [_handler],
        "run_name": "paypilot-recovery",
        "metadata": {
            "langfuse_user_id": hash_pii(customer_id),  # hashed, never the raw id
            "langfuse_tags": ["dunning", "failed-payment-recovery"],
            "failure_code": ev.get("failure_code"),
            "attempt": ev.get("attempt"),
        },
    }


def flush() -> None:
    """Flush buffered traces. Safe no-op when tracing is disabled."""
    if _client is not None:
        _client.flush()
