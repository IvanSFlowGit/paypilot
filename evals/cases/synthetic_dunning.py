"""Synthetic follow-up cases for EVERY failed-payment code at EVERY touch stage, and the rules they must meet.

WHAT "TRAINING ON SYNTHETIC DATA" MEANS HERE, STATED PLAINLY. No model weights are trained. The
customer email is committed, human-reviewed copy (data/templates/dunning.json), so the thing that
improves is the COPY, and the signal that improves it is this grid: every failure code x every
follow-up stage x the tenant caps that change what is true x awkward customer names and plans.
A copy change that breaks a rule here fails the build. Cheap-layer-first and zero-token: inference
writes the copy once at build time, a person reads it, and this checks it on every run for free.

THE GRID IS THE POPULATION, NOT A SAMPLE. It is small enough to run in full on every commit, so no
seed or sampling can hide a cell. ``cells()`` enumerates it and the tests assert its size, so a new
failure code added to the copy library without follow-ups shows up as a failing cell, not silence.

THE RULES ARE ABOUT WHAT IS TRUE, NOT ABOUT TASTE. "Last reminder" may only appear when the tenant's
cap makes it the last; a code where no retry will run may never promise one; a follow-up may not be
longer than the email it follows. Tone is checked only by the cheap, mechanical proxies below and is
otherwise a human read of the copy file, because a regex cannot tell warm from cold.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

# (touch, tenant max_touches) pairs, chosen for what each changes about what is true.
TOUCH_CAPS = [
    (1, 3), (2, 3), (3, 3),     # the default cap
    (2, 2),                     # a cap of two: the second email IS the last
    (3, 5), (4, 5), (5, 5),     # a raised cap: touches 3 and 4 must NOT say last
    (1, 1),                     # a cap of one: no follow-up exists, so nothing may claim to be last
]

#: Failure codes the library carries copy for, plus one the library has never seen (fallback copy).
UNSEEN_CODE = "never_seen_before"

#: Codes where no retry on the stored payment method will run, so no copy may promise one.
NO_RETRY_CODES = frozenset({
    "issuer_do_not_retry", "card_details_invalid", "direct_debit_not_retried", "retries_exhausted",
})

#: Awkward customers. These are what real CRMs and invoice descriptions contain.
CUSTOMERS = [
    ("Ada Lovelace", "Pro"),
    ("O'Brien & Sons", "Scale (annual)"),
    ("Zoe Mueller", "Starter"),
    ("Zoë Müller", "Team"),               # non-ASCII, the failure a template engine hides
    ("", "Pro"),                                  # blank name: greeting must still read
    ("A" * 80, "Pro"),                            # a very long name
    ("Ada Lovelace", ""),                         # blank plan
    ("Ada Lovelace", "{plan}"),                   # brace-shaped plan: must never leak a placeholder
    ("{name}", "Pro"),                            # brace-shaped name
    ("Ada Lovelace", "Plan 50% off $9.99"),       # percent and dollar characters in the plan
]

_BUILD_TERMS = ["faiss", "langgraph", "langchain", "openai", "chatgpt", "embedding", "vector store",
                "api key", "prompt", "mock", " llm"]
_AI_TELLS = ["as an ai", "language model", "i cannot ", "i am unable", "as a large", "delve", "i hope this"]
_DASHES = ("\u2014", "\u2013")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
# A promise of FUTURE automated action: "we'll retry", "we will try again", "will be retried".
_RETRY_PROMISE = re.compile(
    r"(we'?ll|we will|will)\s+(automatically\s+)?(retry|try\b(?!\s+(a|another|different)\b))|will be retried",
    re.IGNORECASE,
)
# A request the customer can act on. Applied to follow-ups only: the first-touch copy is the
# original, human-reviewed text and is held to the universal rules, not to this one.
_CTA = re.compile(r"\bplease\b|\byou can (pay|update|add|re-enter|try)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Cell:
    code: str
    touch: int
    cap: int
    name: str
    plan: str

    @property
    def key(self) -> str:
        return f"{self.code}|t{self.touch}of{self.cap}|{self.name[:10]!r}|{self.plan[:10]!r}"


def cells(codes: list[str]) -> list[Cell]:
    """Every (code x touch/cap x customer) combination. The population, not a sample."""
    return [
        Cell(code, touch, cap, name, plan)
        for code, (touch, cap), (name, plan) in itertools.product(codes + [UNSEEN_CODE], TOUCH_CAPS, CUSTOMERS)
    ]


def words(text: str) -> int:
    return len(text.split())


def ngrams(text: str, n: int = 4) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z']+", text.lower())
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def overlap(a: str, b: str) -> float:
    """Jaccard overlap of word 4-grams: 1.0 is identical, near 0 is a different email."""
    x, y = ngrams(a), ngrams(b)
    return len(x & y) / len(x | y) if (x | y) else 1.0


def check(text: str, subject: str, *, code: str, stage: str, first_words: int | None) -> list[str]:
    """Rule ids this rendered email breaks. Empty means it meets every mechanical rule."""
    bad: list[str] = []
    low = text.lower()
    slow = subject.lower()

    if any(d in text or d in subject for d in _DASHES):
        bad.append("dash")
    if "{" in text or "}" in text or "{" in subject:
        bad.append("unfilled_placeholder")
    if _URL.search(text) or _URL.search(subject):
        bad.append("url_in_copy")            # the single sanctioned link is appended by the sender, never written here
    if any(t in low for t in _BUILD_TERMS):
        bad.append("build_mechanics")
    if any(t in low for t in _AI_TELLS):
        bad.append("ai_tell")
    if not text.startswith("Hello "):
        bad.append("greeting")
    if not 25 <= words(text) <= 220:
        bad.append("length_bounds")
    if stage != "first" and first_words is not None and words(text) > first_words:
        bad.append("follow_up_longer_than_first")
    if stage != "first" and not _CTA.search(text):
        bad.append("no_call_to_action")

    # What is true about "last".
    says_last = "last reminder" in low or "last reminder" in slow
    if stage == "final" and not ("last reminder" in low and "last reminder" in slow):
        bad.append("final_not_marked_last")
    if stage != "final" and says_last:
        bad.append("false_last_reminder")

    # No promise of a retry that will not run, and no promise of any automated action after the last email.
    if code in NO_RETRY_CODES and _RETRY_PROMISE.search(text):
        bad.append("promises_retry_that_will_not_run")
    if stage == "final" and _RETRY_PROMISE.search(text):
        bad.append("final_promises_more_action")
    if stage == "final" and "still running" in low:
        bad.append("final_asserts_service_state")      # we cannot know it stays on after the last email

    # Subject.
    if len(subject) > 60:
        bad.append("subject_too_long")
    want = {"first": "", "reminder": "Reminder: ", "final": "Last reminder: "}[stage]
    if want and not subject.startswith(want):
        bad.append("subject_prefix")
    if stage == "first" and subject.startswith(("Reminder", "Last reminder")):
        bad.append("subject_prefix")
    if "!" in subject or re.search(r"\b[A-Z]{4,}\b", subject) or slow.startswith(("re:", "fw:", "fwd:")):
        bad.append("subject_pressure_or_fake_thread")
    return bad
