# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Speak Bacs ARUDD unpaid Direct Debit reports: one reason code in, one answer out.

Same shape as :mod:`app.ach_return_map` and :mod:`app.sepa_return_map`, and the
same guarantee: no model is consulted, nothing here can be talked out of its
answer by text it reads, and the table is the authority while this module only
looks things up in it.

THE PLAN SAID THIS RAIL SHIPS A REFUSAL. IT DOES NOT.
-----------------------------------------------------
The approved plan for this work said no Bacs-published list is freely readable
and that three secondary sources disagree, one of them asserting a ``C`` the
others do not have, so this module should exist, declare the discrepancy and
refuse at import rather than ship a guessed table.

Measured 2026-10-16, the first half is true and the second half is not.

* The scheme publishes nothing. ``bacs.co.uk/bacs-schemes/direct-debit/
  services/`` returns HTTP 200 and 56,934 bytes, and the control fires: the
  page names "Automated Return of Unpaid Direct Debits Service (ARUDD)"
  verbatim, so it is the right page. It carries zero occurrences of "reason
  code", zero of "refer to payer", and no code list of any kind.
* Four readable secondary sources agree EXACTLY. Twelve codes, ``0`` to ``9``
  plus ``A`` and ``B``, with names agreeing to the letter and differing only
  in capitalisation: docs.acquired.com, wiki.smarterpay.com, gocardless.com
  (where ARUDD is split across four separate tables) and movimo.co.uk. Codes
  were extracted programmatically rather than typed, which matters: a
  hand-typed set dropped a ``6`` during this work and read as a source
  disagreement until it was recomputed.
* The ``C`` is real and is not an ARUDD code. It belongs to ADDACS and to
  AUDDIS. The plan's discrepancy was report-type conflation rather than source
  disagreement, which is why a guessed table was never the only option.

LIMITATION, STATED RATHER THAN SMOOTHED. Four agreeing secondary sources are
not a primary source and they may copy one another. What is measured is that
every readable source agrees, not that the scheme would agree. The ACH table
is built on a published rulebook and the SEPA table on the EPC's own current
guidance with a per-code *Suggested Creditor action* column; this table has
neither, so its decision column is derived from the agreed code MEANINGS
rather than from any scheme's stated action. That is a weaker provenance than
the other two rails and a caller is entitled to know it:
:data:`PROVENANCE` says so and :func:`explain` prints it.

THE REPORT TYPE IS A REQUIRED ARGUMENT, AND THAT IS THE WHOLE GUARD
-------------------------------------------------------------------
Bacs runs three report types over ONE single-character symbol space. Measured
from wiki.smarterpay.com: ARUDD has 12 codes, ADDACS 9, AUDDIS 18, all drawn
from the same alphabet, so a code carries no evidence of which report produced
it.

The collision is the money-moving kind. ``0`` is "Refer To Payer" in ARUDD,
where the instruction is still live and a representation is permitted, and
"Instruction Cancelled - Refer To Payer" in ADDACS, where the authorisation is
gone. Same symbol, opposite answers. ``1`` and ``3`` also differ between ARUDD
and ADDACS, and ``1`` and ``5`` between ARUDD and AUDDIS, while ``2`` and ``B``
mean the same thing in all three.

So an ADDACS ``0`` fed into this table would return "goes again, nothing has to
change" for a cancelled instruction. That is the same class of defect as
retrying a SEPA ``AM05`` duplicate, and no amount of care at the call site
prevents it, because both calls look identical.

:func:`lookup_return_code` therefore takes ``report`` with NO DEFAULT and
refuses any value but ``ARUDD``. A default report type would be exactly the
silent wrong answer this guard exists to stop.

SIX DECISION ROWS, FOR A DIFFERENT REASON FROM SEPA'S
-----------------------------------------------------
The rows are the ACH rows and answer the same question: what has to change
before this debit can exist again. Nothing, the account details, their
instruction, our own entry, or nothing ever will. Row assignments mirror ACH
where the codes mean the same thing, so account closed lands on row 2 beside
``R02`` rather than on row 5, and deceased lands on row 5 beside ``R14``.

SEPA needs a sixth row because the scheme itself refuses to name a reason.
Bacs needs one because the SOURCES SPLIT on a decision:

* ``6`` "No instruction". GoCardless reads it as the payer having no
  instruction set up, which is their instruction to give and row 3. The
  processor and vendor pages read it as a lodgement problem to check at our own
  end, which is our own entry and row 4. Those are opposite instructions, one
  of which forbids re-presenting and one of which requires it, so the code gets
  a reason instead of a decision.
* ``DEFAULT``. An unlisted symbol may be a code from another Bacs report rather
  than an unknown ARUDD code, and the symbol alone cannot say which.

An abstain is not a refusal and it is not a decision. Collapsing it into either
neighbour kills the gate or kills the work, which is why :attr:`Decision.abstains`
is its own field and not an absence.

WHAT THIS MODULE REFUSES TO DO
------------------------------
* It does not decide a code without being told which report it came from, for
  the reason above.
* It does not return ``None`` for a code it has never seen. An unknown symbol
  lands on the abstain row and :attr:`Decision.matched` says it fell through.
  :func:`_validate` asserts DEFAULT is on an abstain row, because an unlisted
  code routed to row 2 would start telling customers their account details are
  wrong on the strength of nothing.
* It does not compute a retry date. One source states that Bacs recommended at
  least five working days notice of a new collection date and a representation
  window of one month from the original processing date. That is RELAYED from a
  single source, is not corroborated by the other three, and is not on the
  Bacs site, so it is carried in :data:`RELAYED_REPRESENTATION_WINDOW` as text
  and nothing here does arithmetic with it.
* It does not treat "Refer to payer" as insufficient funds. The sources are
  explicit that the code covers a bank being unable to pay for reasons that
  include bankruptcy, liquidation and the appointment of a receiver, so the
  CAUSE is opaque. The DECISION is not: every readable source permits a
  representation, which is why this lands on row 1 rather than abstaining. A
  repeated ``0`` against the same payer is a different matter and is the
  caller's history to read, not this table's.
"""

from __future__ import annotations

import csv
import pathlib
import re
from dataclasses import dataclass

TABLE_PATH = pathlib.Path(__file__).resolve().parent / "data" / "bacs_return_codes.csv"

#: The only report this module decides. Not a default: a required argument.
REPORT = "ARUDD"

#: Every Bacs report type that shares the symbol space, for the error message.
KNOWN_REPORTS = ("ARUDD", "ADDACS", "AUDDIS")

DEFAULT_KEY = "DEFAULT"

#: Rows that return a reason instead of a decision.
ABSTAIN_ROWS = frozenset({6})

#: Rows where nothing happens until a named person has looked.
PERSON_REQUIRED_ROWS = frozenset({5, 6})

#: Provenance, printed by :func:`explain`, because this rail is weaker sourced
#: than ACH or SEPA and a caller is entitled to know which it is holding.
PROVENANCE = (
    "Bacs publishes no ARUDD reason-code list. This table is the exact "
    "agreement of four readable secondary sources measured 2026-10-16, and the "
    "decision column is derived from the agreed code meanings rather than from "
    "any scheme-stated action."
)

#: Relayed from movimo.co.uk alone, 2026-10-16. Not corroborated, not computed.
RELAYED_REPRESENTATION_WINDOW = (
    "RELAYED from one source (movimo.co.uk, read 2026-10-16) and not "
    "corroborated: Bacs recommended at least five working days notice of a new "
    "collection date, and a representation up to one month from the original "
    "processing date. Nothing in this module computes a date from this."
)

#: Symbols that are valid Bacs codes in another report and are NOT ARUDD codes.
#: Measured from wiki.smarterpay.com 2026-10-16.
OTHER_REPORT_ONLY = frozenset("CDEFGHIKLMNOPQR")

#: ARUDD symbols whose meaning DIFFERS in another report, so a caller who feeds
#: the wrong report's code in gets a confident wrong answer rather than an error.
#: Measured, with the ARUDD meaning first:
#:   0  Refer to payer          / ADDACS Instruction Cancelled - Refer To Payer
#:   1  Instruction cancelled   / ADDACS and AUDDIS Instruction Cancelled By Payer
#:   3  Account transferred     / ADDACS Instruction Cancelled, Account Transferred
#:   5  No account or wrong type/ AUDDIS No Account
COLLIDING_CODES = frozenset("0135")

#: A Bacs reason code is one character: a digit or an upper-case letter. Unlike
#: ACH's R-prefixed space there is no shape that distinguishes one report's
#: codes from another's, which is the reason ``report`` is required.
_CODE_RE = re.compile(r"^[0-9A-Z]$")


@dataclass(frozen=True)
class Decision:
    """One answer about one returned Direct Debit."""

    code: str
    report: str
    matched: bool
    decision_row: int
    goes_again: str
    what_has_to_change_first: str
    meaning: str
    customer_message: str
    needs_a_person_first: bool
    abstains: bool
    abstain_reason: str | None
    also_valid_in: tuple[str, ...]
    collides_in: tuple[str, ...]


@dataclass(frozen=True)
class DecisionRow:
    """One decision row, for callers that want the shape rather than a lookup."""

    decision_row: int
    goes_again: str
    what_has_to_change_first: str
    codes: tuple[str, ...]
    abstains: bool


@dataclass(frozen=True)
class Explanation:
    """A :class:`Decision` rendered for a person to read."""

    code: str
    lines: tuple[str, ...]


class TableError(RuntimeError):
    """The table on disk is not a table this module will answer from."""


def _load(path: pathlib.Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise TableError(f"no Bacs table at {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise TableError(f"Bacs table at {path} is empty")
    table: dict[str, dict[str, str]] = {}
    for row in rows:
        code = (row.get("code") or "").strip()
        if not code:
            raise TableError(f"Bacs table at {path} has a row with no code")
        if code in table:
            raise TableError(f"Bacs table at {path} lists {code} twice")
        table[code] = {k: (v or "").strip() for k, v in row.items()}
    return table


def _validate(table: dict[str, dict[str, str]]) -> None:
    """Refuse a table this module must not answer from.

    Every refusal here is a defect that would otherwise produce a confident
    wrong answer rather than an error, which is the only kind worth a guard.
    """
    if DEFAULT_KEY not in table:
        raise TableError(
            "Bacs table has no DEFAULT row, so an unlisted code would return "
            "nothing and the caller would read that as no problem"
        )

    rows_seen: dict[int, tuple[str, str]] = {}
    for code, row in table.items():
        if code != DEFAULT_KEY and not _CODE_RE.match(code):
            raise TableError(
                f"Bacs code {code!r} is not one character; a Bacs reason code is "
                "a single digit or upper-case letter"
            )
        try:
            decision_row = int(row["decision_row"])
        except (KeyError, ValueError) as exc:
            raise TableError(f"Bacs {code} has no readable decision_row") from exc

        decision = (row.get("goes_again", ""), row.get("what_has_to_change_first", ""))
        if decision_row in rows_seen and rows_seen[decision_row] != decision:
            raise TableError(
                f"decision row {decision_row} holds two decisions, "
                f"{rows_seen[decision_row]} and {decision}, so it is not one decision"
            )
        rows_seen[decision_row] = decision

        abstains = decision_row in ABSTAIN_ROWS
        if abstains and not row.get("abstain_reason"):
            raise TableError(
                f"Bacs {code} is on abstain row {decision_row} and gives no reason, "
                "so it would refuse to decide without saying why"
            )
        if not abstains and row.get("abstain_reason"):
            raise TableError(
                f"Bacs {code} carries an abstain reason on deciding row "
                f"{decision_row}, so two answers exist for one code"
            )
        if not row.get("meaning"):
            raise TableError(
                f"Bacs {code} has no meaning, and the agreed meaning is the only "
                "evidence this table has: the scheme publishes no action column"
            )
        if not row.get("customer_message"):
            raise TableError(f"Bacs {code} has no customer_message to send")

    default_row = int(table[DEFAULT_KEY]["decision_row"])
    if default_row == 1:
        raise TableError(
            "DEFAULT is on row 1, so an unlisted code would be represented "
            "automatically. It must never do that"
        )
    if default_row not in ABSTAIN_ROWS:
        raise TableError(
            f"DEFAULT is on row {default_row}, which is not an abstain row, so an "
            "unlisted symbol would be given a decision the sources do not support"
        )

    listed = {c for c in table if c != DEFAULT_KEY}

    missing_collisions = COLLIDING_CODES - listed
    if missing_collisions:
        raise TableError(
            f"codes {sorted(missing_collisions)} are recorded as colliding with "
            "another Bacs report and are absent from the table, so the guard "
            "names a code this module cannot answer about"
        )
    for code in sorted(COLLIDING_CODES):
        if not table[code].get("collides_in"):
            raise TableError(
                f"Bacs {code} collides with another report's code of the same "
                "symbol and the table does not say which, so a caller cannot be "
                "warned about the one error that returns a wrong decision"
            )

    overlap = OTHER_REPORT_ONLY & listed
    if overlap:
        raise TableError(
            f"symbols {sorted(overlap)} are recorded as belonging to another "
            "Bacs report only, and are also listed as ARUDD codes. One of the "
            "two is wrong and the collision guard cannot be trusted either way"
        )

    if REPORT not in KNOWN_REPORTS:
        raise TableError(f"{REPORT} is not one of the known Bacs reports")


_TABLE = _load(TABLE_PATH)
_validate(_TABLE)


def normalise(code: object) -> str:
    """Upper-case and strip a code. Never raises: an unusable code falls through."""
    if code is None:
        return ""
    return str(code).strip().upper()


def _split(value: str) -> tuple[str, ...]:
    return tuple(v for v in value.split() if v)


def _decision_from(code: str, row: dict[str, str], *, matched: bool,
                   report: str) -> Decision:
    decision_row = int(row["decision_row"])
    abstains = decision_row in ABSTAIN_ROWS
    return Decision(
        code=code,
        report=report,
        matched=matched,
        decision_row=decision_row,
        goes_again=row["goes_again"],
        what_has_to_change_first=row["what_has_to_change_first"],
        meaning=row["meaning"],
        customer_message=row["customer_message"],
        needs_a_person_first=decision_row in PERSON_REQUIRED_ROWS,
        abstains=abstains,
        abstain_reason=row["abstain_reason"] or None if abstains else None,
        also_valid_in=_split(row.get("also_valid_in", "")),
        collides_in=_split(row.get("collides_in", "")),
    )


def lookup_return_code(code: object, report: str) -> Decision:
    """Answer one returned Direct Debit.

    ``report`` has no default on purpose. Bacs runs ARUDD, ADDACS and AUDDIS
    over one single-character symbol space, and ``0`` means "refer to payer,
    the instruction is live" in ARUDD and "instruction cancelled" in ADDACS. A
    default would silently answer the wrong question for a caller holding the
    wrong report, and both calls would look identical.
    """
    given = normalise(report)
    if given != REPORT:
        if given in KNOWN_REPORTS:
            raise ValueError(
                f"this module decides {REPORT} only; {given} shares the same "
                "one-character symbol space and its codes mean different things, "
                f"so a {given} code cannot be answered from the {REPORT} table"
            )
        raise ValueError(
            f"report must be {REPORT!r}; got {report!r}. The known Bacs reports "
            f"are {', '.join(KNOWN_REPORTS)}"
        )

    key = normalise(code)
    row = _TABLE.get(key)
    if row is not None:
        return _decision_from(key, row, matched=True, report=REPORT)
    return _decision_from(key, _TABLE[DEFAULT_KEY], matched=False, report=REPORT)


def list_decisions() -> tuple[DecisionRow, ...]:
    """Every decision row with the codes that land on it, lowest row first."""
    grouped: dict[int, list[str]] = {}
    shape: dict[int, tuple[str, str]] = {}
    for code, row in _TABLE.items():
        if code == DEFAULT_KEY:
            continue
        decision_row = int(row["decision_row"])
        grouped.setdefault(decision_row, []).append(code)
        shape[decision_row] = (row["goes_again"], row["what_has_to_change_first"])
    return tuple(
        DecisionRow(
            decision_row=n,
            goes_again=shape[n][0],
            what_has_to_change_first=shape[n][1],
            codes=tuple(sorted(grouped[n])),
            abstains=n in ABSTAIN_ROWS,
        )
        for n in sorted(grouped)
    )


def explain(code: object, report: str) -> Explanation:
    """A decision rendered for a person, provenance and collisions included."""
    decision = lookup_return_code(code, report)
    lines = [f"{decision.report} {decision.code}: {decision.meaning}"]
    if not decision.matched:
        lines.append(
            f"Not a listed {REPORT} code, so this fell through to the default row."
        )
        if decision.code in OTHER_REPORT_ONLY:
            lines.append(
                f"{decision.code} IS a Bacs code in another report. Check which "
                "report this came from before deciding anything."
            )
    if decision.abstains:
        lines.append(f"No decision: {decision.abstain_reason}")
    else:
        lines.append(f"Goes again: {decision.goes_again}")
        lines.append(f"Has to change first: {decision.what_has_to_change_first}")
    lines.append(f"To the customer: {decision.customer_message}")
    if decision.needs_a_person_first:
        lines.append("A named person looks before anything is sent.")
    if decision.collides_in:
        lines.append(
            f"WARNING: the symbol {decision.code} also exists in "
            f"{' and '.join(decision.collides_in)} with a DIFFERENT meaning. "
            "This answer is only correct if the report really was " + REPORT + "."
        )
    if decision.code == "0" and decision.matched:
        lines.append(RELAYED_REPRESENTATION_WINDOW)
    lines.append(f"Provenance: {PROVENANCE}")
    return Explanation(code=decision.code, lines=tuple(lines))
