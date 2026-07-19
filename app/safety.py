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
from urllib.parse import urlsplit

# The default link a dunning email may contain: the card-update page. Override
# per environment via PAYPILOT_UPDATE_URL.
PAYMENT_UPDATE_URL = os.getenv("PAYPILOT_UPDATE_URL", "https://app.paypilot.dev/billing/update")

# Hosts whose URLs are allowed in dunning copy. Stripe-hosted pages are the
# real recovery destination once billing portal sessions are in play, and their
# paths are per-session (billing.stripe.com/p/session/<id>), so they can only be
# allowed by host - an exact-URL allowlist cannot express them.
#
# Host matching is deliberately narrower than it looks: it is an exact host
# match, never a suffix match, so "billing.stripe.com.evil.test" does not pass.
# Callers that know the one URL they generated should still pass it explicitly
# to message_violations(); that is a tighter check than the host allowlist and
# is what the drafting node does.
_DEFAULT_ALLOWED_HOSTS = ("billing.stripe.com", "invoice.stripe.com", "pay.stripe.com")

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


def _host_of(url: str) -> str:
    """Hostname of a URL, tolerating the scheme-less ``www.x`` form.

    Uses a real URL parser rather than string splitting. Hand-rolled splitting
    on "/" and "@" is exploitable: a browser treats "#", "?" and "\\" as
    delimiters too, so

        https://evil.test#@billing.stripe.com

    splits to the allowlisted host while the browser navigates to evil.test.
    That URL reached a customer's inbox as the sanctioned card-update link.

    Returns "" when no host can be determined, which fails closed because an
    empty string is never in the allowlist.
    """
    candidate = _norm_url(url)
    # Browsers normalise a backslash to a forward slash; Python's parser does
    # not, and that disagreement is itself a bypass. In
    # "https://evil.test\\@billing.stripe.com" a browser reads the host as
    # evil.test while urlsplit reads "evil.test\\" as userinfo and returns the
    # allowlisted host. Resolve it the way the customer's browser will.
    candidate = candidate.replace("\\", "/")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", candidate):
        candidate = "https://" + candidate  # the bare "www.example.com" form
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    return host.strip().strip(".")


def allowed_link_hosts() -> tuple[str, ...]:
    """Hosts permitted in dunning copy.

    ``PAYPILOT_ALLOWED_LINK_HOSTS`` (comma separated) replaces the Stripe
    defaults outright rather than adding to them, so a client deployment can
    narrow the allowlist to exactly its own domains. Read per call, not cached
    at import, so a test or a redeploy can change it.
    """
    configured = (os.getenv("PAYPILOT_ALLOWED_LINK_HOSTS") or "").strip()
    if configured:
        return tuple(h.strip().lower() for h in configured.split(",") if h.strip())
    return _DEFAULT_ALLOWED_HOSTS


def _allowed_set(allowed) -> set[str]:
    """Normalise the ``allowed`` argument to a set of exact URLs.

    Accepts a single URL string (the original signature, still used by the eval
    guardrails), any iterable of URLs, or None for the configured default.
    """
    if allowed is None:
        allowed = (PAYMENT_UPDATE_URL,)
    elif isinstance(allowed, str):
        allowed = (allowed,)
    return {_norm_url(a) for a in allowed if a}


def find_foreign_urls(text: str, allowed=None, *, allow_hosts: bool = True) -> list[str]:
    """Return every URL in ``text`` that is not sanctioned.

    A URL passes if it exactly matches one of ``allowed``, or if its host is in
    :func:`allowed_link_hosts`. Set ``allow_hosts=False`` to require an exact
    match and nothing else.
    """
    allow = _allowed_set(allowed)
    hosts = set(allowed_link_hosts()) if allow_hosts else set()
    foreign = []
    for url in _URL_RE.findall(text or ""):
        if _norm_url(url) in allow:
            continue
        if hosts and _host_of(url) in hosts:
            continue
        foreign.append(url)
    return foreign


def find_secrets(text: str) -> list[str]:
    """Return secret-shaped substrings found in ``text``."""
    hits: list[str] = []
    for rx in _SECRET_RES:
        hits.extend(m.group(0) for m in rx.finditer(text or ""))
    return hits


def message_violations(text: str, allowed=None, *, allow_hosts: bool = True) -> list[str]:
    """Collect output-safety violations for one drafted message.

    Empty list means clean. Callers fail closed on any violation. ``allowed``
    takes a single URL or an iterable of them; passing the exact link this
    message was built with is stricter than relying on the host allowlist.
    """
    violations: list[str] = []
    foreign = find_foreign_urls(text, allowed, allow_hosts=allow_hosts)
    if foreign:
        violations.append(f"foreign url(s): {foreign}")
    leaked = find_secrets(text)
    if leaked:
        violations.append(f"secret-shaped token(s): {len(leaked)} match(es)")
    return violations
