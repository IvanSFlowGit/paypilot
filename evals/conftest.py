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

# The evals own their ledger. Without this they opened whatever data/paypilot.db the developer had,
# and a copy from before tenancy made 24 of them fail with LegacySchemaError, a fact about one
# laptop's file rather than about the code.
import tempfile  # noqa: E402

os.environ["PAYPILOT_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="pp_evals_")) / "evals.db")

SNAPSHOT_DIR = Path(__file__).parent / "cases" / "snapshots"
