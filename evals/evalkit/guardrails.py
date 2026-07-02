"""Deterministic policy checks - the golden rules, as code.

Guardrails are pass/fail, never fuzzy. Each is a small function returning either
``None`` (clean) or a short problem string. :func:`check_guardrails` runs a list
of them over one text and collects the violations into a :class:`GuardrailReport`.

The factories below cover the rules these projects break most often. Compose the
ones you need per project; a couple (no dashes, no unfilled placeholders) belong
in essentially every list:

    from evalkit.guardrails import (
        no_dashes, no_unfilled_placeholders, max_words, must_not_contain,
    )
    GUARDS = [
        no_dashes(),
        no_unfilled_placeholders(),
        max_words(180),
        must_not_contain(["as an ai", "language model"]),
    ]
    report = check_guardrails(text, GUARDS)
    assert report.ok, report.summary()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# A guardrail: (name, predicate). The predicate returns None when clean, else a
# one-line description of what it found.
Guardrail = tuple[str, Callable[[str, dict], Optional[str]]]


@dataclass
class GuardrailReport:
    """Collected guardrail violations for one text."""

    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        if self.ok:
            return "guardrails: ok"
        return "guardrails failed:\n" + "\n".join(f"  - {v}" for v in self.violations)


def check_guardrails(text: str, guards: list[Guardrail], context: Optional[dict] = None) -> GuardrailReport:
    """Run every guardrail over ``text`` and collect the failures."""
    context = context or {}
    report = GuardrailReport()
    for name, fn in guards:
        try:
            problem = fn(text, context)
        except Exception as exc:  # a broken guard is itself a violation
            problem = f"guard errored: {exc}"
        if problem:
            report.violations.append(f"{name}: {problem}")
    return report


# ---------------------------------------------------------------------------
# Built-in guardrail factories
# ---------------------------------------------------------------------------

def no_dashes() -> Guardrail:
    """GLOBAL GOLDEN RULE: plain hyphen only, never em-dash or en-dash."""
    def _fn(text: str, _ctx: dict) -> Optional[str]:
        bad = [d for d in ("—", "–") if d in text]
        if bad:
            names = {"—": "em-dash", "–": "en-dash"}
            return "contains " + ", ".join(names[d] for d in bad) + " (use '-')"
        return None
    return ("no_dashes", _fn)


def no_unfilled_placeholders() -> Guardrail:
    """No template placeholders left in shipped text: {x}, {{x}}, <x>, %x%."""
    pattern = re.compile(r"\{\{?\s*[a-zA-Z0-9_.]+\s*\}?\}|<[a-zA-Z0-9_]+>|%[a-zA-Z0-9_]+%")
    def _fn(text: str, _ctx: dict) -> Optional[str]:
        hits = pattern.findall(text)
        if hits:
            return "unfilled placeholder(s): " + ", ".join(sorted(set(hits))[:5])
        return None
    return ("no_unfilled_placeholders", _fn)


def no_todo_markers() -> Guardrail:
    """No TODO/FIXME/placeholder/"before you go live" notes in a deliverable."""
    markers = ("todo", "fixme", "tbd", "placeholder", "before you go live", "lorem ipsum")
    def _fn(text: str, _ctx: dict) -> Optional[str]:
        low = text.lower()
        found = [m for m in markers if m in low]
        return "left-in note(s): " + ", ".join(found) if found else None
    return ("no_todo_markers", _fn)


def max_words(limit: int) -> Guardrail:
    """Cap length so generated copy stays tight."""
    def _fn(text: str, _ctx: dict) -> Optional[str]:
        n = len(text.split())
        return f"{n} words > {limit}" if n > limit else None
    return (f"max_words_{limit}", _fn)


def min_words(minimum: int) -> Guardrail:
    """Reject empty / stub output."""
    def _fn(text: str, _ctx: dict) -> Optional[str]:
        n = len(text.split())
        return f"{n} words < {minimum}" if n < minimum else None
    return (f"min_words_{minimum}", _fn)


def must_contain(needles: list[str], any_of: bool = False) -> Guardrail:
    """Require substrings (case-insensitive). ``any_of`` = at least one."""
    def _fn(text: str, _ctx: dict) -> Optional[str]:
        low = text.lower()
        present = [n for n in needles if n.lower() in low]
        if any_of:
            return None if present else f"missing all of: {needles}"
        missing = [n for n in needles if n.lower() not in low]
        return f"missing required: {missing}" if missing else None
    return ("must_contain", _fn)


def must_not_contain(needles: list[str]) -> Guardrail:
    """Ban substrings (case-insensitive) - AI tells, banned phrases, etc."""
    def _fn(text: str, _ctx: dict) -> Optional[str]:
        low = text.lower()
        found = [n for n in needles if n.lower() in low]
        return f"contains banned: {found}" if found else None
    return ("must_not_contain", _fn)


def no_disclosure(terms: list[str]) -> Guardrail:
    """GOLDEN RULE: never reveal build mechanics in client-facing copy.

    Pass the stack/mechanic words that must not surface in shipped copy
    (e.g. ["n8n", "supabase", "gemini", "prompt", "webhook", "faiss"]).
    """
    def _fn(text: str, _ctx: dict) -> Optional[str]:
        low = text.lower()
        found = [t for t in terms if t.lower() in low]
        return f"discloses build mechanics: {found}" if found else None
    return ("no_disclosure", _fn)
