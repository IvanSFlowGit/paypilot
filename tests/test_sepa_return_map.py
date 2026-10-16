"""What the SEPA table must keep being true about, and what must break it.

Every mutation here edits the loaded table and asserts :func:`_validate`
refuses it. One of them reloads the module against a broken file on disk, to
prove the validator is actually on the import path rather than merely present
in the file: a validator nothing calls and a validator that passes everything
produce the same green run.

The four letter codes get their own test because the defect they caught was
real. A pattern written as ``[A-Z]{2}[0-9]{2}`` reported 23 codes where the
document has 25, and ``CNOR`` and ``DNOR`` were found by reading the document
rather than by matching it.
"""

from __future__ import annotations

import copy
import csv
import importlib.util
import pathlib
import shutil
import sys

import pytest

from app import sepa_return_map as sepa

# The count the source document carries. Pinned deliberately: this is OUR table
# built from a document we read, so pinning it is pinning something we control,
# and a row appearing or vanishing is a thing somebody must notice.
EXPECTED_CODES = 25

#: The real module file, imported from a copy to prove import-time refusal.
_MODULE_SOURCE = pathlib.Path(sepa.__file__).resolve()


def _mutable_table() -> dict[str, dict[str, str]]:
    return copy.deepcopy(sepa._TABLE)


def test_the_table_holds_every_code_the_source_document_lists():
    assert len(sepa.CODES) == EXPECTED_CODES
    assert sepa.DEFAULT_KEY not in sepa.CODES


def test_the_four_letter_codes_resolve_and_match_the_shape():
    # The regression. Both are real rows in EPC173-14 v8.1 and neither is two
    # letters and two digits.
    for code in ("CNOR", "DNOR"):
        assert code in sepa.CODES, f"{code} is missing from the table"
        assert sepa._CODE_RE.match(code), f"{code} does not match the code shape"
        assert sepa.lookup_return_code(code).matched is True
    # And the ordinary shape still matches, so widening did not break it.
    assert sepa._CODE_RE.match("AC01")
    assert not sepa._CODE_RE.match("AC1")
    assert not sepa._CODE_RE.match("ACCOUNT")


def test_every_listed_code_resolves_to_a_matched_decision():
    for code in sepa.CODES:
        decision = sepa.lookup_return_code(code)
        assert decision.matched is True
        assert decision.code == code
        assert decision.suggested_creditor_action, f"{code} lost its scheme action"


def test_an_unlisted_code_abstains_and_says_it_fell_through():
    decision = sepa.lookup_return_code("ZZ99")
    assert decision.matched is False
    assert decision.abstains is True
    assert decision.decision_row != 1
    assert decision.fallback_reason is not None
    assert "fell through" in decision.fallback_reason
    # The shape report distinguishes a plausible unknown from a typo.
    assert "looks like a reason code" in decision.fallback_reason
    assert "is not shaped like" in sepa.lookup_return_code("banana").fallback_reason


def test_the_default_row_is_an_abstain_row_and_never_row_one():
    row = int(sepa._TABLE[sepa.DEFAULT_KEY]["decision_row"])
    assert row != 1
    assert row in sepa.ABSTAIN_ROWS


def test_ms03_abstains_because_its_meanings_span_more_than_one_row():
    decision = sepa.lookup_return_code("MS03")
    assert decision.abstains is True
    assert decision.abstain_reason is not None
    rows = {int(sepa._TABLE[c]["decision_row"]) for c in sepa.MS03_STANDS_IN_FOR}
    assert len(rows) > 1, "a single row would mean MS03 need not abstain"
    # The span is the argument, so assert it reaches both ends of the table.
    assert min(rows) == 1
    assert 5 in rows


def test_an_abstain_carries_a_reason_and_a_decision_carries_none():
    for code in sepa.CODES:
        decision = sepa.lookup_return_code(code)
        if decision.abstains:
            assert decision.abstain_reason, f"{code} abstains with no reason"
        else:
            assert decision.abstain_reason is None, f"{code} both decides and abstains"


def test_a_duplicate_never_goes_again_automatically():
    # AM05 means the money may already have been taken once. A retry takes it
    # twice, so nothing automatic may happen on this code.
    decision = sepa.lookup_return_code("AM05")
    assert decision.goes_again != "yes"
    assert decision.decision_row != 1
    assert "ALREADY HAVE BEEN TAKEN" in decision.customer_message


def test_row_one_is_the_only_row_where_nothing_has_to_change():
    for row in sepa.list_decisions():
        if row.decision_row == 1:
            assert row.goes_again == "yes"
            assert row.what_has_to_change_first == "nothing"
            assert row.abstains is False
        else:
            assert row.goes_again != "yes", f"row {row.decision_row} retries unchanged"


def test_a_person_is_required_on_exactly_the_rows_that_say_so():
    for row in sepa.list_decisions():
        assert row.person_required is (row.decision_row in sepa.PERSON_REQUIRED_ROWS)
        if row.abstains:
            assert row.person_required, "an abstain with nobody looking is a dead end"


def test_a_refund_only_code_is_reported_as_a_refund():
    # MD06 is a Refund and nothing else: the money moved and is being clawed
    # back, which is a different operational event from a reject.
    assert sepa.lookup_return_code("MD06").r_transaction_types == ("Refund",)
    assert "Refund" in sepa.lookup_return_code("MD01").r_transaction_types
    assert "Refund" not in sepa.lookup_return_code("AM04").r_transaction_types


def test_the_b2b_only_code_says_it_is_b2b_only():
    assert sepa.lookup_return_code("AC13").scheme_scope == "b2b"
    assert sepa.lookup_return_code("MD06").scheme_scope == "core"
    assert sepa.lookup_return_code("AM04").scheme_scope == "both"


def test_ag01_carries_the_schemes_own_must_not_and_explain_surfaces_it():
    decision = sepa.lookup_return_code("AG01")
    assert decision.must_not_be_used_for == "AC13"
    why = sepa.explain("AG01").why
    assert "AC13" in why
    assert sepa.AG01_IMPORTANT_NOTE in why
    # One direction only. The document states AG01 must not be used where AC13
    # applies and says nothing about the reverse, so neither do we.
    assert sepa.lookup_return_code("AC13").must_not_be_used_for is None


def test_explain_reports_no_decision_rather_than_a_decision_when_it_abstains():
    why = sepa.explain("MS03").why
    assert "No decision" in why
    assert "Goes again" not in why
    assert "Goes again" in sepa.explain("AM04").why


def test_normalise_takes_case_and_space_and_refuses_everything_else():
    assert sepa.normalise(" ac01 ") == "AC01"
    with pytest.raises(sepa.TableError):
        sepa.normalise(None)
    with pytest.raises(sepa.TableError):
        sepa.normalise("   ")


# ---------------------------------------------------------------------------
# Mutations. Each one must red on its own property and say which.
# ---------------------------------------------------------------------------


def test_moving_default_off_an_abstain_row_is_refused():
    table = _mutable_table()
    table[sepa.DEFAULT_KEY]["decision_row"] = "2"
    table[sepa.DEFAULT_KEY]["abstain_reason"] = ""
    table[sepa.DEFAULT_KEY]["goes_again"] = "not_on_this_account"
    table[sepa.DEFAULT_KEY]["what_has_to_change_first"] = "account details"
    with pytest.raises(sepa.TableError, match="not an abstain row"):
        sepa._validate(table)


def test_putting_default_on_row_one_is_refused():
    table = _mutable_table()
    table[sepa.DEFAULT_KEY]["decision_row"] = "1"
    with pytest.raises(sepa.TableError, match="must never do that"):
        sepa._validate(table)


def test_an_abstain_row_with_no_reason_is_refused():
    table = _mutable_table()
    table["MS03"]["abstain_reason"] = ""
    with pytest.raises(sepa.TableError, match="gives no reason"):
        sepa._validate(table)


def test_a_deciding_row_that_also_abstains_is_refused():
    table = _mutable_table()
    table["AM04"]["abstain_reason"] = "something"
    with pytest.raises(sepa.TableError, match="two answers exist"):
        sepa._validate(table)


def test_a_row_holding_two_decisions_is_refused():
    table = _mutable_table()
    table["AC04"]["goes_again"] = "yes"
    with pytest.raises(sepa.TableError, match="is not one decision"):
        sepa._validate(table)


def test_a_must_not_pointing_at_a_code_that_is_not_here_is_refused():
    table = _mutable_table()
    table["AG01"]["must_not_be_used_for"] = "AC99"
    with pytest.raises(sepa.TableError, match="not in this table"):
        sepa._validate(table)


def test_an_unknown_scheme_scope_is_refused():
    table = _mutable_table()
    table["AM04"]["scheme_scope"] = "sepa"
    with pytest.raises(sepa.TableError, match="not one of"):
        sepa._validate(table)


def test_a_code_with_no_scheme_action_to_derive_from_is_refused():
    table = _mutable_table()
    table["AM04"]["suggested_creditor_action"] = ""
    with pytest.raises(sepa.TableError, match="no suggested_creditor_action"):
        sepa._validate(table)


def test_ms03_no_longer_needing_to_abstain_is_refused():
    # If every code MS03 stands in for landed on one row, a single answer would
    # be available and the abstain would be over-caution rather than honesty.
    table = _mutable_table()
    for code in sepa.MS03_STANDS_IN_FOR:
        table[code]["decision_row"] = "4"
        table[code]["goes_again"] = "corrected_only"
        table[code]["what_has_to_change_first"] = "our own entry"
        table[code]["abstain_reason"] = ""
    with pytest.raises(sepa.TableError, match="no longer needs to abstain"):
        sepa._validate(table)


def test_ms03_moved_off_the_abstain_row_is_refused():
    table = _mutable_table()
    table["MS03"]["decision_row"] = "1"
    table["MS03"]["goes_again"] = "yes"
    table["MS03"]["what_has_to_change_first"] = "nothing"
    table["MS03"]["abstain_reason"] = ""
    with pytest.raises(sepa.TableError, match="not on an abstain row"):
        sepa._validate(table)


def _import_from(directory: pathlib.Path, name: str):
    """Import the REAL module source from a copy, so TABLE_PATH resolves there.

    The module must be in ``sys.modules`` before ``exec_module``: a frozen
    dataclass resolves its own module during class creation and gets ``None``
    otherwise, which surfaces as an unrelated AttributeError.
    """
    spec = importlib.util.spec_from_file_location(name, directory / "sepa_return_map.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _broken_copy(tmp_path: pathlib.Path) -> pathlib.Path:
    """The real module beside a table with no DEFAULT row to fall through to."""
    package = tmp_path / "copy"
    (package / "data").mkdir(parents=True)
    shutil.copy(_MODULE_SOURCE, package / "sepa_return_map.py")
    with (package / "data" / "sepa_return_codes.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sepa._TABLE["AM04"].keys()))
        writer.writeheader()
        writer.writerow(dict(sepa._TABLE["AM04"]))
    return package


def _good_copy(tmp_path: pathlib.Path) -> pathlib.Path:
    package = tmp_path / "copy"
    (package / "data").mkdir(parents=True)
    shutil.copy(_MODULE_SOURCE, package / "sepa_return_map.py")
    shutil.copy(sepa.TABLE_PATH, package / "data" / "sepa_return_codes.csv")
    return package


def test_the_validator_is_on_the_import_path(tmp_path):
    # The wiring, not the logic. A validator present in the file and never
    # called produces exactly the same green suite as one that works.
    #
    # monkeypatching TABLE_PATH and reloading does NOT prove this: the module
    # recomputes TABLE_PATH from __file__ on every execution, so the reload
    # discards the patch and reads the real table. That version of this test
    # passed while proving nothing, which is the shape it exists to catch.
    with pytest.raises(Exception, match="no DEFAULT row"):
        _import_from(_broken_copy(tmp_path), "sepa_copy_broken")


def test_the_import_path_control_can_also_pass(tmp_path):
    # The other direction. Without it, the test above would still pass if the
    # copied module could never import at all for an unrelated reason, and a
    # refusal that is really a broken fixture reads exactly like a working gate.
    module = _import_from(_good_copy(tmp_path), "sepa_copy_good")
    try:
        assert len(module.CODES) == EXPECTED_CODES
        assert module.TABLE_PATH != sepa.TABLE_PATH, "the copy read the real table"
    finally:
        sys.modules.pop("sepa_copy_good", None)


def test_a_missing_table_file_is_refused_rather_than_defaulted(tmp_path):
    with pytest.raises(sepa.TableError, match="missing at"):
        sepa._load(tmp_path / "not-here.csv")
