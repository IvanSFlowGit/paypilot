"""Tests for the durable recovery store and its state machine.

These cover the parts where a bug costs money rather than a 500: double-counted
recoveries, a redelivered webhook processed twice, an invoice reopened after it
closed, and money mangled by float arithmetic.

Every test uses an isolated on-disk DB in ``tmp_path`` so nothing touches the
real ``data/paypilot.db``.
"""

from __future__ import annotations

import pytest

from app.store import (
    STATE_CHURNED,
    STATE_CLICKED,
    STATE_EXHAUSTED,
    STATE_FAILED,
    STATE_MESSAGED,
    STATE_RECOVERED,
    InvalidTransition,
    Store,
    UnknownInvoice,
    can_transition,
    get_store,
    reset_store,
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _fail(store, invoice_id="in_test_1", **kw):
    """Record a representative failed invoice (EUR 49.00, expired card)."""
    params = {
        "invoice_id": invoice_id,
        "customer_id": "cus_test_1",
        "amount_minor": 4900,
        "currency": "eur",
        "failure_code": "card_expired",
    }
    params.update(kw)
    return store.record_failure(**params)


# ---------------------------------------------------------------------------
# Schema / recording
# ---------------------------------------------------------------------------

def test_record_failure_persists_minor_units_and_currency(store):
    row = _fail(store)
    assert row["amount_minor"] == 4900
    assert isinstance(row["amount_minor"], int)
    assert row["currency"] == "eur"
    assert row["state"] == STATE_FAILED
    assert row["holdout"] == 0


def test_currency_is_normalised_to_lowercase(store):
    row = _fail(store, currency="EUR")
    assert row["currency"] == "eur"


def test_repeat_failure_updates_attempt_rather_than_duplicating(store):
    """Stripe fires invoice.payment_failed once per retry of the SAME invoice.

    Inserting a second row would double the failed count and deflate every
    recovery rate computed from it.
    """
    _fail(store)
    _fail(store, attempt_count=2)
    assert len(store.list_failures()) == 1
    assert store.get_failure("in_test_1")["attempt_count"] == 2


def test_repeat_failure_does_not_reopen_a_closed_invoice(store):
    """A late retry webhook must not resurrect a recovered invoice."""
    _fail(store)
    store.transition("in_test_1", STATE_RECOVERED, recovered_amount_minor=4900)
    _fail(store, attempt_count=3)
    assert store.get_failure("in_test_1")["state"] == STATE_RECOVERED


def test_get_failure_returns_none_for_unknown_invoice(store):
    assert store.get_failure("in_nope") is None


def test_list_failures_filters_by_state(store):
    _fail(store, invoice_id="in_a")
    _fail(store, invoice_id="in_b")
    store.transition("in_b", STATE_MESSAGED)
    assert [r["invoice_id"] for r in store.list_failures(STATE_FAILED)] == ["in_a"]
    assert [r["invoice_id"] for r in store.list_failures(STATE_MESSAGED)] == ["in_b"]


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_happy_path_failed_to_recovered(store):
    _fail(store)
    assert store.transition("in_test_1", STATE_MESSAGED) is True
    assert store.transition("in_test_1", STATE_CLICKED) is True
    assert store.transition("in_test_1", STATE_RECOVERED, recovered_amount_minor=4900) is True

    row = store.get_failure("in_test_1")
    assert row["state"] == STATE_RECOVERED
    assert row["recovered_amount_minor"] == 4900
    assert row["recovered_at"]


def test_failed_to_recovered_directly_is_legal(store):
    """The attribution baseline: Stripe's own retry pays the invoice with no
    dunning from us. Rejecting this would erase the control group."""
    _fail(store, holdout=True)
    assert store.transition("in_test_1", STATE_RECOVERED, recovered_amount_minor=4900) is True
    assert store.get_failure("in_test_1")["holdout"] == 1


def test_resending_a_touch_keeps_the_messaged_state(store):
    """A dunning sequence sends more than once; messaged -> messaged is legal."""
    _fail(store)
    store.transition("in_test_1", STATE_MESSAGED)
    assert store.transition("in_test_1", STATE_MESSAGED) is False  # idempotent no-op


def test_clicked_can_be_nudged_again(store):
    _fail(store)
    store.transition("in_test_1", STATE_MESSAGED)
    store.transition("in_test_1", STATE_CLICKED)
    assert store.transition("in_test_1", STATE_MESSAGED) is True


@pytest.mark.parametrize(
    "terminal", [STATE_RECOVERED, STATE_CHURNED, STATE_EXHAUSTED]
)
def test_terminal_states_accept_no_further_moves(store, terminal):
    _fail(store)
    store.transition("in_test_1", terminal)
    with pytest.raises(InvalidTransition):
        store.transition("in_test_1", STATE_MESSAGED)


def test_recovered_cannot_become_churned(store):
    """The move that would let the dashboard contradict revenue it reported."""
    _fail(store)
    store.transition("in_test_1", STATE_RECOVERED, recovered_amount_minor=4900)
    with pytest.raises(InvalidTransition) as exc:
        store.transition("in_test_1", STATE_CHURNED)
    assert exc.value.from_state == STATE_RECOVERED
    assert exc.value.to_state == STATE_CHURNED


def test_transition_on_unknown_invoice_raises(store):
    """A paid event for an invoice we never saw fail is not ours to count."""
    with pytest.raises(UnknownInvoice):
        store.transition("in_never_failed", STATE_RECOVERED)


def test_repeated_recovery_is_an_idempotent_no_op(store):
    """A redelivered invoice.paid must not count as a second recovery."""
    _fail(store)
    assert store.transition("in_test_1", STATE_RECOVERED, recovered_amount_minor=4900) is True
    assert store.transition("in_test_1", STATE_RECOVERED, recovered_amount_minor=4900) is False


def test_transitions_are_recorded_in_order(store):
    _fail(store)
    store.transition("in_test_1", STATE_MESSAGED, reason="touch 1")
    store.transition("in_test_1", STATE_RECOVERED, recovered_amount_minor=4900, reason="invoice.paid")

    history = store.transitions_for("in_test_1")
    assert [(t["from_state"], t["to_state"]) for t in history] == [
        (STATE_FAILED, STATE_MESSAGED),
        (STATE_MESSAGED, STATE_RECOVERED),
    ]
    assert history[1]["reason"] == "invoice.paid"


def test_no_op_transition_writes_no_history(store):
    _fail(store)
    store.transition("in_test_1", STATE_MESSAGED)
    store.transition("in_test_1", STATE_MESSAGED)
    assert len(store.transitions_for("in_test_1")) == 1


def test_can_transition_matches_the_table():
    assert can_transition(STATE_FAILED, STATE_MESSAGED)
    assert can_transition(STATE_FAILED, STATE_RECOVERED)
    assert not can_transition(STATE_RECOVERED, STATE_MESSAGED)
    assert not can_transition(STATE_FAILED, "banana")


# ---------------------------------------------------------------------------
# Webhook idempotency
# ---------------------------------------------------------------------------

def test_event_is_only_seen_once(store):
    assert store.mark_event_seen("evt_1", "invoice.payment_failed") is True
    assert store.mark_event_seen("evt_1", "invoice.payment_failed") is False


def test_event_dedupe_survives_a_restart(store, tmp_path):
    """The reason this is a table and not an in-process cache."""
    store.mark_event_seen("evt_restart", "invoice.paid")
    store.close()

    reopened = Store(tmp_path / "test.db")
    try:
        assert reopened.mark_event_seen("evt_restart", "invoice.paid") is False
    finally:
        reopened.close()


def test_missing_event_id_does_not_drop_work(store):
    """No id to dedupe on: proceed rather than silently discard the event."""
    assert store.mark_event_seen("", "invoice.paid") is True


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def test_message_lifecycle_queued_to_sent(store):
    _fail(store)
    msg_id = store.record_message(invoice_id="in_test_1", status="queued")
    assert store.sent_message_count("in_test_1") == 0

    store.update_message(msg_id, status="sent", provider_message_id="rs_123")
    msg = store.messages_for("in_test_1")[0]
    assert msg["status"] == "sent"
    assert msg["provider_message_id"] == "rs_123"
    assert msg["sent_at"]
    assert store.sent_message_count("in_test_1") == 1


def test_bounce_marks_the_message_without_losing_the_provider_id(store):
    _fail(store)
    msg_id = store.record_message(invoice_id="in_test_1", status="sent",
                                  provider_message_id="rs_123")
    store.update_message(msg_id, status="bounced", error="mailbox_unavailable")

    msg = store.messages_for("in_test_1")[0]
    assert msg["status"] == "bounced"
    assert msg["provider_message_id"] == "rs_123"
    assert msg["error"] == "mailbox_unavailable"
    assert store.sent_message_count("in_test_1") == 0


def test_failed_sends_do_not_count_as_touches(store):
    """Sequence caps must count what landed, not what we attempted."""
    _fail(store)
    store.record_message(invoice_id="in_test_1", status="failed", attempt=1)
    store.record_message(invoice_id="in_test_1", status="sent", attempt=2)
    assert store.sent_message_count("in_test_1") == 1


# ---------------------------------------------------------------------------
# Singleton seam
# ---------------------------------------------------------------------------

def test_get_store_is_a_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(tmp_path / "singleton.db"))
    reset_store(None)
    try:
        assert get_store() is get_store()
    finally:
        reset_store(None)


def test_reset_store_swaps_the_instance(store):
    reset_store(store)
    try:
        assert get_store() is store
    finally:
        reset_store(None)


def test_db_path_honours_the_env_var(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "custom.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(target))
    s = Store()
    try:
        assert s.path == str(target)
        assert target.exists()  # parent dir created, schema written
    finally:
        s.close()
