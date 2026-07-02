"""Regression evals: snapshot the deterministic decision fields.

Guards against silent pipeline breakage - a node that stops routing correctly,
a scoring change nobody intended. Only risk / strategy / impact are snapshotted
(schedule carries relative dates). Re-baseline intentional changes with
EVAL_UPDATE_SNAPSHOTS=1.
"""

from __future__ import annotations

import pytest

from app import graph as graph_module
from cases.dunning_cases import CASES, SNAPSHOT_DIR, SNAPSHOT_FIELDS
from evalkit.goldenset import assert_snapshot


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_decision_snapshot(case):
    out = graph_module.run_recovery(case.event)
    decision = {k: out[k] for k in SNAPSHOT_FIELDS}
    assert_snapshot(case.key, decision, SNAPSHOT_DIR)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_schedule_matches_strategy(case):
    """The scheduled cadence must equal the strategy's retry_in_days (date-safe)."""
    out = graph_module.run_recovery(case.event)
    assert out["schedule"]["retry_in_days"] == out["strategy"]["retry_in_days"]
