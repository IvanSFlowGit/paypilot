"""Guardrail evals: deterministic golden-rule checks on generated text.

No em-dash, no unfilled placeholders, sane length, no build-mechanic disclosure
in the customer email, no AI tells.
"""

from __future__ import annotations

import pytest
from cases.dunning_cases import CASES, DIAGNOSIS_GUARDS, MESSAGE_GUARDS
from evalkit.guardrails import check_guardrails

from app import graph as graph_module


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_message_guardrails(case):
    out = graph_module.run_recovery(case.event)
    report = check_guardrails(out["message"], MESSAGE_GUARDS, case.context)
    assert report.ok, f"[{case.key}] {report.summary()}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_diagnosis_guardrails(case):
    out = graph_module.run_recovery(case.event)
    report = check_guardrails(out["diagnosis"], DIAGNOSIS_GUARDS, case.context)
    assert report.ok, f"[{case.key}] {report.summary()}"
