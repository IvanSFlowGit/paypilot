"""Shared test fixtures.

The one job here is keeping the suite away from the real recovery database.
``app.store.get_store()`` builds a singleton at ``data/paypilot.db`` by default,
so without this every test run would write invoices and state transitions into
the operator's actual ledger. The autouse fixture points each test at its own
throwaway file and resets the singleton on both sides, so state never leaks
between tests either.
"""

from __future__ import annotations

import os

import pytest

from app.store import Store, reset_store

# Every variable that changes how the app behaves. ``app/__init__.py`` calls
# load_dotenv() at import, so without this the suite inherits whatever is in the
# developer's .env and stops testing the same thing CI tests.
#
# This is not hypothetical: adding PAYPILOT_DEMO_EMAIL to a local .env made
# test_missing_email_is_refused pass its argument check and run the whole demo
# cycle into the real ledger, and the test then failed on a machine where the
# variable was set while passing everywhere else.
_APP_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "PAYPILOT_LLM_DRAFT",
    "PAYPILOT_ENV",
    "PAYPILOT_DB_PATH",
    "PAYPILOT_UPDATE_URL",
    "PAYPILOT_PORTAL_RETURN_URL",
    "PAYPILOT_ALLOWED_LINK_HOSTS",
    "PAYPILOT_SEND_EMAIL",
    "PAYPILOT_ALLOWED_RECIPIENTS",
    "PAYPILOT_FROM_EMAIL",
    "PAYPILOT_DEMO_EMAIL",
    "PAYPILOT_DEMO_DB",
    "PAYPILOT_HOLDOUT_PCT",
    "PAYPILOT_HOLDOUT_SEED",
    "STRIPE_API_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_REQUIRE_SIGNATURE",
    "RESEND_API_KEY",
    "WEBHOOK_SECRET",
    "ADMIN_TOKEN",
)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    """Run every test against a clean environment, not the developer's .env.

    Tests that need a variable set it themselves; that way what a test depends
    on is visible in the test.
    """
    for name in _APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Keep it out of the real ledger even if a test forgets to pass a path.
    monkeypatch.setenv("PAYPILOT_DB_PATH", os.devnull + "-unset")


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Give every test a private SQLite database."""
    db_path = tmp_path / "paypilot-test.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(db_path))
    store = Store(db_path)
    reset_store(store)
    try:
        yield store
    finally:
        reset_store(None)
        store.close()
