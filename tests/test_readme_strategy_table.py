"""The README's strategy table must list exactly the codes in the rules table.

It drifted once already: three codes for weeks while the code had six. A table
that a reader trusts and nothing checks is the cheapest place for that to happen.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.decision import STRATEGY_RULES

README = Path(__file__).resolve().parents[1] / "README.md"


def _table_rows() -> dict[str, tuple[str, str]]:
    text = README.read_text(encoding="utf-8")
    start = text.index("| Failure code")
    rows = {}
    for line in text[start:].splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows[cells[0].strip("`")] = (cells[1], cells[2])
    return rows


def test_readme_table_lists_every_failure_code():
    assert set(_table_rows()) == set(STRATEGY_RULES)


def test_readme_table_matches_action_and_retry():
    for code, (retry, action) in _table_rows().items():
        rule = STRATEGY_RULES[code]
        assert action == rule["action"].replace("_", " "), code
        days = rule["retry_in_days"]
        expected = "never" if days == 0 else f"~{days} day" + ("" if days == 1 else "s")
        assert retry == expected, code


def test_control_parser_finds_rows():
    # Control: the parser must actually read rows, or the tests above check nothing.
    assert len(_table_rows()) >= 3
