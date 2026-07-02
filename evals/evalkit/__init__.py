"""evalkit - a small, portable evaluation harness for LLM/agent outputs.

This package is intentionally dependency-free (standard library only) so it can
be vendored into any Python project by copying the ``evals/`` folder. It gives
three kinds of check, matching how these projects actually fail:

* **Quality** (:mod:`evalkit.judge`) - score a generated text against a rubric.
  Runs a deterministic *heuristic* judge by default (offline, free, stable in
  CI) and upgrades to a *live* LLM judge when a key is configured. Both share
  one :class:`~evalkit.judge.Rubric`, so the criteria never drift apart.
* **Guardrails** (:mod:`evalkit.guardrails`) - deterministic policy checks that
  encode the project golden rules (no em-dash, no unfilled ``{placeholders}``,
  no build-mechanic disclosure, and so on). These are pass/fail, never fuzzy.
* **Regression** (:mod:`evalkit.goldenset`) - snapshot the deterministic parts
  of a pipeline's output and fail when they drift, catching silent breakage.

Nothing here imports a specific project, so the same three modules drop into
PayPilot, Streamflow, Cart Recovery, etc. Each project supplies its own cases.
"""

from evalkit.guardrails import GuardrailReport, check_guardrails
from evalkit.judge import Criterion, JudgeResult, Rubric, judge
from evalkit.goldenset import Snapshot, load_snapshot, save_snapshot

__all__ = [
    "Criterion",
    "JudgeResult",
    "Rubric",
    "judge",
    "GuardrailReport",
    "check_guardrails",
    "Snapshot",
    "load_snapshot",
    "save_snapshot",
]
