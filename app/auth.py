# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Endpoint authentication (RBAC-lite, demo-appropriate).

Two independent, optional controls, both constant-time and both fail-open to a
warned demo mode when their secret is unset (PayPilot's public URL is a
credential-free interview demo):

* :func:`verify_webhook_signature` - HMAC-SHA256 over the raw request body,
  compared against ``X-PayPilot-Signature``, keyed by ``WEBHOOK_SECRET``.
  NOTE: this is body-only HMAC with no timestamp, so a captured signed request
  is replayable. That is acceptable for a demo; production must add a timestamp
  component (see the security baseline, item 5).
* :func:`verify_bearer` - constant-time bearer-token check for admin/metrics
  routes, keyed by ``ADMIN_TOKEN``.

An empty-string secret is treated as unset. Secrets are never logged.
"""

from __future__ import annotations

import hashlib
import hmac


def _configured(secret: str | None) -> bool:
    """True only when a non-empty secret is set (empty string counts as unset)."""
    return bool(secret and secret.strip())


def sign_body(body: bytes, secret: str) -> str:
    """Return the hex HMAC-SHA256 of ``body`` under ``secret`` (for tests/clients)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_webhook_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    """Validate ``X-PayPilot-Signature`` over the raw body.

    Returns True (allow) when no secret is configured - demo mode. When a secret
    is set, only a matching HMAC passes. Comparison is constant-time.
    """
    if not _configured(secret):
        return True
    if not signature:
        return False
    expected = sign_body(body, secret)  # secret is non-empty here
    # Compared as BYTES. hmac.compare_digest raises TypeError on str
    # arguments containing non-ASCII, and Starlette decodes headers as
    # latin-1, so two high bytes in a header turned every rejection into an
    # unhandled 500 - and bypassed the security audit event, meaning the one
    # input shape most likely to be an attacker was the one that did not
    # alert. encode() keeps the comparison constant-time.
    return hmac.compare_digest(expected.encode("utf-8"), signature.strip().encode("utf-8"))


def verify_bearer(auth_header: str | None, expected_token: str | None) -> bool:
    """Validate an ``Authorization: Bearer <token>`` header against ``expected_token``.

    Returns True (allow) when no token is configured - demo mode. Comparison is
    constant-time.
    """
    if not _configured(expected_token):
        return True
    if not auth_header:
        return False
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return False
    presented = auth_header[len(prefix):].strip()
    # Compared as BYTES. hmac.compare_digest raises TypeError on str
    # arguments containing non-ASCII, and Starlette decodes headers as
    # latin-1, so two high bytes in a header turned every rejection into an
    # unhandled 500 - and bypassed the security audit event, meaning the one
    # input shape most likely to be an attacker was the one that did not
    # alert. encode() keeps the comparison constant-time.
    return hmac.compare_digest(presented.encode("utf-8"), expected_token.strip().encode("utf-8"))
