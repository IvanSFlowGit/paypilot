"""Shared test fixtures.

The one job here is keeping the suite away from the real recovery database.
``app.store.get_store()`` builds a singleton at ``data/paypilot.db`` by default,
so without this every test run would write invoices and state transitions into
the operator's actual ledger. The autouse fixture points each test at its own
throwaway file and resets the singleton on both sides, so state never leaks
between tests either.
"""

from __future__ import annotations

import pytest

from app.store import Store, reset_store


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
