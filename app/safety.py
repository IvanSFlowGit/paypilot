"""Runtime output-safety for LLM-drafted dunning copy.

Webhook/customer content is untrusted: it can carry prompt-injection that tries
to make the model paste a phishing link or leak a secret. Two layers use this
module:

* :func:`wrap_untrusted` fences untrusted input between per-request, unguessable
  markers so any embedded "instructions" read as data inside a prompt.
* :func:`message_violations` runs over the model's OUTPUT. Callers fail closed on
  any violation - swapping the model's text for a deterministic template - so a
  link we did not sanction, or anything shaped like a secret, never ships.

Kept dependency-free and importable from both ``app`` (runtime) and the eval
guardrails, so the URL/secret rules have exactly one definition.
"""

from __future__ import annotations

import os
import re
import secrets

# The ONE link a dunning email may contain: the card-update page. Every other
# URL is treated as foreign/injected and trips the fail-closed swap. Override
# per environment via PAYPILOT_UPDATE_URL.
PAYMENT_UPDATE_URL = os.getenv("PAYPILOT_UPDATE_URL", "https://app.paypilot.dev/billing/update")

_URL_RE = re.compile(r"https?://[^\s<>\"')]+|www\.[^\s<>\"')]+", re.IGNORECASE)

# Secret-shaped tokens: provider keys, AWS ids, bearer/JWT blobs, PATs, and
# generic "key: value" leakage. Conservative and aimed at obvious exfiltration;
# it only needs to be good enough to fail closed.
_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),                        # OpenAI-style
    re.compile(r"AKIA[0-9A-Z]{16}"),                             # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),                         # GitHub PAT
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),                 # Slack token
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"),  # JWT
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),             # bearer header
    re.compile(r"(?i)(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{6,}"),
)


def new_boundary() -> str:
    """Random per-request boundary token.

    Never hardcoded, so poisoned content cannot guess the marker and close the
    fence itself.
    """
    return secrets.token_hex(5)


def wrap_untrusted(content, boundary: str) -> str:
    """Fence ``content`` between unguessable ``<<UNTRUSTED-...>>`` markers."""
    return f"<<UNTRUSTED-{boundary}>>\n{content}\n<</UNTRUSTED-{boundary}>>"


def _norm_url(u: str) -> str:
    return u.rstrip("/.,);:\"'").lower()


def find_foreign_urls(text: str, allowed: str = PAYMENT_UPDATE_URL) -> list[str]:
    """Return every URL in ``text`` that is not the sanctioned payment link."""
    allow = _norm_url(allowed)
    return [u for u in _URL_RE.findall(text or "") if _norm_url(u) != allow]


def find_secrets(text: str) -> list[str]:
    """Return secret-shaped substrings found in ``text``."""
    hits: list[str] = []
    for rx in _SECRET_RES:
        hits.extend(m.group(0) for m in rx.finditer(text or ""))
    return hits


def message_violations(text: str, allowed: str = PAYMENT_UPDATE_URL) -> list[str]:
    """Collect output-safety violations for one drafted message.

    Empty list means clean. Callers fail closed on any violation.
    """
    violations: list[str] = []
    foreign = find_foreign_urls(text, allowed)
    if foreign:
        violations.append(f"foreign url(s): {foreign}")
    leaked = find_secrets(text)
    if leaked:
        violations.append(f"secret-shaped token(s): {len(leaked)} match(es)")
    return violations
