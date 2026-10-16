# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
"""Bacs ARUDD table and its guards.

The load-bearing cases are the ones about the SYMBOL SPACE. Bacs runs three
report types over one single-character alphabet and ``0`` means opposite things
in two of them, so the tests that matter are the ones proving a caller cannot
get an answer without saying which report they hold.
"""

from __future__ import annotations

import csv
import importlib.util
import pathlib
import re
import shutil
import sys

import pytest

from app import bacs_return_map as bacs

EXPECTED_CODES = 12
_MODULE_SOURCE = pathlib.Path(bacs.__file__).resolve()


def _import_from(directory: pathlib.Path, name: str):
    """Import the REAL module source from a copy, so TABLE_PATH resolves there.

    The module must be in ``sys.modules`` before ``exec_module``: a frozen
    dataclass resolves its own module during class creation and gets ``None``
    otherwise, which surfaces as an unrelated AttributeError.
    """
    target = directory / "bacs_return_map.py"
    shutil.copy2(_MODULE_SOURCE, target)
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    sys.modules.pop(name, None)
    return module


def _copy_table(tmp_path: pathlib.Path) -> pathlib.Path:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    shutil.copy2(bacs.TABLE_PATH, data / "bacs_return_codes.csv")
    return data / "bacs_return_codes.csv"


def _rewrite(path: pathlib.Path, mutate) -> None:
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    fields = list(rows[0].keys())
    replaced = mutate(rows)
    if replaced is not None:
        rows = replaced
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

def test_the_table_holds_the_twelve_measured_codes():
    codes = {c for c in bacs._TABLE if c != bacs.DEFAULT_KEY}
    assert codes == set("0123456789AB")
    assert len(codes) == EXPECTED_CODES


def test_every_code_is_one_character():
    for code in bacs._TABLE:
        if code != bacs.DEFAULT_KEY:
            assert bacs._CODE_RE.match(code), code


def test_the_csv_parses_to_a_constant_field_count():
    """A comma inside a field silently splits the row and the guards then pass.

    That happened while this table was being written: an unquoted comma in the
    abstain reason gave one row eleven fields against a nine-field header, the
    overflow landed under a ``None`` key, and the invariant checks run at the
    time passed on the mis-parse.
    """
    with bacs.TABLE_PATH.open(newline="") as fh:
        rows = list(csv.reader(fh))
    widths = {len(r) for r in rows if r}
    assert widths == {len(rows[0])}, widths
    with bacs.TABLE_PATH.open(newline="") as fh:
        assert all(None not in r for r in csv.DictReader(fh))


def test_each_decision_row_holds_exactly_one_decision():
    seen: dict[int, tuple[str, str]] = {}
    for row in bacs._TABLE.values():
        n = int(row["decision_row"])
        decision = (row["goes_again"], row["what_has_to_change_first"])
        assert seen.setdefault(n, decision) == decision, n


def test_row_one_is_the_only_row_where_nothing_has_to_change():
    for row in bacs.list_decisions():
        assert (row.what_has_to_change_first == "nothing") == (row.decision_row == 1)


def test_account_closed_lands_on_the_account_details_row_beside_ach_r02():
    assert bacs.lookup_return_code("B", "ARUDD").decision_row == 2


def test_deceased_lands_on_the_nothing_will_change_row():
    decision = bacs.lookup_return_code("2", "ARUDD")
    assert decision.decision_row == 5
    assert decision.goes_again == "no"
    assert decision.needs_a_person_first is True


def test_our_own_entry_row_holds_the_four_codes_we_caused():
    row = {r.decision_row: r for r in bacs.list_decisions()}[4]
    assert row.codes == ("7", "8", "9", "A")
    assert row.what_has_to_change_first == "our own entry"


# --------------------------------------------------------------------------
# the report type, which is the guard this rail exists for
# --------------------------------------------------------------------------

def test_the_report_argument_is_required():
    with pytest.raises(TypeError):
        bacs.lookup_return_code("0")  # type: ignore[call-arg]


def test_an_addacs_code_is_refused_rather_than_answered():
    """``0`` is a live instruction in ARUDD and a cancelled one in ADDACS.

    Answering an ADDACS ``0`` from this table returns "goes again" for a
    cancelled instruction, which is the money-moving wrong answer.
    """
    with pytest.raises(ValueError) as exc:
        bacs.lookup_return_code("0", "ADDACS")
    assert "ADDACS" in str(exc.value)
    assert "symbol space" in str(exc.value)


def test_an_auddis_code_is_refused_rather_than_answered():
    with pytest.raises(ValueError):
        bacs.lookup_return_code("1", "AUDDIS")


def test_an_unknown_report_names_the_known_ones():
    with pytest.raises(ValueError) as exc:
        bacs.lookup_return_code("0", "DDICA")
    for report in bacs.KNOWN_REPORTS:
        assert report in str(exc.value)


def test_the_report_argument_is_case_and_space_insensitive():
    assert bacs.lookup_return_code("0", " arudd ").decision_row == 1


def test_every_colliding_code_warns_which_report_it_collides_with():
    for code in sorted(bacs.COLLIDING_CODES):
        decision = bacs.lookup_return_code(code, "ARUDD")
        assert decision.collides_in, code
        assert "DIFFERENT meaning" in "\n".join(
            bacs.explain(code, "ARUDD").lines
        ), code


def test_a_code_identical_across_reports_carries_no_collision_warning():
    """``2`` and ``B`` mean the same in all three reports, so warning on them
    would be a false positive, and a gate that cries on correct input is one
    somebody switches off."""
    for code in ("2", "B"):
        assert bacs.lookup_return_code(code, "ARUDD").collides_in == ()


def test_a_symbol_belonging_to_another_report_falls_through_and_says_so():
    decision = bacs.lookup_return_code("C", "ARUDD")
    assert decision.matched is False
    assert decision.abstains is True
    lines = "\n".join(bacs.explain("C", "ARUDD").lines)
    assert "IS a Bacs code in another report" in lines


def test_no_other_report_symbol_is_listed_as_an_arudd_code():
    listed = {c for c in bacs._TABLE if c != bacs.DEFAULT_KEY}
    assert bacs.OTHER_REPORT_ONLY & listed == set()


# --------------------------------------------------------------------------
# abstain and fall-through
# --------------------------------------------------------------------------

def test_no_instruction_abstains_because_the_sources_split():
    decision = bacs.lookup_return_code("6", "ARUDD")
    assert decision.abstains is True
    assert decision.decision_row == 6
    assert "split" in (decision.abstain_reason or "")
    assert decision.needs_a_person_first is True


def test_an_unlisted_code_abstains_and_says_it_fell_through():
    decision = bacs.lookup_return_code("Z", "ARUDD")
    assert decision.matched is False
    assert decision.abstains is True
    assert decision.decision_row in bacs.ABSTAIN_ROWS


def test_an_unusable_code_never_returns_none_and_never_reaches_row_one():
    for code in (None, "", "   ", "99", "ARUDD"):
        decision = bacs.lookup_return_code(code, "ARUDD")
        assert decision is not None
        assert decision.decision_row != 1
        assert decision.abstains is True


def test_an_abstain_states_no_decision_rather_than_goes_again():
    lines = "\n".join(bacs.explain("6", "ARUDD").lines)
    assert "No decision:" in lines
    assert "Goes again:" not in lines


def test_refer_to_payer_decides_rather_than_abstains_and_carries_the_relayed_window():
    """The CAUSE of ``0`` is opaque and the DECISION is not.

    Every readable source permits a representation, so abstaining here would
    refuse the most common code in the report and the table would be switched
    off. The uncorroborated retry window rides along as text.
    """
    decision = bacs.lookup_return_code("0", "ARUDD")
    assert decision.abstains is False
    assert decision.decision_row == 1
    lines = "\n".join(bacs.explain("0", "ARUDD").lines)
    assert "RELAYED" in lines
    assert "five working days" in lines


def test_explain_always_states_the_weaker_provenance():
    for code in ("0", "6", "C"):
        assert "publishes no ARUDD reason-code list" in "\n".join(
            bacs.explain(code, "ARUDD").lines
        )


# --------------------------------------------------------------------------
# the validator is on the import path, proven in both directions
# --------------------------------------------------------------------------

def _table_with(mutate) -> dict:
    """The real table as a dict, mutated, for calling ``_validate`` in process.

    These cases run against the module's OWN ``TableError`` rather than a
    copy's. A copied module defines its own exception class, so a test that
    imports a copy and asserts ``bacs.TableError`` cannot catch it: that is
    what the import-path test below is for, and it matches on the message.
    """
    table = {code: dict(row) for code, row in bacs._TABLE.items()}
    mutate(table)
    return table


@pytest.mark.parametrize("mutate,fragment", [
    (lambda t: t.pop("DEFAULT"), "no DEFAULT row"),
    # DEFAULT moved onto a real decision row, carrying that row's own
    # decision so the table stays internally consistent. A mutation that
    # also breaks the one-decision-per-row rule trips the earlier guard and
    # proves nothing about this one.
    (lambda t: t["DEFAULT"].update(
        decision_row="1", goes_again=t["0"]["goes_again"],
        what_has_to_change_first=t["0"]["what_has_to_change_first"],
        abstain_reason=""), "must never do that"),
    (lambda t: t["DEFAULT"].update(
        decision_row="2", goes_again=t["B"]["goes_again"],
        what_has_to_change_first=t["B"]["what_has_to_change_first"],
        abstain_reason=""), "not an abstain row"),
    (lambda t: t["6"].update(abstain_reason=""), "gives no reason"),
    (lambda t: t["0"].update(abstain_reason="because"), "two answers exist"),
    (lambda t: t["0"].update(meaning=""), "has no meaning"),
    (lambda t: t["0"].update(customer_message=""), "no customer_message"),
    (lambda t: t["0"].update(collides_in=""), "does not say which"),
    (lambda t: t.pop("3"), "absent from the table"),
    (lambda t: t["B"].update(goes_again="yes"), "holds two decisions"),
    (lambda t: t.__setitem__("C", dict(t["0"], collides_in="")),
     "belonging to another"),
    (lambda t: t.__setitem__("XX", dict(t["0"])), "is not one character"),
    (lambda t: t["0"].update(decision_row="not a number"),
     "no readable decision_row"),
])
def test_the_validator_refuses_a_broken_table(mutate, fragment):
    with pytest.raises(bacs.TableError, match=re.escape(fragment)):
        bacs._validate(_table_with(mutate))


def test_the_validator_accepts_the_real_table():
    """Positive control. Without it, every case above could be passing because
    ``_validate`` refuses everything rather than because it refuses defects."""
    bacs._validate(_table_with(lambda t: None))


def test_the_validator_is_on_the_import_path(tmp_path):
    """Wiring, not logic: the copy's TableError is a different class, so this
    matches on the message. Commenting out the ``_validate`` call in the module
    reds this row and the one below stays green, which is how the two are told
    apart."""
    table = _copy_table(tmp_path)
    _rewrite(table, lambda rows: [r for r in rows if r["code"] != "DEFAULT"])
    with pytest.raises(Exception, match="no DEFAULT row"):
        _import_from(tmp_path, "bacs_broken_default")


def test_the_import_path_control_can_also_pass(tmp_path):
    """Positive control for the test above: the same copy with the real table
    must import, or a broken fixture and a working guard look identical."""
    _copy_table(tmp_path)
    module = _import_from(tmp_path, "bacs_good_copy")
    assert len([c for c in module._TABLE if c != "DEFAULT"]) == EXPECTED_CODES


def test_a_missing_table_refuses_rather_than_loading_empty(tmp_path):
    with pytest.raises(Exception, match="no Bacs table at"):
        _import_from(tmp_path, "bacs_no_table")
