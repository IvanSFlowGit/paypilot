"""The README's decision table must list exactly what the CSV decides.

Mirrors ``tests/test_readme_strategy_table.py``, which exists because that table
drifted for weeks: three codes in the README against six in the code. A table a
reader trusts and nothing checks is the cheapest place for that to happen again,
and this one is in a public README on a repository offered as evidence.

Parsed out of the rendered markdown rather than out of a generator's input, so it
checks the thing a reader actually sees.
"""

from __future__ import annotations

from pathlib import Path

from app import ach_return_map as m

README = Path(__file__).resolve().parents[1] / "README.md"
HEADER = "| Row | Goes again | What has to change first | A person acts first | Codes |"


def _table_rows() -> dict[int, tuple[str, str, str, tuple[str, ...]]]:
    text = README.read_text(encoding="utf-8")
    start = text.index(HEADER)
    rows: dict[int, tuple[str, str, str, tuple[str, ...]]] = {}
    for line in text[start:].splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        codes = tuple(c.strip().strip("`") for c in cells[4].split(","))
        rows[int(cells[0])] = (cells[1], cells[2], cells[3], codes)
    return rows


def test_control_parser_finds_every_row():
    """Control: without this the assertions below could be checking nothing."""
    rows = _table_rows()
    assert len(rows) == 5, rows
    assert sorted(rows) == [1, 2, 3, 4, 5]


def test_readme_table_lists_every_code_exactly_once():
    listed: list[str] = []
    for _, _, _, codes in _table_rows().values():
        listed.extend(codes)
    assert sorted(listed) == sorted(m.CODES)
    assert len(listed) == len(set(listed))


def test_readme_table_matches_the_decision_for_each_row():
    parsed = _table_rows()
    for row in m.list_decisions():
        goes, change, person, codes = parsed[row.decision_row]
        assert goes == row.goes_again.replace("_", " "), row.decision_row
        assert change == row.what_has_to_change_first, row.decision_row
        assert person == ("yes" if row.person_required else "no"), row.decision_row
        assert codes == row.codes, row.decision_row


def test_readme_says_the_default_never_lands_on_row_one():
    text = README.read_text(encoding="utf-8")
    assert "never null and it is never row 1" in text
    assert m.lookup_return_code("__unlisted__").decision_row != 1


def test_readme_claims_only_the_family_the_table_states():
    text = README.read_text(encoding="utf-8")
    assert "`R11` is the row worth the table" in text
    assert list(m.FAMILY_STATED) == ["R11"], (
        "the README names R11 as the only sourced family; if another is added "
        "the README has to say so too"
    )


def test_readme_still_says_it_carries_no_client_traffic():
    """Both halves, every time: deployed and running, and no client traffic."""
    text = README.read_text(encoding="utf-8")
    assert "it does not carry client traffic" in text
