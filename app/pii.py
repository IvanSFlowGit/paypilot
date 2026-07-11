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
import re

from app.safety import find_foreign_urls, find_secrets

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
_CARD_RUN_RE = re.compile(r"\b\d{13,19}\b")     # card-shaped candidate for Luhn


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
    none match a bare 13-19 digit run. Only a run that also passes Luhn - i.e.
    looks like a real card number - is masked, so real invoice references survive.
    """
    def _mask(m: re.Match) -> str:
        digits = m.group(0)
        return "{{CARD}}" if _luhn_ok(digits) else digits

    return _CARD_RUN_RE.sub(_mask, text or "")


def hash_pii(value) -> str:
    """Stable sha256 hex of a PII value, for logging without the raw value."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
