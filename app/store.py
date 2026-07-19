"""Durable state for the closed recovery loop.

Until now PayPilot was stateless: it answered a webhook and forgot. A closed
loop can't be: to say "we recovered EUR 4,120 this month" you have to remember
which invoices failed, which ones we actually messaged, and which later paid.
This module is that memory.

Three things live here:

* the SQLite schema (``failures`` / ``events`` / ``messages`` / ``transitions``),
* the per-invoice state machine (:data:`ALLOWED_TRANSITIONS`), which rejects
  illegal moves rather than silently overwriting state, and
* :class:`Store`, the only object that touches the database.

Design notes that are deliberate, not accidental:

* **Money is integer minor units plus a currency code.** No float ever holds an
  amount here; a mixed-currency billing run must never sum into one meaningless
  number.
* **Idempotency is a table, not a cache.** ``events`` records every Stripe event
  id we have processed, so a redelivery after a restart is still a no-op - an
  in-process LRU forgets, and a forgotten redelivery double-counts revenue.
* **Recovery can happen without us.** ``failed -> recovered`` is legal precisely
  so Stripe's own smart retries (and the holdout control group) are recorded.
  That path is the attribution baseline; forbidding it would flatter our numbers.

Obtained via :func:`get_store`, a lazily-built singleton, so tests can
monkeypatch the seam the same way they patch ``get_llm`` / ``get_retriever``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _REPO_ROOT / "data" / "paypilot.db"

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

STATE_FAILED = "failed"
STATE_MESSAGED = "messaged"
STATE_CLICKED = "clicked"
STATE_RECOVERED = "recovered"
STATE_CHURNED = "churned"
STATE_EXHAUSTED = "exhausted"

#: States from which nothing further happens. Reaching one closes the invoice.
TERMINAL_STATES = frozenset({STATE_RECOVERED, STATE_CHURNED, STATE_EXHAUSTED})

#: Legal moves per state. Anything absent raises :class:`InvalidTransition`.
#:
#: ``failed -> recovered`` is intentional: an invoice can pay itself off via
#: Stripe's retries with no dunning from us, and the dashboard's honest baseline
#: depends on those being recorded rather than rejected.
#:
#: ``messaged -> messaged`` and ``clicked -> messaged`` are intentional too: a
#: dunning sequence sends more than one touch, and a customer who opened the
#: portal but didn't pay still gets the next scheduled nudge.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_FAILED: frozenset(
        {STATE_MESSAGED, STATE_RECOVERED, STATE_CHURNED, STATE_EXHAUSTED}
    ),
    STATE_MESSAGED: frozenset(
        {STATE_MESSAGED, STATE_CLICKED, STATE_RECOVERED, STATE_CHURNED, STATE_EXHAUSTED}
    ),
    STATE_CLICKED: frozenset(
        {STATE_MESSAGED, STATE_RECOVERED, STATE_CHURNED, STATE_EXHAUSTED}
    ),
    STATE_RECOVERED: frozenset(),
    STATE_CHURNED: frozenset(),
    STATE_EXHAUSTED: frozenset(),
}


class InvalidTransition(ValueError):
    """Raised when a state move is not in :data:`ALLOWED_TRANSITIONS`.

    Loud on purpose. A dunning loop that silently accepts
    ``recovered -> churned`` will happily report revenue it later contradicts.
    """

    def __init__(self, invoice_id: str, from_state: str, to_state: str) -> None:
        super().__init__(
            f"invoice {invoice_id}: {from_state} -> {to_state} is not a legal transition"
        )
        self.invoice_id = invoice_id
        self.from_state = from_state
        self.to_state = to_state


class UnknownInvoice(KeyError):
    """Raised when a closed-loop event names an invoice we never saw fail."""


def can_transition(from_state: str, to_state: str) -> bool:
    """True when ``from_state -> to_state`` is a legal move."""
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    invoice_id              TEXT PRIMARY KEY,
    customer_id             TEXT NOT NULL,
    amount_minor            INTEGER NOT NULL,
    currency                TEXT NOT NULL,
    attempt_count           INTEGER NOT NULL DEFAULT 1,
    failure_code            TEXT NOT NULL,
    state                   TEXT NOT NULL,
    holdout                 INTEGER NOT NULL DEFAULT 0,
    recovered_amount_minor  INTEGER,
    failed_at               TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    recovered_at            TEXT,
    -- Stripe's own identifiers, kept alongside customer_id (which may be a
    -- local/demo id resolved from metadata). Closing events name the Stripe
    -- customer or subscription, so matching needs the real ones.
    stripe_customer_id      TEXT,
    subscription_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_failures_state ON failures(state);
CREATE INDEX IF NOT EXISTS idx_failures_customer ON failures(customer_id);

-- Processed Stripe event ids. Survives restarts, which an in-process cache does
-- not, so a redelivered invoice.paid cannot be counted as a second recovery.
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    invoice_id   TEXT,
    received_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id           TEXT NOT NULL,
    channel              TEXT NOT NULL,
    status               TEXT NOT NULL,
    provider_message_id  TEXT,
    attempt              INTEGER NOT NULL DEFAULT 1,
    error                TEXT,
    created_at           TEXT NOT NULL,
    sent_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_invoice ON messages(invoice_id);

-- Append-only history of every state move, so a disputed number can be traced
-- back to the events that produced it.
CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_invoice ON transitions(invoice_id);
"""


# Columns added after the first schema shipped. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so each is applied only when absent. This runs on
# every open: a client deploy that has been live for a month must pick up a new
# column on upgrade without anyone remembering to run a migration step.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("failures", "stripe_customer_id", "TEXT"),
    ("failures", "subscription_id", "TEXT"),
)

# Indexes over migrated columns, created after the columns are guaranteed to
# exist (an index in _SCHEMA would fail on a database predating the column).
_POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_failures_stripe_customer "
    "ON failures(stripe_customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_failures_subscription "
    "ON failures(subscription_id)",
)


def _now() -> str:
    """Current UTC instant as an ISO 8601 string (the storage format here)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """SQLite-backed persistence for failed invoices and their recovery state.

    One instance owns one connection. FastAPI runs sync endpoints in a
    threadpool, so the connection is opened with ``check_same_thread=False`` and
    every write is serialised behind a lock; WAL mode keeps concurrent readers
    from blocking.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path or os.getenv("PAYPILOT_DB_PATH") or _DEFAULT_DB_PATH)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring an older database up to the current schema, idempotently."""
        for table, column, coltype in _ADDED_COLUMNS:
            existing = {
                r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        for statement in _POST_MIGRATION_INDEXES:
            self._conn.execute(statement)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- idempotency ------------------------------------------------------

    def forget_event(self, event_id: str) -> None:
        """Undo :meth:`mark_event_seen` when processing failed.

        Claiming an event and then failing to process it is worse than not
        claiming it: Stripe retries after a 5xx, the retry hits the dedupe row,
        and the event is dropped for good. A lost ``invoice.payment_failed`` is
        a customer never dunned; a lost ``invoice.paid`` is revenue never
        recorded as recovered. Releasing the claim lets the retry work.
        """
        if not event_id:
            return
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
            self._conn.commit()

    def mark_event_seen(self, event_id: str, event_type: str, invoice_id: str | None = None) -> bool:
        """Record a Stripe event id; return True only the first time.

        The caller treats a False as "already handled, replay the ack and do
        nothing else". Insert-first (rather than check-then-insert) so two
        concurrent redeliveries can't both win the race.
        """
        if not event_id:
            # No id to dedupe on - let the caller proceed rather than drop work.
            return True
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events (event_id, event_type, invoice_id, received_at) "
                "VALUES (?, ?, ?, ?)",
                (event_id, event_type, invoice_id, _now()),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # -- failures ---------------------------------------------------------

    def record_failure(
        self,
        *,
        invoice_id: str,
        customer_id: str,
        amount_minor: int,
        currency: str,
        failure_code: str,
        attempt_count: int = 1,
        holdout: bool = False,
        stripe_customer_id: str | None = None,
        subscription_id: str | None = None,
    ) -> dict:
        """Persist a failed invoice, or bump an existing one's attempt count.

        Stripe fires ``invoice.payment_failed`` once per retry of the *same*
        invoice, so a repeat is an updated attempt on the row we already have,
        not a second failure. Re-inserting would double the "failed" count and
        inflate every rate computed from it.
        """
        now = _now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT state FROM failures WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO failures (invoice_id, customer_id, amount_minor, currency, "
                    "attempt_count, failure_code, state, holdout, failed_at, updated_at, "
                    "stripe_customer_id, subscription_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        invoice_id,
                        customer_id,
                        int(amount_minor),
                        currency.lower(),
                        int(attempt_count),
                        failure_code,
                        STATE_FAILED,
                        1 if holdout else 0,
                        now,
                        now,
                        stripe_customer_id,
                        subscription_id,
                    ),
                )
            else:
                # A later retry of an invoice we already closed must not reopen
                # it; record the attempt, leave the terminal state alone.
                # COALESCE keeps an identifier we already learned if this event
                # happens not to carry it.
                self._conn.execute(
                    "UPDATE failures SET attempt_count = ?, failure_code = ?, "
                    "amount_minor = ?, updated_at = ?, "
                    "stripe_customer_id = COALESCE(?, stripe_customer_id), "
                    "subscription_id = COALESCE(?, subscription_id) "
                    "WHERE invoice_id = ?",
                    (
                        int(attempt_count),
                        failure_code,
                        int(amount_minor),
                        now,
                        stripe_customer_id,
                        subscription_id,
                        invoice_id,
                    ),
                )
            self._conn.commit()
        return self.get_failure(invoice_id)

    def get_failure(self, invoice_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM failures WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_failures(self, state: str | None = None) -> list[dict]:
        if state:
            rows = self._conn.execute(
                "SELECT * FROM failures WHERE state = ? ORDER BY failed_at", (state,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM failures ORDER BY failed_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def open_failures(
        self,
        *,
        stripe_customer_id: str | None = None,
        subscription_id: str | None = None,
    ) -> list[dict]:
        """Non-terminal failures for a Stripe customer or subscription.

        ``customer.subscription.deleted`` names a subscription (and a customer),
        not an invoice, so churn has to be resolved to the invoices still open
        against it. Already-terminal rows are excluded: an invoice that was paid
        before the customer later cancelled was still a recovery, and marking it
        churned would erase a real win.
        """
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        params: list = list(TERMINAL_STATES)
        clauses = [f"state NOT IN ({placeholders})"]
        if subscription_id:
            clauses.append("subscription_id = ?")
            params.append(subscription_id)
        if stripe_customer_id:
            clauses.append("stripe_customer_id = ?")
            params.append(stripe_customer_id)
        if not subscription_id and not stripe_customer_id:
            return []
        rows = self._conn.execute(
            f"SELECT * FROM failures WHERE {' AND '.join(clauses)} ORDER BY failed_at",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # -- state machine ----------------------------------------------------

    def transition(
        self,
        invoice_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        recovered_amount_minor: int | None = None,
    ) -> bool:
        """Move an invoice to ``to_state``.

        Returns True if the state changed, False if the invoice was already
        there (an idempotent no-op - the shape a webhook redelivery takes).
        Raises :class:`InvalidTransition` for an illegal move and
        :class:`UnknownInvoice` for an invoice we never recorded as failed.
        """
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM failures WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
            if row is None:
                raise UnknownInvoice(invoice_id)
            from_state = row["state"]
            if from_state == to_state:
                return False
            if not can_transition(from_state, to_state):
                raise InvalidTransition(invoice_id, from_state, to_state)

            if to_state == STATE_RECOVERED:
                self._conn.execute(
                    "UPDATE failures SET state = ?, updated_at = ?, recovered_at = ?, "
                    "recovered_amount_minor = ? WHERE invoice_id = ?",
                    (to_state, now, now, recovered_amount_minor, invoice_id),
                )
            else:
                self._conn.execute(
                    "UPDATE failures SET state = ?, updated_at = ? WHERE invoice_id = ?",
                    (to_state, now, invoice_id),
                )
            self._conn.execute(
                "INSERT INTO transitions (invoice_id, from_state, to_state, reason, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (invoice_id, from_state, to_state, reason, now),
            )
            self._conn.commit()
        return True

    def count_ever_reached(self, state: str) -> int:
        """How many invoices have EVER been in ``state``, not how many are now.

        A current-state count answers a different question than a dashboard
        reader thinks it does: an invoice that was messaged and then recovered
        is no longer "messaged", so counting live states would report fewer
        messages the better the tool performed. The transitions table is
        append-only, so it can answer the cumulative question honestly.
        """
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT invoice_id) AS n FROM transitions WHERE to_state = ?",
            (state,),
        ).fetchone()
        return int(row["n"])

    def transitions_for(self, invoice_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM transitions WHERE invoice_id = ? ORDER BY id", (invoice_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- messages ---------------------------------------------------------

    def record_message(
        self,
        *,
        invoice_id: str,
        channel: str = "email",
        status: str = "queued",
        provider_message_id: str | None = None,
        attempt: int = 1,
        error: str | None = None,
    ) -> int:
        """Log one delivery attempt and return its row id."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages (invoice_id, channel, status, provider_message_id, "
                "attempt, error, created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invoice_id,
                    channel,
                    status,
                    provider_message_id,
                    int(attempt),
                    error,
                    now,
                    now if status == "sent" else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_message(
        self,
        message_id: int,
        *,
        status: str,
        provider_message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update a delivery attempt (send succeeded, failed, or later bounced)."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET status = ?, provider_message_id = "
                "COALESCE(?, provider_message_id), error = COALESCE(?, error), "
                "sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END WHERE id = ?",
                (status, provider_message_id, error, status, _now(), int(message_id)),
            )
            self._conn.commit()

    def messages_for(self, invoice_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE invoice_id = ? ORDER BY id", (invoice_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def find_message_by_provider_id(self, provider_message_id: str) -> dict | None:
        """Look up a delivery attempt by the provider's own id (bounce webhooks)."""
        if not provider_message_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM messages WHERE provider_message_id = ? ORDER BY id DESC LIMIT 1",
            (provider_message_id,),
        ).fetchone()
        return dict(row) if row else None

    def last_send_at(
        self, *, invoice_id: str | None = None, stripe_customer_id: str | None = None
    ) -> str | None:
        """When we last actually sent mail about this invoice or to this customer.

        Business-level deduplication, which is a different question from
        transport idempotency. Event dedupe answers "have I processed this
        delivery before"; this answers "have I already emailed this human about
        this recently". A customer with three failed invoices in one billing run
        generates three distinct events, none of them duplicates, and without
        this they receive three emails at once.

        Only ``sent`` counts. A dry run or a suppressed attempt did not reach a
        person and must not start a cooldown.
        """
        clauses, params = [], []
        if invoice_id:
            clauses.append("m.invoice_id = ?")
            params.append(invoice_id)
        if stripe_customer_id:
            clauses.append("f.stripe_customer_id = ?")
            params.append(stripe_customer_id)
        if not clauses:
            return None
        row = self._conn.execute(
            "SELECT MAX(m.sent_at) AS last FROM messages m "
            "LEFT JOIN failures f ON f.invoice_id = m.invoice_id "
            f"WHERE m.status = 'sent' AND ({' OR '.join(clauses)})",
            params,
        ).fetchone()
        return row["last"] if row and row["last"] else None

    def sent_message_count(self, invoice_id: str) -> int:
        """How many touches actually went out for this invoice (sequence caps)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE invoice_id = ? AND status = 'sent'",
            (invoice_id,),
        ).fetchone()
        return int(row["n"])


# ---------------------------------------------------------------------------
# Singleton seam
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_store: Store | None = None


def get_store() -> Store:
    """Return the process-wide :class:`Store`, building it on first use.

    The single seam the API and nodes go through, so tests monkeypatch this one
    function (mirroring ``app.nodes.get_llm`` / ``app.ingest.get_retriever``).
    """
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def reset_store(store: Store | None = None) -> None:
    """Replace the singleton. Test helper; also used to reopen a moved DB."""
    global _store
    with _store_lock:
        _store = store
