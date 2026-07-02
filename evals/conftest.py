"""Eval-suite fixtures and setup.

Forces PayPilot into its offline mock mode for the whole eval run by clearing
``OPENAI_API_KEY`` before ``app`` is imported. That keeps evals free, network-
free, and deterministic - the text under test is the playbook-grounded mock,
which is exactly what a keyless visitor to the live demo sees.

To evaluate the *real* model instead, run with a key AND ``EVAL_JUDGE=live`` in a
dedicated job; this conftest only governs the app-under-test, not the judge.
"""

from __future__ import annotations

import os
from pathlib import Path

# Clear before app import so app.nodes.use_mock() reports True at module load.
os.environ.pop("OPENAI_API_KEY", None)

SNAPSHOT_DIR = Path(__file__).parent / "cases" / "snapshots"
