"""One wrapper for every outbound call this package makes.

Not a guard bolted on after each incident: one function with the five
properties the estate's stability rule names, plus the two that a bank API
makes unavoidable.

WHAT IT DOES.

Timeout on every call, because a hung read is indistinguishable from a slow
one and only one of them is worth waiting for. Bounded retry with backoff. A
typed error on give-up, so a caller cannot mistake a failure for an answer.
One structured line carrying a correlation id. And the provider's STATUS AND
BODY on every failure, because a status code never names the fault: 401, 403
and 429 are exactly the kind of difference that reads as informative and
settles nothing, while the body says "Required scope: accounts:read".

WHAT IT REFUSES TO DO, AND WHY EACH REFUSAL IS HERE.

RETRY IS OPT-IN PER CALL, NEVER A DEFAULT. An AIS read is a GET and retrying
it is free. ``exchange_code`` and ``refresh`` are POSTs that consume a
single-use value, so retrying one turns a slow success into a hard failure and
can burn the consent. A wrapper that retried by default would be correct for
the reads and wrong for the two calls that matter, so the caller says which it
is and there is no default to get wrong.

A TIMEOUT ON A NON-RETRYABLE CALL IS UNKNOWN, NOT FAILED. The request may have
landed. Returning "failed" would licence a caller to re-issue it, which is the
one thing that must not happen to a single-use code, so the typed error says
``outcome_unknown`` and the caller's only safe move is to read state back.

QUOTA EXHAUSTION IS A REFUSAL RATHER THAN A RETRY. Running out of credit or
hitting a rate limit is an ordinary provider response, not a transport fault,
so retrying it spends the remainder and reports a transport problem. It raises
``QuotaExhausted`` immediately.

IT ALERTS ON THE SECOND CONSECUTIVE FAILURE, NOT THE FIRST. One blip clears
itself far more often than it persists, and a stream that is mostly noise
trains the reader to skim the one message that matters. A healthy call resets
the counter. The count lives in a state file rather than in memory, because a
process that restarts between failures would otherwise never reach two.

THE BODY IS EXTRACTED POSITIVELY, NEVER REDACTED. This is the one decision
here that was got wrong first and is worth stating as a decision. The original
version ran ``app.pii.scrub_freeform`` over the provider's body and the
docstring claimed names and addresses were scrubbed. That function masks
Luhn-valid card runs and nothing else, so the first test written against the
claim put an email address straight into the log. A negative redaction fails
open on every shape nobody thought of; a positive extraction fails closed on
all of them.

So ``_diagnostic`` parses the body as JSON and emits ONLY the values of keys
known to carry diagnostics - detail, error, error_description, message, code,
title, status. Everything else is dropped unread, including a key a future
provider invents. A body that is not JSON contributes its media type and its
length and no content at all. What survives is then scrubbed for card runs and
email addresses as a second assertion at a different layer, and truncated.

The thing the wrapper exists for still arrives: "Required scope: accounts:read"
lives in a JSON ``detail`` field, which is exactly what is kept.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app import pii
from app.audit import audit_security_event

_log = logging.getLogger("paypilot.bank.http")

# Deliberately short. A bank AIS read that has not answered in ten seconds is
# not going to, and a caller holding a request open is a caller holding a
# worker.
DEFAULT_TIMEOUT_S = 10.0

# Three attempts, not more. Past that the provider is down rather than busy,
# and a longer ladder only moves the failure later into somebody's day.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S = 0.5

# How much of a provider's error body reaches the log. Enough to carry a
# message like "Required scope: accounts:read", short enough that a verbose
# error page cannot fill a log file.
BODY_LOG_CHARS = 400

# Statuses worth trying again. 408 and 425 are the provider saying "not now",
# and 5xx is the provider saying "not me". 429 is NOT here: it is a refusal.
RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})

# Statuses that mean stop asking. 429 is a rate limit and 402 is payment
# required, which on a metered API is the credit having run out.
QUOTA_STATUS = frozenset({402, 429})

_STATE_ENV = "PAYPILOT_BANK_FAILURE_STATE"
_DEFAULT_STATE = pathlib.Path(".paypilot_bank_failures.json")

# Second consecutive failure, per the rule above. Named rather than inlined so
# a test can state the threshold it is asserting.
ALERT_ON_CONSECUTIVE = 2


class BankHTTPError(RuntimeError):
    """A call gave up. Carries the status and the masked body, never a guess."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        correlation_id: str,
        attempts: int,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.correlation_id = correlation_id
        self.attempts = attempts
        # True when the request may have landed. A caller must read state back
        # rather than re-issue.
        self.outcome_unknown = outcome_unknown


class QuotaExhausted(BankHTTPError):
    """The provider refused on credit or rate, which no retry can clear."""


class ProviderUnreachable(BankHTTPError):
    """Transport never got an answer. Separate from a provider that said no."""


@dataclass
class _FailureState:
    """Consecutive failures per provider, durable across restarts."""

    path: pathlib.Path
    counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> _FailureState:
        resolved = path or pathlib.Path(os.getenv(_STATE_ENV, str(_DEFAULT_STATE)))
        counts: dict[str, int] = {}
        try:
            raw = json.loads(resolved.read_text())
            if isinstance(raw, dict):
                counts = {
                    str(k): int(v) for k, v in raw.items() if isinstance(v, int)
                }
        except (OSError, ValueError):
            # A missing or unreadable state file means "no failures recorded",
            # which is the honest reading: it cannot mean a count we never saw.
            counts = {}
        return cls(path=resolved, counts=counts)

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.counts))
            tmp.replace(self.path)
        except OSError:
            # Losing the counter degrades alerting to first-failure-is-quiet.
            # It must never take down the call that was otherwise fine.
            _log.warning("could not persist bank failure state at %s", self.path)

    def record_failure(self, provider: str) -> int:
        self.counts[provider] = self.counts.get(provider, 0) + 1
        self._save()
        return self.counts[provider]

    def record_success(self, provider: str) -> None:
        if self.counts.pop(provider, None) is not None:
            self._save()


# Keys that carry a provider's own description of the fault. RFC 7807 uses
# title/detail/status/type; OAuth 2 uses error/error_description; most REST
# APIs use message or detail plus a code. Anything not on this list is dropped
# unread, so a key a future provider invents cannot leak by default.
_DIAGNOSTIC_KEYS = (
    "code",
    "detail",
    "error",
    "error_code",
    "error_description",
    "message",
    "reason",
    "status",
    "title",
    "type",
)

# Unanchored, unlike app.pii._EMAIL_RE which matches a whole field. This is a
# second layer over the positive extraction above rather than the only guard:
# a diagnostic value can legitimately name the account holder it failed on.
_EMAIL_IN_TEXT_RE = re.compile(r"[^\s<>()\[\],;:\"]+@[^\s<>()\[\],;:\"]+\.[A-Za-z]{2,}")


def _scrub(text: str) -> str:
    """Card runs and email addresses out of whatever survived extraction."""
    return _EMAIL_IN_TEXT_RE.sub("{{EMAIL}}", pii.scrub_freeform(text or ""))


def _diagnostic(response: httpx.Response | None, fallback: str = "") -> str:
    """The provider's own description of the fault, and nothing else.

    Positive extraction. A JSON body contributes only the diagnostic keys it
    actually carries; anything else contributes its shape rather than its
    content, because a body of unknown shape is untrusted content and a
    blocklist over it fails open on the first shape nobody anticipated.
    """
    if response is None:
        return _scrub(fallback)[:BODY_LOG_CHARS]

    media = (response.headers.get("content-type") or "unknown").split(";")[0].strip()
    try:
        payload = response.json()
    except ValueError:
        # Not JSON. Its shape is useful and its content is not worth the risk.
        return f"<{media}, {len(response.content)} bytes, not JSON>"

    found: list[str] = []

    def _harvest(node: object, depth: int = 0) -> None:
        # Depth-bounded: a provider that nests errors three deep is ordinary,
        # and an unbounded walk over a hostile document is not.
        if depth > 3 or len(found) >= 10:
            return
        if isinstance(node, dict):
            for key in _DIAGNOSTIC_KEYS:
                if key in node and isinstance(node[key], (str, int, float)):
                    found.append(f"{key}={node[key]}")
            for value in node.values():
                if isinstance(value, (dict, list)):
                    _harvest(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:5]:
                _harvest(value, depth + 1)

    _harvest(payload)
    if not found:
        return f"<{media}, no diagnostic key present>"
    return _scrub("; ".join(found))[:BODY_LOG_CHARS]


def _emit(
    *,
    provider: str,
    correlation_id: str,
    method: str,
    url: str,
    attempt: int,
    status: int | None,
    body: str | None,
    outcome: str,
    elapsed_ms: int,
) -> None:
    """One structured line per attempt. The failing line carries the body."""
    record = {
        "correlation_id": correlation_id,
        "provider": provider,
        "method": method,
        # The path only. A full URL can carry a token in a query string.
        "path": httpx.URL(url).path,
        "attempt": attempt,
        "status": status,
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
    }
    if body is not None:
        record["body"] = body
    line = json.dumps(record)
    if outcome == "ok":
        _log.info(line)
    else:
        _log.warning(line)


def request(
    method: str,
    url: str,
    *,
    provider: str,
    retry: bool,
    client: httpx.Client | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_s: float = DEFAULT_BACKOFF_S,
    state: _FailureState | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """One outbound call, or a typed refusal that says what the provider said.

    ``retry`` has no default on purpose. A GET against an account feed is safe
    to repeat and a POST that spends a single-use code is not, and only the
    caller knows which this is. Making it explicit means the dangerous case
    cannot be reached by forgetting an argument.
    """
    if not isinstance(retry, bool):
        raise TypeError(
            "retry must be an explicit bool: a read may be retried and a call "
            "that consumes a single-use code may not, and there is no safe "
            "default for both."
        )
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    attempts = 1 if not retry else max_attempts
    tracker = state or _FailureState.load()
    correlation_id = uuid.uuid4().hex
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout_s)

    def _fail(exc: BankHTTPError, status: int | None, body: str | None) -> None:
        count = tracker.record_failure(provider)
        if count >= ALERT_ON_CONSECUTIVE:
            audit_security_event(
                event="bank_call_failed",
                severity="error",
                detail=(
                    f"{provider}: {count} consecutive failures, last status "
                    f"{status}, correlation {correlation_id}"
                ),
            )
        exc.status = status
        exc.body = body

    try:
        last: BankHTTPError | None = None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = http.request(method, url, timeout=timeout_s, **kwargs)
            except httpx.TimeoutException as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                _emit(
                    provider=provider,
                    correlation_id=correlation_id,
                    method=method,
                    url=url,
                    attempt=attempt,
                    status=None,
                    body=_diagnostic(None, str(exc)),
                    outcome="timeout",
                    elapsed_ms=elapsed,
                )
                last = ProviderUnreachable(
                    f"{provider}: timed out after {timeout_s}s",
                    correlation_id=correlation_id,
                    attempts=attempt,
                    # A retryable call is a read, so a timeout means no answer.
                    # A non-retryable call may have landed, and saying so is
                    # the whole point: the caller must read state back.
                    outcome_unknown=not retry,
                )
                if attempt < attempts:
                    sleep(backoff_s * attempt)
                    continue
                _fail(last, None, _diagnostic(None, str(exc)))
                raise last from exc
            except httpx.HTTPError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                _emit(
                    provider=provider,
                    correlation_id=correlation_id,
                    method=method,
                    url=url,
                    attempt=attempt,
                    status=None,
                    body=_diagnostic(None, str(exc)),
                    outcome="transport_error",
                    elapsed_ms=elapsed,
                )
                last = ProviderUnreachable(
                    f"{provider}: transport error",
                    correlation_id=correlation_id,
                    attempts=attempt,
                    outcome_unknown=not retry,
                )
                if attempt < attempts:
                    sleep(backoff_s * attempt)
                    continue
                _fail(last, None, _diagnostic(None, str(exc)))
                raise last from exc

            elapsed = int((time.monotonic() - started) * 1000)
            status = response.status_code
            body = _diagnostic(response) if status >= 400 else None

            if status in QUOTA_STATUS:
                _emit(
                    provider=provider,
                    correlation_id=correlation_id,
                    method=method,
                    url=url,
                    attempt=attempt,
                    status=status,
                    body=body,
                    outcome="quota",
                    elapsed_ms=elapsed,
                )
                refusal = QuotaExhausted(
                    f"{provider}: refused on quota or rate, status {status}",
                    correlation_id=correlation_id,
                    attempts=attempt,
                )
                _fail(refusal, status, body)
                raise refusal

            if status in RETRYABLE_STATUS and attempt < attempts:
                _emit(
                    provider=provider,
                    correlation_id=correlation_id,
                    method=method,
                    url=url,
                    attempt=attempt,
                    status=status,
                    body=body,
                    outcome="retrying",
                    elapsed_ms=elapsed,
                )
                sleep(backoff_s * attempt)
                continue

            if status >= 400:
                _emit(
                    provider=provider,
                    correlation_id=correlation_id,
                    method=method,
                    url=url,
                    attempt=attempt,
                    status=status,
                    body=body,
                    outcome="error",
                    elapsed_ms=elapsed,
                )
                failure = BankHTTPError(
                    f"{provider}: status {status}",
                    correlation_id=correlation_id,
                    attempts=attempt,
                )
                _fail(failure, status, body)
                raise failure

            _emit(
                provider=provider,
                correlation_id=correlation_id,
                method=method,
                url=url,
                attempt=attempt,
                status=status,
                body=None,
                outcome="ok",
                elapsed_ms=elapsed,
            )
            tracker.record_success(provider)
            return response

        # Unreachable: every path above either returns or raises. Kept so a
        # future edit to the loop cannot fall out of it returning None.
        raise last or BankHTTPError(
            f"{provider}: no attempt was made",
            correlation_id=correlation_id,
            attempts=0,
        )
    finally:
        if owns_client:
            http.close()
