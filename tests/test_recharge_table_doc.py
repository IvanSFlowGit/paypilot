"""docs/recharge-error-types.md must be exactly what the code renders."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_recharge_table.py"
_spec = importlib.util.spec_from_file_location("render_recharge_table", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_committed_table_matches_the_code():
    assert mod.OUT.read_text(encoding="utf-8") == mod.render(), (
        "docs/recharge-error-types.md is stale: run python scripts/render_recharge_table.py")


def test_every_type_lands_in_exactly_one_pile():
    from app.recharge_map import PUBLISHED_ERROR_TYPES

    doc = mod.render()
    for code in PUBLISHED_ERROR_TYPES:
        assert doc.count(f"| `{code}` |") == 1, code
