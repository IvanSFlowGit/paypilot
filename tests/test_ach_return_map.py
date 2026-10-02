"""The ACH return-code decision table: every code, and every validator failing.

The suite reads the CSV ITSELF rather than restating what it expects, so an edit
to the table fails the tests rather than waiting for somebody to remember to
update them. A test written against a literal the author typed out tests the
author's expectation, which is the one thing already known.

Half of this file points at the validators. A guard that has only ever passed
has been run, not tested, so each invariant in ``_validate`` is broken on purpose
here and watched to go red: a DEFAULT row on row 1, the person column filled in,
a row holding two different decisions, and a stated family whose sentence has
gone. Deleting any one of those checks turns a case here red.
"""

from __future__ import annotations

import csv
import socket

import pytest

from app import ach_return_map as m

#: Read the source table a second time, independently of the module, so these
#: assertions are against the FILE and not against the module's own parse.
with m.TABLE_PATH.open(newline="", encoding="utf-8") as _handle:
    SOURCE = {r["code"]: r for r in csv.DictReader(_handle)}

LISTED = sorted(c for c in SOURCE if c != m.DEFAULT_KEY)


def _table_without(**overrides):
    """A copy of the parsed table with one or more rows replaced."""
    copy = {code: dict(row) for code, row in m._TABLE.items()}
    for code, changes in overrides.items():
        copy.setdefault(code, dict(copy[m.DEFAULT_KEY]))
        copy[code].update(changes)
    return copy


# --- the table itself -------------------------------------------------------


def test_source_table_has_the_rows_the_module_loaded():
    assert sorted(m.CODES) == LISTED
    assert m.DEFAULT_KEY in SOURCE


@pytest.mark.parametrize("code", LISTED)
def test_every_listed_code_returns_its_recorded_row(code):
    """Not a sample. All of them, compared against the CSV."""
    decision = m.lookup_return_code(code)
    assert decision.matched is True
    assert decision.decision_row == int(SOURCE[code]["decision_row"])
    assert decision.goes_again == SOURCE[code]["goes_again"].strip()
    assert decision.what_has_to_change_first == SOURCE[code]["what_has_to_change_first"].strip()
    assert decision.customer_message == SOURCE[code]["customer_message"].strip()
    assert decision.fallback_reason is None


def test_unknown_code_lands_on_the_default_row_and_says_it_fell_through():
    decision = m.lookup_return_code("R99")
    assert decision.decision_row == int(SOURCE[m.DEFAULT_KEY]["decision_row"])
    assert decision.decision_row != 1
    assert decision.matched is False
    assert decision.fallback_reason
    assert "fell through" in decision.fallback_reason


def test_unknown_code_is_never_none_and_never_raises():
    for junk in ("ZZZ", "R", "banana", "R011", "0"):
        decision = m.lookup_return_code(junk)
        assert decision is not None
        assert decision.matched is False
        assert decision.decision_row != 1


@pytest.mark.parametrize("given", ["r01", " R01 ", "\tr01\n", "R01"])
def test_case_and_whitespace_resolve_to_the_same_code(given):
    assert m.lookup_return_code(given) == m.lookup_return_code("R01")


def test_empty_and_non_string_codes_are_refused_rather_than_guessed():
    for bad in ("", "   ", None, 11, ["R01"]):
        with pytest.raises(m.TableError):
            m.lookup_return_code(bad)


# --- the five rows ----------------------------------------------------------


def test_list_decisions_returns_five_rows_covering_every_code_once():
    rows = m.list_decisions()
    assert len(rows) == 5
    assert [r.decision_row for r in rows] == [1, 2, 3, 4, 5]

    seen: list[str] = []
    for row in rows:
        seen.extend(row.codes)
    assert sorted(seen) == LISTED, "every listed code lands on exactly one row"
    assert len(seen) == len(set(seen)), "no code appears on two rows"


def test_each_row_reports_the_decision_its_own_codes_carry():
    for row in m.list_decisions():
        for code in row.codes:
            assert SOURCE[code]["goes_again"].strip() == row.goes_again
            assert SOURCE[code]["what_has_to_change_first"].strip() == row.what_has_to_change_first


def test_person_required_is_true_only_on_the_rows_that_say_so():
    for code in LISTED:
        expected = int(SOURCE[code]["decision_row"]) in m.PERSON_REQUIRED_ROWS
        assert m.lookup_return_code(code).person_required is expected
    assert m.lookup_return_code("R05").person_required is True
    assert m.lookup_return_code("R01").person_required is False
    assert m.lookup_return_code("R99").person_required is True, "fallback is conservative"


def test_the_person_column_in_the_source_is_still_empty():
    """person_required is DERIVED. If this column is ever filled, two answers
    exist and the import is the place that must notice."""
    for code, row in SOURCE.items():
        assert not (row.get("needs_a_person_first") or "").strip(), code


# --- explain ----------------------------------------------------------------


def test_explain_r11_names_the_unauthorized_family_and_the_correctability():
    """The one row where the family and the decision disagree, which is the only
    thing in this table that is hard to know."""
    result = m.explain("R11")
    assert result.family == "unauthorized"
    assert "unauthorized" in result.family_note
    assert result.decision_row == 4
    assert "corrected_only" in result.why
    assert "our own entry" in result.why
    assert "unauthorized" in SOURCE["R11"]["customer_message"].lower()
    assert "correctable" in SOURCE["R11"]["customer_message"].lower()


def test_explain_states_no_family_where_the_table_states_none():
    for code in LISTED:
        result = m.explain(code)
        if code in m.FAMILY_STATED:
            continue
        assert result.family is None
        assert "not stated in this table" in result.family_note


def test_explain_on_an_unknown_code_carries_the_fallback_in_its_reason():
    result = m.explain("R98")
    assert result.matched is False
    assert "fell through" in result.why
    assert result.family is None


def test_explain_says_a_person_acts_where_one_does():
    assert "named person" in m.explain("R05").why
    assert "named person" not in m.explain("R01").why


# --- the validators, each broken on purpose ---------------------------------


def test_validate_refuses_a_default_row_that_lands_on_row_one():
    with pytest.raises(m.TableError, match="never do that"):
        m._validate(_table_without(**{m.DEFAULT_KEY: {"decision_row": "1"}}))


def test_validate_refuses_a_table_with_no_default_row():
    table = {c: r for c, r in m._TABLE.items() if c != m.DEFAULT_KEY}
    with pytest.raises(m.TableError, match="no DEFAULT row"):
        m._validate(table)


def test_validate_refuses_a_populated_person_column():
    with pytest.raises(m.TableError, match="no longer the only answer"):
        m._validate(_table_without(R05={"needs_a_person_first": "yes"}))


def test_validate_refuses_a_row_holding_two_different_decisions():
    with pytest.raises(m.TableError, match="is not one decision"):
        m._validate(_table_without(R09={"goes_again": "no"}))


def test_validate_refuses_a_stated_family_whose_sentence_has_gone():
    with pytest.raises(m.TableError, match="no longer says so"):
        m._validate(_table_without(R11={"customer_message": "nothing about families"}))


def test_validate_refuses_a_family_for_a_code_not_in_the_table():
    table = {c: r for c, r in m._TABLE.items() if c != "R11"}
    with pytest.raises(m.TableError, match="absent from the table"):
        m._validate(table)


def test_load_refuses_a_missing_file(tmp_path):
    with pytest.raises(m.TableError, match="missing at"):
        m._load(tmp_path / "nope.csv")


def test_load_refuses_a_header_only_file(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("code,decision_row\n", encoding="utf-8")
    with pytest.raises(m.TableError, match="zero rows"):
        m._load(path)


def test_load_refuses_a_duplicated_code(tmp_path):
    path = tmp_path / "dupe.csv"
    path.write_text("code,decision_row\nR01,1\nR01,5\n", encoding="utf-8")
    with pytest.raises(m.TableError, match="twice"):
        m._load(path)


# --- no egress --------------------------------------------------------------


def test_the_table_answers_with_the_network_removed(monkeypatch):
    """Asserted rather than intended: every socket call raises for the duration,
    and the answers still come back."""

    def refuse(*args, **kwargs):
        raise AssertionError("the decision table opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert m.lookup_return_code("R11").decision_row == 4
    assert len(m.list_decisions()) == 5
    assert m.explain("R05").person_required is True
