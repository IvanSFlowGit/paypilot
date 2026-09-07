# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The load driver is a measuring instrument, so it gets tested like one.

An instrument that has never disagreed with anything has not been tested, it has
only been run. Each test here is pointed at a way the driver could produce a
confident wrong number:

* it could send bodies the API would reject, and report an error rate that is a
  fact about the driver rather than about the system,
* it could sign bytes other than the ones it sends, so every request 401s and
  the run measures the auth path,
* its percentiles could be wrong, which is invisible because percentiles have no
  obvious sanity check,
* and its ledger probe could return zeros for a missing database, so a broken
  probe would read as a clean result.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import loadtest  # noqa: E402

from app import stripe_map  # noqa: E402


def test_build_event_survives_the_apps_own_stripe_mapper():
    """Validate through the real mapper, not against a copy of its rules.

    A hand-written assertion about field names would drift the moment the event
    contract changed. Running the driver's payload through the app's own
    ``stripe_event_to_failure`` means the driver breaks loudly if the contract
    moves, which is the only thing that keeps it a valid instrument.

    The invoice id assertion is the load-bearing one: ``handle_payment_failed``
    returns ``handled: False, reason: missing_invoice_id`` and writes NOTHING
    when it is absent. A driver that sent those would get 200s all day, record
    zero rows, and report a throughput figure for work that never happened.
    """
    for i in range(50):
        row = stripe_map.stripe_event_to_failure(loadtest.build_event(i))
        assert row["invoice_id"], f"event {i} maps to no invoice id, so nothing would be recorded"


def test_event_and_invoice_ids_are_unique_across_the_run():
    """Reused ids would measure the idempotency cache and call it throughput."""
    events = [loadtest.build_event(i) for i in range(500)]
    assert len({e["id"] for e in events}) == 500
    assert len({e["data"]["object"]["id"] for e in events}) == 500


def test_event_type_is_one_the_route_actually_handles():
    from app.loop import HANDLED_EVENT_TYPES

    assert loadtest.build_event(0)["type"] in HANDLED_EVENT_TYPES


def test_build_event_is_deterministic():
    # Two runs of the same ladder must send byte-identical sequences, or a
    # difference between runs cannot be attributed to the system.
    assert [loadtest.build_event(i) for i in range(20)] == [
        loadtest.build_event(i) for i in range(20)
    ]


def test_signature_verifies_against_the_apps_own_verifier():
    """The driver signs; the app verifies. Only this proves the two agree."""
    secret = "test-secret"
    body = loadtest.encode(loadtest.build_event(7))
    headers = loadtest.headers_for(body, secret)
    assert stripe_map.verify_stripe_signature(body, headers["stripe-signature"], secret)


def test_signature_is_absent_when_no_secret_configured():
    body = loadtest.encode(loadtest.build_event(1))
    assert "stripe-signature" not in loadtest.headers_for(body, None)


def test_stale_timestamp_is_rejected_so_the_freshness_window_is_real():
    """Prove the signature can FAIL, and prove the 300s window is why.

    This is the failure mode that would look like a system fault: sign once at
    startup, and a long ladder starts 400ing partway through. The driver stamps
    per request; this is the test that says why it has to.
    """
    secret = "test-secret"
    body = loadtest.encode(loadtest.build_event(2))
    stale = loadtest.headers_for(body, secret, timestamp=int(time.time()) - 3600)
    assert not stripe_map.verify_stripe_signature(body, stale["stripe-signature"], secret)
    fresh = loadtest.headers_for(body, secret, timestamp=int(time.time()))
    assert stripe_map.verify_stripe_signature(body, fresh["stripe-signature"], secret)


def test_signing_a_reserialised_copy_would_fail():
    """The bug this guards: sign one serialisation, send another.

    json.dumps with default separators produces different bytes to the compact
    form the driver sends. If the driver ever signs a re-encoded copy, every
    request 401s and the run silently measures the auth path instead of the
    recovery path. Proving the wrong bytes FAIL is what makes the passing case
    above mean something.
    """
    import json

    event = loadtest.build_event(3)
    sent = loadtest.encode(event)
    other = json.dumps(event).encode("utf-8")  # default separators, unsorted keys
    assert sent != other
    ts = int(time.time())
    header = loadtest.headers_for(other, "test-secret", timestamp=ts)["stripe-signature"]
    assert not stripe_map.verify_stripe_signature(sent, header, "test-secret")


@pytest.mark.parametrize(
    "pct,expected",
    [(50, 5), (90, 9), (99, 10), (100, 10)],
)
def test_percentile_against_a_hand_checkable_set(pct, expected):
    # 1..10, so every percentile can be checked by counting on fingers.
    assert loadtest.percentile(list(range(1, 11)), pct) == expected


def test_percentile_sorts_rather_than_trusting_arrival_order():
    # Latencies arrive out of order under concurrency; that is the normal case.
    assert loadtest.percentile([10, 1, 9, 2, 8, 3, 7, 4, 6, 5], 50) == 5


def test_percentile_of_nothing_is_not_zero():
    # Zero would read as "instant". NaN reads as "no data", which is the truth.
    assert loadtest.percentile([], 50) != loadtest.percentile([], 50)  # NaN != NaN


def test_missing_database_is_reported_not_counted_as_empty(tmp_path):
    snap = loadtest.ledger_snapshot(str(tmp_path / "nope.db"))
    assert "unavailable" in snap
    assert "failures" not in snap


_LEDGER_DDL = (
    "CREATE TABLE failures (invoice_id TEXT, state TEXT, failure_code TEXT);"
    "CREATE TABLE transitions (invoice_id TEXT);"
    "CREATE TABLE messages (invoice_id TEXT);"
    "CREATE TABLE events (id TEXT);"
)


def test_empty_database_reports_zeros_and_is_distinguishable(tmp_path):
    db = tmp_path / "empty.db"
    con = sqlite3.connect(db)
    con.executescript(_LEDGER_DDL)
    con.commit()
    con.close()
    snap = loadtest.ledger_snapshot(str(db))
    assert "unavailable" not in snap
    assert snap["failures"] == 0
    assert snap["failures_without_state"] == 0
    assert snap["failures_without_reason"] == 0


def test_integrity_probes_actually_detect_the_things_they_look_for(tmp_path):
    """Prove each probe can FAIL, not just that it can pass.

    A check that has only ever returned zero has not been shown to work. Every
    row here is a deliberate defect of a different shape, and each probe must
    find exactly its own.
    """
    db = tmp_path / "orphan.db"
    con = sqlite3.connect(db)
    con.executescript(
        _LEDGER_DDL
        + "INSERT INTO failures VALUES "
        "('inv_ok','failed','expired_card'),"
        "('inv_no_state',NULL,'expired_card'),"
        "('inv_no_reason','failed',''),"
        "('inv_no_transition','failed','expired_card');"
        "INSERT INTO transitions VALUES ('inv_ok');"
        "INSERT INTO messages VALUES ('inv_ghost');"
    )
    con.commit()
    con.close()
    snap = loadtest.ledger_snapshot(str(db))
    assert snap["failures_without_state"] == 1
    assert snap["failures_without_reason"] == 1
    assert snap["messages_without_failure"] == 1
    # 3 of the 4 failures have no transition. Reported, not asserted.
    assert snap["failures_without_transition_REPORTED"] == 3


def test_transition_metric_is_reported_not_asserted():
    """The name carries the caveat, so nobody downstream reads it as a defect.

    This exists because the first version of the probe DID assert it, and would
    have published "every failure lost its audit trail" as a load-test finding
    when the real answer is that transitions are written on state change only.
    """
    src = (Path(__file__).resolve().parents[1] / "scripts" / "loadtest.py").read_text()
    assert "failures_without_transition_REPORTED" in src
    assert "failures_without_transition\"" not in src
