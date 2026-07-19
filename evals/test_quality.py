"""Quality evals: judge the generated message + diagnosis against rubrics.

Heuristic judge by default (offline, deterministic). Set EVAL_JUDGE=live plus a
key to score the same rubrics with an LLM judge.
"""

from __future__ import annotations

import pytest
from cases.dunning_cases import CASES, DIAGNOSIS_RUBRIC, MESSAGE_RUBRIC
from evalkit.judge import judge

from app import graph as graph_module


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_message_quality(case):
    out = graph_module.run_recovery(case.event)
    result = judge(out["message"], MESSAGE_RUBRIC, case.context)
    assert result.passed, f"[{case.key}] message score {result.score} ({result.mode})\n" + "\n".join(result.reasons)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_diagnosis_quality(case):
    out = graph_module.run_recovery(case.event)
    result = judge(out["diagnosis"], DIAGNOSIS_RUBRIC, case.context)
    assert result.passed, f"[{case.key}] diagnosis score {result.score} ({result.mode})\n" + "\n".join(result.reasons)
