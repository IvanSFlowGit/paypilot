"""The registry, and the import-time control that keeps its claims honest.

The case that earns this file is test_a_default_on_a_required_rail_argument
_breaks_the_registry_import. It is a SECOND assertion, at a different layer,
on the same property the Bacs suite already pins: that Bacs's report argument
has no default. The Bacs test fails in the Bacs suite. This one fails in
anything that imports the registry, which is everything that will eventually
call a rail.
"""

from __future__ import annotations

import importlib
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest

from app import rails


# --------------------------------------------------------------------------
# What the registry knows
# --------------------------------------------------------------------------

def test_every_rail_is_listed_and_sorted():
    assert rails.rails() == ("ach", "bacs", "sepa")


def test_each_rail_names_its_scheme_and_its_module():
    for name in rails.rails():
        record = rails.get(name)
        assert record.name == name
        assert record.scheme
        assert record.module.__name__.endswith("_return_map")


def test_a_rail_name_is_matched_case_and_space_insensitively():
    assert rails.get("  BACS ").name == "bacs"


# --------------------------------------------------------------------------
# The refusals. Each one names what is wrong rather than returning a guess.
# --------------------------------------------------------------------------

def test_an_unknown_rail_is_refused_and_the_known_ones_are_named():
    with pytest.raises(rails.RailError, match="no rail named"):
        rails.lookup("faster-payments", "0")
    try:
        rails.lookup("faster-payments", "0")
    except rails.RailError as exc:
        for name in rails.rails():
            assert name in str(exc)


def test_a_non_string_rail_is_refused_rather_than_coerced():
    with pytest.raises(rails.RailError, match="must be a string"):
        rails.get(None)


def test_bacs_without_a_report_is_refused_and_never_defaulted():
    """The money-moving case. A guessed report is a confident wrong answer."""
    with pytest.raises(rails.RailError, match="requires report"):
        rails.lookup("bacs", "0")
    with pytest.raises(rails.RailError, match="requires report"):
        rails.explain("bacs", "0")


def test_a_rail_that_takes_no_context_refuses_context_rather_than_ignoring_it():
    with pytest.raises(rails.RailError, match="takes no report"):
        rails.lookup("ach", "R01", report="ARUDD")
    with pytest.raises(rails.RailError, match="takes no scope"):
        rails.lookup("sepa", "AC04", scope="core")


# --------------------------------------------------------------------------
# What it returns: the rail's own shape, not a flattened one
# --------------------------------------------------------------------------

def test_each_rail_returns_its_own_decision_type():
    assert isinstance(rails.lookup("ach", "R01"), rails.get("ach").module.Decision)
    assert isinstance(
        rails.lookup("bacs", "0", report="ARUDD"), rails.get("bacs").module.Decision
    )
    assert isinstance(rails.lookup("sepa", "AC04"), rails.get("sepa").module.Decision)


def test_the_fields_that_only_one_rail_has_survive_the_registry():
    """A common shape would have dropped every one of these."""
    bacs = rails.lookup("bacs", "0", report="ARUDD")
    assert bacs.report == "ARUDD"
    assert bacs.meaning
    assert bacs.collides_in  # 0 collides with ADDACS

    sepa = rails.lookup("sepa", "AC04")
    assert sepa.scheme_scope
    assert sepa.suggested_creditor_action


def test_the_three_fields_every_rail_shares_are_present_on_all_three():
    for name, context in (("ach", {}), ("bacs", {"report": "ARUDD"}), ("sepa", {})):
        code = {"ach": "R01", "bacs": "0", "sepa": "AC04"}[name]
        decision = rails.lookup(name, code, **context)
        assert isinstance(decision.decision_row, int)
        assert decision.goes_again
        assert decision.what_has_to_change_first
        assert decision.person_required in (True, False)


def test_every_explanation_carries_the_same_spine_across_the_three_rails():
    """The spine is what makes a cross-rail report possible at all.

    Written after the registry caught Bacs returning a bag of ``lines`` where
    ACH and SEPA return named fields. The spine is asserted here rather than
    only inside each rail, because a rail can be internally consistent and
    still be the odd one out, and only something that sees all three notices.
    """
    spine = ("code", "matched", "decision_row", "why", "person_required")
    for name, context in (("ach", {}), ("bacs", {"report": "ARUDD"}), ("sepa", {})):
        code = {"ach": "R01", "bacs": "0", "sepa": "AC04"}[name]
        explanation = rails.explain(name, code, **context)
        for field in spine:
            assert hasattr(explanation, field), (name, field)
        assert isinstance(explanation.why, str) and explanation.why.strip(), name
        assert explanation.decision_row >= 1, name


def test_list_decisions_returns_rows_for_every_rail():
    for name in rails.rails():
        rows = rails.list_decisions(name)
        assert rows
        assert all(row.codes for row in rows)


# --------------------------------------------------------------------------
# Whether a rail abstains at all is declared, and the declaration is checked
# --------------------------------------------------------------------------

def test_the_abstain_declaration_matches_the_rail():
    assert rails.get("ach").abstains is False
    assert rails.get("bacs").abstains is True
    assert rails.get("sepa").abstains is True


def test_ach_has_no_abstain_row_and_the_registry_says_so_rather_than_faking_one():
    """ACH's DEFAULT lands on a decision row. There is nothing to abstain to."""
    assert not hasattr(rails.get("ach").module, "ABSTAIN_ROWS")
    assert not hasattr(rails.lookup("ach", "R01"), "abstains")


# --------------------------------------------------------------------------
# The import-time control, exercised in a copied tree so the real one is safe
# --------------------------------------------------------------------------

_HERE = pathlib.Path(__file__).resolve().parent
_APP = _HERE.parent / "app"


def _tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A whole copy of app/, because the registry imports three real modules."""
    root = tmp_path / "copy"
    root.mkdir()
    shutil.copytree(_APP, root / "app", ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _import_registry(root: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Import the copied registry in a fresh process and report what happened.

    A subprocess rather than importlib: the registry validates at import, and
    a module already in sys.modules would not re-run that.
    """
    return subprocess.run(
        [sys.executable, "-c", "import app.rails"],
        cwd=root,
        capture_output=True,
        text=True,
    )


def test_the_copied_registry_imports_cleanly(tmp_path):
    """Positive control. Without it, every refusal below could be a bad copy."""
    done = _import_registry(_tree(tmp_path))
    assert done.returncode == 0, done.stderr


def test_a_default_on_a_required_rail_argument_breaks_the_registry_import(tmp_path):
    """The one that matters.

    Bacs's report has no default because ARUDD, ADDACS and AUDDIS share a
    one-character symbol space. If somebody gives it one, this refuses.
    """
    root = _tree(tmp_path)
    target = root / "app" / "bacs_return_map.py"
    source = target.read_text()
    before = "def lookup_return_code(code: object, report: str) -> Decision:"
    assert source.count(before) == 1
    target.write_text(
        source.replace(
            before,
            'def lookup_return_code(code: object, report: str = "ARUDD") -> Decision:',
        )
    )
    done = _import_registry(root)
    assert done.returncode != 0
    assert "RegistryError" in done.stderr
    assert "requires ()" in done.stderr and "declares ('report',)" in done.stderr


def test_a_new_required_argument_nobody_declared_breaks_the_import(tmp_path):
    """The other direction, which is the one that rots quietly.

    Undeclared, every registry call would raise TypeError from inside the
    rail, which reads as the caller's fault rather than the registry's.
    """
    root = _tree(tmp_path)
    target = root / "app" / "ach_return_map.py"
    source = target.read_text()
    before = "def lookup_return_code(code: object) -> Decision:"
    assert source.count(before) == 1
    target.write_text(
        source.replace(before, "def lookup_return_code(code: object, era: str) -> Decision:")
    )
    done = _import_registry(root)
    assert done.returncode != 0
    assert "RegistryError" in done.stderr
    assert "requires ('era',)" in done.stderr


def test_a_rail_missing_a_required_name_breaks_the_import(tmp_path):
    root = _tree(tmp_path)
    target = root / "app" / "sepa_return_map.py"
    source = target.read_text()
    assert source.count("def list_decisions(") == 1
    target.write_text(source.replace("def list_decisions(", "def _list_decisions("))
    done = _import_registry(root)
    assert done.returncode != 0
    assert "has no list_decisions" in done.stderr


def test_a_missing_table_breaks_the_import_rather_than_the_first_lookup(tmp_path):
    root = _tree(tmp_path)
    (root / "app" / "data" / "bacs_return_codes.csv").unlink()
    done = _import_registry(root)
    assert done.returncode != 0
    # The rail's own loader refuses first, which is correct and is why the
    # registry's table check is a backstop rather than the primary guard.
    assert "bacs_return_codes.csv" in done.stderr


def test_a_wrong_abstain_declaration_breaks_the_import(tmp_path):
    root = _tree(tmp_path)
    target = root / "app" / "rails.py"
    source = target.read_text()
    before = textwrap.dedent(
        """\
                name="bacs",
                module=bacs_return_map,
                required_context=("report",),
                abstains=True,"""
    )
    assert source.count('        abstains=True,\n        scheme="Bacs') == 1
    target.write_text(
        source.replace(
            '        abstains=True,\n        scheme="Bacs',
            '        abstains=False,\n        scheme="Bacs',
        )
    )
    done = _import_registry(root)
    assert done.returncode != 0
    assert "ABSTAIN_ROWS" in done.stderr and "abstains=False" in done.stderr


def test_an_empty_registry_is_refused(tmp_path):
    """Otherwise every structural check above passes by having nothing to check."""
    root = _tree(tmp_path)
    target = root / "app" / "rails.py"
    source = target.read_text()
    assert source.count("_validate_registry(_RAILS)") == 1
    target.write_text(source.replace("_validate_registry(_RAILS)", "_validate_registry(())"))
    done = _import_registry(root)
    assert done.returncode != 0
    assert "registry is empty" in done.stderr
