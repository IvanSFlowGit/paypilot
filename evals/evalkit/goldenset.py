"""Regression snapshots for the deterministic parts of a pipeline's output.

LLM text is judged fuzzily; everything else (routing decisions, computed
numbers, state transitions) should be *exactly* stable run to run. This module
stores a JSON snapshot of those deterministic fields and fails when they drift -
the cheapest possible guard against a pipeline that silently stops doing its job.

Usage in a test::

    from evalkit.goldenset import assert_snapshot

    def test_decisions_stable():
        out = run_recovery(EVENT)
        # Keep only the deterministic slice; drop LLM prose.
        decision = {k: out[k] for k in ("risk", "strategy", "schedule")}
        assert_snapshot("card_expired_attempt1", decision, SNAP_DIR)

First run writes the snapshot and passes. Later runs compare. To intentionally
re-baseline after a deliberate change, run with ``EVAL_UPDATE_SNAPSHOTS=1``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Snapshot:
    """A named JSON snapshot backed by ``<dir>/<name>.json``."""

    name: str
    path: Path
    data: Any


def _snap_path(name: str, snapshot_dir: str | Path) -> Path:
    safe = name.replace("/", "_").replace(" ", "_")
    return Path(snapshot_dir) / f"{safe}.json"


def save_snapshot(name: str, data: Any, snapshot_dir: str | Path) -> Snapshot:
    """Write ``data`` as the canonical snapshot for ``name``."""
    path = _snap_path(name, snapshot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    return Snapshot(name=name, path=path, data=data)


def load_snapshot(name: str, snapshot_dir: str | Path) -> Snapshot | None:
    """Load the stored snapshot for ``name``, or None if it does not exist."""
    path = _snap_path(name, snapshot_dir)
    if not path.exists():
        return None
    return Snapshot(name=name, path=path, data=json.loads(path.read_text()))


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def diff(expected: Any, actual: Any) -> str:
    """Human-readable line diff between two JSON-able values."""
    import difflib

    e = _canonical(expected).splitlines()
    a = _canonical(actual).splitlines()
    return "\n".join(difflib.unified_diff(e, a, fromfile="snapshot", tofile="actual", lineterm=""))


def assert_snapshot(name: str, actual: Any, snapshot_dir: str | Path) -> None:
    """Compare ``actual`` to the stored snapshot; raise AssertionError on drift.

    Creates the snapshot on first run (and when ``EVAL_UPDATE_SNAPSHOTS=1``),
    which passes. This is the function tests call.
    """
    updating = os.getenv("EVAL_UPDATE_SNAPSHOTS", "").strip() in ("1", "true", "yes")
    existing = load_snapshot(name, snapshot_dir)

    if existing is None or updating:
        save_snapshot(name, actual, snapshot_dir)
        return

    # Round-trip actual through JSON so tuples/sets compare like the stored form.
    actual_norm = json.loads(_canonical(actual))
    if actual_norm != existing.data:
        raise AssertionError(
            f"snapshot drift for '{name}':\n{diff(existing.data, actual_norm)}\n"
            f"(re-baseline with EVAL_UPDATE_SNAPSHOTS=1 if this change is intended)"
        )
