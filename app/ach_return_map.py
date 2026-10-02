# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Speak ACH returns: one return code in, one deterministic decision out.

No model is consulted and nothing here can be talked out of its answer by text
it reads. The table is the authority; this module only looks things up in it.

The table sorts seventeen ACH return codes into FIVE decision rows, because
there are five answers to the one question that decides what happens next:
what has to change before this debit can exist again. Nothing, the account
details, their instruction, our own entry, or nothing ever will. A table with
one row per code is a reference document and the registrar publishes that for
free; a table with one row per decision is a rule set.

Source of the mapping: ``app/data/ach_return_codes.csv``, which is the file the
work was done in, copied in byte for byte. It is parsed at import and the parse
refuses rather than guesses, so a malformed or truncated table fails loudly at
start up instead of answering wrongly all day.

THREE THINGS THIS MODULE REFUSES TO DO, each because the honest answer is
narrower than the convenient one:

* It does not report a return code FAMILY unless the source table states one.
  The Nacha Operating Rules are sold rather than published, so a family
  asserted from memory is an unverified claim, and sixteen of them would be
  sixteen. ``R11`` is the one code whose family the source states, and it is
  the one that matters: it arrives in the unauthorized family and is still
  correctable, which is the single fact in the table that is hard to know.
* It does not invent a value for ``needs_a_person_first``, which is present as
  a column in the source and EMPTY on every row. ``person_required`` is derived
  instead, from the decision row, by the rule stated at
  :data:`PERSON_REQUIRED_ROWS`. :func:`_validate` asserts the source column is
  still empty, so if anybody ever fills it in the import fails rather than
  silently disagreeing with the derivation.
* It does not return ``None`` for a code it has never seen. An unknown code
  lands on the conservative row, where nothing automatic happens, and the
  answer says in :attr:`Decision.matched` that it fell through rather than
  matched. A caller that cannot tell a match from a fallback has been handed a
  guess dressed as an answer.

Nothing in here writes, and nothing in here opens a socket.
"""

from __future__ import annotations

import csv
import pathlib
import re
from dataclasses import dataclass, field

#: The source table, copied byte for byte from the file the work was done in.
TABLE_PATH = pathlib.Path(__file__).resolve().parent / "data" / "ach_return_codes.csv"

#: The row an unlisted code lands on. Never row 1.
DEFAULT_KEY = "DEFAULT"

#: Decision rows on which a named person acts before anything automatic. Row 5
#: is the row whose own message says "nothing automatic. A named person writes
#: that day", so the derivation is a restatement of the table rather than a new
#: claim. Kept deliberately narrow: row 4 needs our own entry corrected, which
#: a person does, and the source table does not say so, so it is not asserted.
PERSON_REQUIRED_ROWS = frozenset({5})

#: Families the SOURCE TABLE states, not families recalled from a rulebook.
#: Every entry is checked against the source text at import by :func:`_validate`,
#: so this cannot drift away from the file it claims to be reading.
FAMILY_STATED: dict[str, str] = {"R11": "unauthorized"}

#: What a caller is told where the table states no family.
FAMILY_UNSTATED = (
    "not stated in this table; the published rulebook is sold rather than "
    "free, so no family is asserted here"
)

_CODE_RE = re.compile(r"^[A-Z]{1}[0-9]{2}$")


@dataclass(frozen=True)
class Decision:
    """One code, one decision, plus whether it was matched or fell through."""

    code: str
    decision_row: int
    goes_again: str
    what_has_to_change_first: str
    customer_message: str
    person_required: bool
    matched: bool
    fallback_reason: str | None = None


@dataclass(frozen=True)
class DecisionRow:
    """One of the five rows, with the codes that land on it."""

    decision_row: int
    goes_again: str
    what_has_to_change_first: str
    person_required: bool
    codes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Explanation:
    """Which row fired, why, and what the table does and does not state."""

    code: str
    matched: bool
    decision_row: int
    why: str
    person_required: bool
    family: str | None
    family_note: str


class TableError(RuntimeError):
    """The source table is missing, malformed, or has stopped agreeing."""


def _load(path: pathlib.Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise TableError(f"decision table missing at {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise TableError(f"decision table at {path} parsed to zero rows")
    table: dict[str, dict[str, str]] = {}
    for row in rows:
        code = (row.get("code") or "").strip()
        if not code:
            raise TableError("decision table has a row with no code")
        if code in table:
            raise TableError(f"decision table lists {code} twice")
        table[code] = row
    return table


def _validate(table: dict[str, dict[str, str]]) -> None:
    """Refuse to serve a table that has stopped agreeing with this module."""
    if DEFAULT_KEY not in table:
        raise TableError("decision table has no DEFAULT row to fall through to")
    if int(table[DEFAULT_KEY]["decision_row"]) == 1:
        raise TableError("the DEFAULT row lands on row 1; it must never do that")

    # The person-required column is empty in the source, which is why
    # person_required is derived. If it is ever filled in, two answers exist and
    # this import is the place that must notice, not a caller months later.
    filled = [c for c, r in table.items() if (r.get("needs_a_person_first") or "").strip()]
    if filled:
        raise TableError(
            "needs_a_person_first is now populated for "
            f"{sorted(filled)}; the derivation in PERSON_REQUIRED_ROWS is no "
            "longer the only answer and must be reconciled by hand"
        )

    # goes_again and what_has_to_change_first must be constant within a row, or
    # the row is not one decision and list_decisions would be describing a set
    # that does not exist.
    seen: dict[int, tuple[str, str]] = {}
    for code, row in table.items():
        key = int(row["decision_row"])
        pair = (row["goes_again"].strip(), row["what_has_to_change_first"].strip())
        if key in seen and seen[key] != pair:
            raise TableError(
                f"row {key} is not one decision: {seen[key]} and {pair} both "
                f"appear in it, the second at {code}"
            )
        seen.setdefault(key, pair)

    # Every family claimed above must be present in the source text for that
    # code, so FAMILY_STATED cannot outlive the sentence it came from.
    for code, family in FAMILY_STATED.items():
        if code not in table:
            raise TableError(f"FAMILY_STATED names {code}, absent from the table")
        text = table[code].get("customer_message", "").lower()
        if family.lower() not in text:
            raise TableError(
                f"FAMILY_STATED says {code} is {family!r}, and the source text "
                "for that code no longer says so"
            )


_TABLE = _load(TABLE_PATH)
_validate(_TABLE)

#: Every code the table lists, DEFAULT excluded. A tuple something reads.
CODES: tuple[str, ...] = tuple(sorted(c for c in _TABLE if c != DEFAULT_KEY))


def normalise(code: object) -> str:
    """``r01`` and ``' R01 '`` are the same code; anything else is not a code."""
    if not isinstance(code, str):
        raise TableError(f"a return code is a string, got {type(code).__name__}")
    cleaned = code.strip().upper()
    if not cleaned:
        raise TableError("a return code cannot be empty")
    return cleaned


def _decision_from(code: str, row: dict[str, str], *, matched: bool,
                   fallback_reason: str | None) -> Decision:
    decision_row = int(row["decision_row"])
    return Decision(
        code=code,
        decision_row=decision_row,
        goes_again=row["goes_again"].strip(),
        what_has_to_change_first=row["what_has_to_change_first"].strip(),
        customer_message=row["customer_message"].strip(),
        person_required=decision_row in PERSON_REQUIRED_ROWS,
        matched=matched,
        fallback_reason=fallback_reason,
    )


def lookup_return_code(code: object) -> Decision:
    """One code in, one decision out. Never ``None``, never a guess."""
    cleaned = normalise(code)
    row = _TABLE.get(cleaned)
    if row is not None:
        return _decision_from(cleaned, row, matched=True, fallback_reason=None)
    shaped = "looks like a return code" if _CODE_RE.match(cleaned) else "is not shaped like a return code"
    return _decision_from(
        cleaned,
        _TABLE[DEFAULT_KEY],
        matched=False,
        fallback_reason=(
            f"{cleaned} is not in this table, so it fell through to the "
            f"conservative row rather than matching one. It {shaped}."
        ),
    )


def list_decisions() -> tuple[DecisionRow, ...]:
    """The five rows, each with the codes that land on it."""
    by_row: dict[int, list[str]] = {}
    for code in CODES:
        by_row.setdefault(int(_TABLE[code]["decision_row"]), []).append(code)
    rows = []
    for number in sorted(by_row):
        example = _TABLE[by_row[number][0]]
        rows.append(
            DecisionRow(
                decision_row=number,
                goes_again=example["goes_again"].strip(),
                what_has_to_change_first=example["what_has_to_change_first"].strip(),
                person_required=number in PERSON_REQUIRED_ROWS,
                codes=tuple(sorted(by_row[number])),
            )
        )
    return tuple(rows)


def explain(code: object) -> Explanation:
    """Which row fired and why, so the answer can be reconstructed."""
    decision = lookup_return_code(code)
    why = (
        f"Row {decision.decision_row}. Goes again: {decision.goes_again}. "
        f"What has to change first: {decision.what_has_to_change_first}."
    )
    if not decision.matched:
        why = f"{decision.fallback_reason} {why}"
    if decision.person_required:
        why += " A named person acts before anything automatic."
    family = FAMILY_STATED.get(decision.code) if decision.matched else None
    return Explanation(
        code=decision.code,
        matched=decision.matched,
        decision_row=decision.decision_row,
        why=why,
        person_required=decision.person_required,
        family=family,
        family_note=(
            f"the source table states this code arrives in the {family} family"
            if family
            else FAMILY_UNSTATED
        ),
    )
