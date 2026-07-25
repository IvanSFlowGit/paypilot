# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""PII pseudonymization for LLM prompt assembly.

Graph state keeps *raw* customer values; this module masks them to placeholders
only at the moment a prompt is built, so a customer's real name/email never
reach the model (and, in real mode, never leave for a third-party LLM). After
the model answers and the output-safety guards run, :func:`rehydrate` swaps the
placeholders back for the real values, and a final guard pass re-checks the
now-hydrated text (re-hydration inserts data *after* the first guard pass, so it
must be re-guarded - see ``app.nodes``).

Design (matches the security baseline, item 6):

* Structured, known fields are masked by field, not by NER: ``name`` ->
  ``{{NAME_1}}``, ``email`` -> ``{{EMAIL_1}}``. Plan/tier are not PII.
* Input validation lives here too: a value that will be re-inserted into model
  output is length-capped and charset-checked, so a URL or secret-shaped string
  can never be smuggled in as a "name" and walk past the URL allowlist on
  re-hydration. An invalid name/email maps to a safe fallback, never the raw
  hostile value.
* :func:`scrub_freeform` masks card-shaped digit runs (Luhn-verified) in
  free-text that reaches a prompt, while leaving schema-validated structured
  values (invoice ids, event ids, amounts, ISO dates) intact.

Kept dependency-free so it imports cleanly from the runtime nodes and the tests.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

from app.safety import find_foreign_urls, find_secrets

_log = logging.getLogger("paypilot")

# Placeholder tokens. Double-brace form so a stray single brace in real copy
# never looks like a placeholder, and so the "no unresolved placeholder" guard
# in the nodes has an unambiguous pattern to scan for.
NAME_PLACEHOLDER = "{{NAME_1}}"
EMAIL_PLACEHOLDER = "{{EMAIL_1}}"

# Safe fallbacks used when a maskable field fails validation, so re-hydration
# inserts a harmless value rather than the hostile original.
_NAME_FALLBACK = "there"
_EMAIL_FALLBACK = ""

# Any leftover ``{{...}}`` after re-hydration means a placeholder went
# unresolved (e.g. the model mangled the token); the nodes fail closed on it.
_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}")

# A pragmatic email shape. Deliberately conservative: it only needs to reject
# non-emails (URLs, injected text) so a bad value never round-trips as PII.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_MAX_NAME_LEN = 80
_DIGIT_RUN_RE = re.compile(r"\d{7,}")           # long digit run: not a real name
# Separators included: a human typing a card into a support reply writes
# "4242 4242 4242 4242", and a contiguous-only pattern left that unmasked
# while masking the joined form.
_CARD_RUN_RE = re.compile(r"\b(?:\d[ \t.\-]{0,2}){12,18}\d\b")     # card-shaped candidate for Luhn


def _luhn_ok(number: str) -> bool:
    """True if ``number`` (digits only) passes the Luhn checksum."""
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def name_is_safe(name: str) -> bool:
    """Public alias. The single definition of "safe to re-insert as a name".

    Anything deciding whether untrusted free text may appear in model-facing
    copy must use THIS, not a weaker URL/secret-only check. Two validators
    disagreeing is how a name that one layer masked reached a prompt raw.
    """
    return _name_is_safe(name)


def _name_is_safe(name: str) -> bool:
    """Reject names that carry a URL, a long digit run, or secret-shaped text.

    These are the shapes that turn a "name" into an injection vector once it is
    re-inserted into model output, so a name matching any of them is treated as
    hostile and mapped to the safe fallback.
    """
    if len(name) > _MAX_NAME_LEN:
        return False
    if "://" in name or "www." in name.lower():
        return False
    if _DIGIT_RUN_RE.search(name):
        return False
    if find_foreign_urls(name) or find_secrets(name):
        return False
    return True


def mask_structured_pii(fields: dict) -> tuple[dict, dict]:
    """Mask known PII fields to placeholders.

    Returns ``(masked_fields, mapping)`` where ``masked_fields`` is a copy of
    ``fields`` with ``name``/``email`` replaced by their placeholders, and
    ``mapping`` maps each placeholder to the *validated* value to restore on
    re-hydration. An unsafe name or malformed email maps to a safe fallback, so
    a hostile value never round-trips.
    """
    masked = dict(fields)
    mapping: dict[str, str] = {}

    if "name" in fields and fields["name"] is not None:
        raw = str(fields["name"]).strip()
        masked["name"] = NAME_PLACEHOLDER
        mapping[NAME_PLACEHOLDER] = raw if raw and _name_is_safe(raw) else _NAME_FALLBACK

    if "email" in fields and fields["email"] is not None:
        raw = str(fields["email"]).strip()
        masked["email"] = EMAIL_PLACEHOLDER
        mapping[EMAIL_PLACEHOLDER] = raw if _EMAIL_RE.match(raw) else _EMAIL_FALLBACK

    return masked, mapping


def rehydrate(text: str, mapping: dict) -> str:
    """Replace every placeholder in ``text`` with its real value from ``mapping``."""
    for placeholder, value in mapping.items():
        text = text.replace(placeholder, value)
    return text


def remask_text(text: str, fields: dict) -> str:
    """Inverse of :func:`rehydrate` for internally-produced text.

    Replaces the raw ``name``/``email`` values from ``fields`` back to their
    placeholders, so already-hydrated text (e.g. a diagnosis that the prior node
    rehydrated for the API output) can be fed into a *second* prompt without
    re-exposing the raw PII to the model. Only safe, well-formed values are
    remasked, so a fallback value like ``there`` is never blindly substituted.
    """
    name = str(fields.get("name") or "").strip()
    if name and _name_is_safe(name):
        text = text.replace(name, NAME_PLACEHOLDER)
    email = str(fields.get("email") or "").strip()
    if email and _EMAIL_RE.match(email):
        text = text.replace(email, EMAIL_PLACEHOLDER)
    return text


def unresolved_placeholders(text: str) -> list[str]:
    """Return any ``{{...}}`` placeholders left in ``text`` after re-hydration."""
    return _PLACEHOLDER_RE.findall(text or "")


def scrub_freeform(text: str) -> str:
    """Mask card-shaped digit runs (Luhn-valid) in free-text bound for a prompt.

    Structured, schema-validated values are left intact: invoice/event ids carry
    letters or separators, amounts are short, and ISO dates contain hyphens, so
    none match a 13-19 digit run (with optional spaces or dashes). Only a run
    that also passes Luhn - i.e.
    looks like a real card number - is masked, so real invoice references survive.
    """
    def _mask(m: re.Match) -> str:
        matched = m.group(0)
        # Luhn over DIGITS only. The pattern now tolerates spaces and dashes,
        # and feeding those to the checksum scored them as characters, so a
        # spaced PAN failed the check and shipped unmasked.
        digits = re.sub(r"\D", "", matched)
        return "{{CARD}}" if _luhn_ok(digits) else matched

    return _CARD_RUN_RE.sub(_mask, text or "")


# ---------------------------------------------------------------------------
# Salted pseudonymization for logs/traces (GDPR: never log the raw value)
# ---------------------------------------------------------------------------

#: Env var carrying the per-deployment PII salt. A hash without a secret salt is
#: reversible for a low-entropy value like an email (rainbow table / brute force
#: over a known customer list), so a production deployment MUST set this to a
#: high-entropy random string, held only in the environment (never in code, logs
#: or the repo). See README "Compliance posture".
PII_SALT_ENV = "PAYPILOT_PII_SALT"

#: Fail-closed default used ONLY when PAYPILOT_PII_SALT is unset. We do not ship
#: a bare (unsalted) digest silently: without a salt we substitute this constant
#: AND warn once, so the log line still carries no raw PII while the operator is
#: told, loudly and idempotently, to set a real salt. Tests and the demo path run
#: with the salt unset, so this keeps hashing deterministic there too.
_DEFAULT_PII_SALT = "paypilot-unset-salt-set-PAYPILOT_PII_SALT-in-production"

_salt_warned = False


def _pii_salt() -> str:
    """Return the configured PII salt, or the documented default with a warning.

    Read per call (not cached at import) so a test or a redeploy can change the
    salt without reimporting, mirroring ``safety.allowed_link_hosts``.
    """
    global _salt_warned
    salt = os.getenv(PII_SALT_ENV)
    if salt:
        return salt
    if not _salt_warned:
        _log.warning(
            '{"event": "pii_salt_unset", "detail": "%s is not set; falling back '
            'to the documented default salt. Set %s to a high-entropy secret in '
            'production so hashed PII cannot be reversed."}',
            PII_SALT_ENV,
            PII_SALT_ENV,
        )
        _salt_warned = True
    return _DEFAULT_PII_SALT


def hash_pii(value) -> str:
    """Stable SALTED sha256 hex of a PII value, for logging without the raw value.

    The salt (``PAYPILOT_PII_SALT``) is prepended before hashing so the digest of
    a known value (an email, a customer id) cannot be recovered by a rainbow
    table or a brute force over a candidate list. The output is still stable for
    a given salt, so a hashed id remains a usable correlation key within a
    deployment while never being the raw value.
    """
    salt = _pii_salt()
    return hashlib.sha256(f"{salt}:{str(value or '')}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Allowlist logging (canon rule: guard what you EXTRACT, not what you exclude)
# ---------------------------------------------------------------------------

#: The ONLY fields that may appear VERBATIM in a log line, trace, or audit event.
#: An allowlist, not a denylist: a denylist is correct only until someone adds a
#: new PII field (a phone, a receipt_email) and forgets to add it to the blocked
#: list, at which point it leaks silently. Anything not named here is dropped.
#: These are non-PII, shape-constrained business fields (see app.store schema and
#: app.nodes prompt allowlists - the same doctrine).
LOG_SAFE_FIELDS = frozenset({
    "invoice_id",
    "customer_id",
    "stripe_customer_id",
    "subscription_id",
    "event",
    "event_id",
    "event_type",
    "amount_minor",
    "currency",
    "failure_code",
    "state",
    "from_state",
    "to_state",
    "channel",
    "status",
    "attempt",
    "attempt_count",
    "provider_message_id",
    "node",
    "model",
    "severity",
    "reason",
    "path",
    "count",
    "error_type",
    "duration_ms",
    "ts",
})

#: Keys that are PII and must be HASHED (not dropped) so a log line can still be
#: correlated to a subject without carrying the raw value. Each is emitted as
#: ``<key>_sha256`` via :func:`hash_pii`.
_LOG_HASH_FIELDS = frozenset({"name", "email", "to", "recipient", "customer_email"})


def safe_log_fields(record: dict) -> dict:
    """Reduce an arbitrary dict to only what is safe to log.

    Allowlisted keys pass through verbatim; known-PII keys are replaced by their
    salted hash under ``<key>_sha256``; every other key is DROPPED. This is the
    single gate every structured log/audit event should pass its payload through,
    so a raw name or email can never reach a log, trace, or error path even when
    a new field is added upstream.
    """
    out: dict = {}
    for key, value in (record or {}).items():
        if key in LOG_SAFE_FIELDS:
            out[key] = value
        elif key in _LOG_HASH_FIELDS and value not in (None, ""):
            out[f"{key}_sha256"] = hash_pii(value)
    return out
