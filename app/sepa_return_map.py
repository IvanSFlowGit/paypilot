# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Speak SEPA Direct Debit R-transactions: one reason code in, one answer out.

Same shape as :mod:`app.ach_return_map` and the same guarantee: no model is
consulted, nothing here can be talked out of its answer by text it reads, and
the table is the authority while this module only looks things up in it.

SOURCE, READ FIRST HAND
-----------------------
European Payments Council, *Guidance On Reason Codes for SDD R-transactions*,
EPC173-14 / Version 8.1 / Date of Publication: 30 September 2026, 13 pages,
marked Public. Fetched 2026-10-16 from the EPC's own static asset path, HTTP
200, 442,296 bytes, ``application/pdf``, sha256
``d9a316461b5123d1d1e9b7725eafa446e8b8c295cc4f03679c18ae8bd9c4a0fc``. Text
extracted with ``pdftotext -layout`` to 37,328 characters; the positive
controls (the document title, ``EPC173-14``, ``Version 8.1``) all fired and a
nonsense control returned zero, so the extraction is a real read rather than a
failed one wearing a result's clothes.

The decision column is derived from the document's own *Suggested Creditor
action* column, which is carried in the table verbatim beside every row, so
the derivation is checkable against the scheme rather than against anybody's
reasoning. That column is the reason this rail can ship a table at all.

SIX DECISION ROWS, NOT FIVE
---------------------------
The ACH table sorts its codes into five rows by the one question that decides
what happens next: what has to change before this debit can exist again.
Nothing, the account details, their instruction, our own entry, or nothing
ever will. SEPA needs a sixth, and the sixth is the point.

``MS03`` exists because national data protection law forbids the Debtor PSP
from naming the real reason. The document states what it stands in for:
``AC04``, ``AM04``, ``MD07``, ``RR01``, ``RR02``, ``RR03`` and ``RR04``. Those
land on rows 2, 1, 5 and 4 respectively, which is to say the real answer spans
retry-nothing-changed through to nothing-will-ever-change. A code whose
possible meanings span the whole table cannot be given a decision, so row 6
ABSTAINS: it returns a reason for not deciding rather than a decision. ``ED05``
refers the action to a service level agreement between two parties we are not,
and ``RR04`` is defined as a regulatory reason OTHER than the named ones, so
both abstain for their own stated reasons too.

An abstain is not a refusal and it is not a decision. Collapsing it into either
neighbour kills the gate or kills the work, which is why :attr:`Decision.abstains`
is its own field and not an absence.

WHAT THIS MODULE REFUSES TO DO
------------------------------
* It does not retry a duplicate. ``AM05`` means somebody thinks the collection
  was already sent, so the money may already have been taken once and a retry
  would take it again. It lands on row 4 and nothing goes automatically.
* It does not return ``None`` for a code it has never seen. An unknown code
  lands on the abstain row, and :attr:`Decision.matched` says it fell through
  rather than matched. :func:`_validate` asserts DEFAULT is on an abstain row,
  because an unlisted code routed to row 2 would start telling customers their
  account details are wrong on the strength of nothing.
* It does not assert the AC13/AG01 relation in both directions. The document
  states one: AG01 must not be used where AC13 applies. The reverse is not
  written there, so it is not written here.
* It does not guess a code's shape. SEPA codes are NOT all two letters and two
  digits: ``CNOR`` and ``DNOR`` are four letters, and a pattern written as
  ``[A-Z]{2}[0-9]{2}`` silently loses both. That pattern was written during
  this work, reported 23 codes where the document has 25, and the two missing
  rows were found by reading rather than by matching.

Nothing in here writes, and nothing in here opens a socket.
"""

from __future__ import annotations

import csv
import pathlib
import re
from dataclasses import dataclass, field

#: The source table, hand-built from the v8.1 document named in the docstring.
TABLE_PATH = pathlib.Path(__file__).resolve().parent / "data" / "sepa_return_codes.csv"

#: The row an unlisted code lands on. Never row 1, and always an abstain row.
DEFAULT_KEY = "DEFAULT"

#: Rows that return a reason for not deciding rather than a decision.
ABSTAIN_ROWS = frozenset({6})

#: Rows on which a named person acts before anything automatic. Row 5 closes an
#: agreement with a deceased customer's estate; row 6 does not know what
#: happened. Both are restatements of the table rather than new claims.
PERSON_REQUIRED_ROWS = frozenset({5, 6})

#: Permitted values of the scheme_scope column.
SCOPES = frozenset({"core", "b2b", "both"})

#: Verbatim from EPC173-14 v8.1, page 6, in the AG01 row's use-case column.
#: Carried whole because a paraphrase of a must-not is a different rule.
AG01_IMPORTANT_NOTE = (
    "Important note: AG01 cannot be used in the situation of an SDD B2B "
    "collection presented to a Debtor account that is a consumer account. "
    "The code AC13 needs to be used."
)

#: What MS03 stands in for, as the document itself lists it. Checked against
#: the table at import so it cannot drift away from the sentence it came from.
MS03_STANDS_IN_FOR: tuple[str, ...] = (
    "AC04",
    "AM04",
    "MD07",
    "RR01",
    "RR02",
    "RR03",
    "RR04",
)

#: Both shapes the scheme uses. The four letter form is not an exception to
#: hunt for later; it is two of the twenty five codes.
_CODE_RE = re.compile(r"^(?:[A-Z]{2}[0-9]{2}|[A-Z]{4})$")


@dataclass(frozen=True)
class Decision:
    """One code, one answer, and whether that answer is a decision at all."""

    code: str
    decision_row: int
    goes_again: str
    what_has_to_change_first: str
    r_transaction_types: tuple[str, ...]
    scheme_scope: str
    suggested_creditor_action: str
    customer_message: str
    person_required: bool
    abstains: bool
    abstain_reason: str | None
    matched: bool
    fallback_reason: str | None = None
    must_not_be_used_for: str | None = None


@dataclass(frozen=True)
class DecisionRow:
    """One of the six rows, with the codes that land on it."""

    decision_row: int
    goes_again: str
    what_has_to_change_first: str
    person_required: bool
    abstains: bool
    codes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Explanation:
    """Which row fired, why, and what the scheme itself suggests."""

    code: str
    matched: bool
    decision_row: int
    why: str
    person_required: bool
    abstains: bool
    suggested_creditor_action: str


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
    default_row = int(table[DEFAULT_KEY]["decision_row"])
    if default_row == 1:
        raise TableError("the DEFAULT row lands on row 1; it must never do that")
    if default_row not in ABSTAIN_ROWS:
        raise TableError(
            f"the DEFAULT row lands on row {default_row}, which is not an "
            "abstain row. An unlisted code would then be answered with a "
            "decision nothing supports"
        )

    # goes_again and what_has_to_change_first are constant within a row, or the
    # row is not one decision and list_decisions describes a set that does not
    # exist.
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

    for code, row in table.items():
        scope = row["scheme_scope"].strip()
        if scope not in SCOPES:
            raise TableError(f"{code} has scheme_scope {scope!r}, not one of {sorted(SCOPES)}")

        # An abstain row carries its reason and no other row may carry one. A
        # row-6 answer whose reason is blank would read as a decision that
        # happens to say nothing, which is the state this row exists to avoid.
        reason = (row.get("abstain_reason") or "").strip()
        if int(row["decision_row"]) in ABSTAIN_ROWS:
            if not reason:
                raise TableError(f"{code} abstains and gives no reason for abstaining")
        elif reason:
            raise TableError(
                f"{code} does not abstain and carries an abstain_reason, so two "
                "answers exist for it"
            )

        # Every code the scheme suggests an action for carries that action, and
        # the derivation of the decision row rests on it.
        if code != DEFAULT_KEY and not row["suggested_creditor_action"].strip():
            raise TableError(f"{code} carries no suggested_creditor_action to derive from")

        target = (row.get("must_not_be_used_for") or "").strip()
        if target and target not in table:
            raise TableError(
                f"{code} must not be used for {target}, which is not in this table"
            )

    # MS03 is the reason row 6 exists. If the codes it stands in for stopped
    # spanning more than one decision row, a single answer would be available
    # and the abstain would be over-cautious rather than honest.
    missing = [c for c in MS03_STANDS_IN_FOR if c not in table]
    if missing:
        raise TableError(f"MS03_STANDS_IN_FOR names {missing}, absent from the table")
    spanned = {int(table[c]["decision_row"]) for c in MS03_STANDS_IN_FOR}
    if len(spanned) < 2:
        raise TableError(
            "the codes MS03 stands in for now all land on one decision row "
            f"({spanned}), so MS03 no longer needs to abstain"
        )
    if int(table["MS03"]["decision_row"]) not in ABSTAIN_ROWS:
        raise TableError("MS03 is not on an abstain row")


_TABLE = _load(TABLE_PATH)
_validate(_TABLE)

#: Every code the table lists, DEFAULT excluded. A tuple something reads.
CODES: tuple[str, ...] = tuple(sorted(c for c in _TABLE if c != DEFAULT_KEY))


def normalise(code: object) -> str:
    """``ac01`` and ``' AC01 '`` are the same code; anything else is not one."""
    if not isinstance(code, str):
        raise TableError(f"a reason code is a string, got {type(code).__name__}")
    cleaned = code.strip().upper()
    if not cleaned:
        raise TableError("a reason code cannot be empty")
    return cleaned


def _decision_from(code: str, row: dict[str, str], *, matched: bool,
                   fallback_reason: str | None) -> Decision:
    decision_row = int(row["decision_row"])
    types = tuple(t for t in row["r_transaction_types"].split(";") if t)
    reason = (row.get("abstain_reason") or "").strip()
    target = (row.get("must_not_be_used_for") or "").strip()
    return Decision(
        code=code,
        decision_row=decision_row,
        goes_again=row["goes_again"].strip(),
        what_has_to_change_first=row["what_has_to_change_first"].strip(),
        r_transaction_types=types,
        scheme_scope=row["scheme_scope"].strip(),
        suggested_creditor_action=row["suggested_creditor_action"].strip(),
        customer_message=row["customer_message"].strip(),
        person_required=decision_row in PERSON_REQUIRED_ROWS,
        abstains=decision_row in ABSTAIN_ROWS,
        abstain_reason=reason or None,
        matched=matched,
        fallback_reason=fallback_reason,
        must_not_be_used_for=target or None,
    )


def lookup_return_code(code: object) -> Decision:
    """One code in, one answer out. Never ``None``, never a guess."""
    cleaned = normalise(code)
    row = _TABLE.get(cleaned)
    if row is not None:
        return _decision_from(cleaned, row, matched=True, fallback_reason=None)
    shaped = (
        "looks like a reason code"
        if _CODE_RE.match(cleaned)
        else "is not shaped like a reason code"
    )
    return _decision_from(
        cleaned,
        _TABLE[DEFAULT_KEY],
        matched=False,
        fallback_reason=(
            f"{cleaned} is not in the EPC173-14 v8.1 table, so it fell through "
            f"to the abstain row rather than matching one. It {shaped}."
        ),
    )


def list_decisions() -> tuple[DecisionRow, ...]:
    """The six rows, each with the codes that land on it."""
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
                abstains=number in ABSTAIN_ROWS,
                codes=tuple(sorted(by_row[number])),
            )
        )
    return tuple(rows)


def explain(code: object) -> Explanation:
    """Which row fired and why, so the answer can be reconstructed."""
    decision = lookup_return_code(code)
    if decision.abstains:
        why = (
            f"Row {decision.decision_row}. No decision: {decision.abstain_reason}."
        )
    else:
        why = (
            f"Row {decision.decision_row}. Goes again: {decision.goes_again}. "
            f"What has to change first: {decision.what_has_to_change_first}."
        )
    if not decision.matched:
        why = f"{decision.fallback_reason} {why}"
    if decision.person_required:
        why += " A named person acts before anything automatic."
    if decision.must_not_be_used_for:
        why += (
            f" The scheme states this code must not be used where "
            f"{decision.must_not_be_used_for} applies: {AG01_IMPORTANT_NOTE}"
        )
    return Explanation(
        code=decision.code,
        matched=decision.matched,
        decision_row=decision.decision_row,
        why=why,
        person_required=decision.person_required,
        abstains=decision.abstains,
        suggested_creditor_action=decision.suggested_creditor_action,
    )
