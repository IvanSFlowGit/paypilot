# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Durable state for the closed recovery loop.

Until now PayPilot was stateless: it answered a webhook and forgot. A closed
loop can't be: to say "we recovered EUR 4,120 this month" you have to remember
which invoices failed, which ones we actually messaged, and which later paid.
This module is that memory.

Three things live here:

* the SQLite schema (``failures`` / ``events`` / ``messages`` / ``transitions``),
* the per-invoice state machine (:data:`ALLOWED_TRANSITIONS`), which rejects
  illegal moves rather than silently overwriting state, and
* the data layer: :class:`Database` owns the connection, lock and schema;
  :class:`Store` is a view bound to one tenant; :class:`Platform` holds the few
  operations that span tenants.

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
* **Tenancy is enforced here, in the one data layer.** Every table carries a
  ``tenant_id``, and a :class:`Store` method never takes one as an argument: the
  tenant is bound when the view is constructed. See ``docs/multi-tenancy.md``.

The process-wide default is obtained via :func:`get_store`, a lazily-built
singleton, so tests can monkeypatch the seam the same way they patch
``get_llm`` / ``get_retriever``. In ``PAYPILOT_TENANCY=multi`` it refuses.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _REPO_ROOT / "data" / "paypilot.db"

#: The deployment's own tenant. Its settings come from the environment.
DEFAULT_TENANT = "default"

#: Single mode binds this tenant. Unset means :data:`DEFAULT_TENANT`. The public
#: app sets it to ``demo`` so the ledger it migrated is the one it reads.
ENV_TENANT = "PAYPILOT_TENANT"

_TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

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


class LegacySchemaError(RuntimeError):
    """The database is in the pre-tenancy layout and has not been migrated.

    Raised instead of migrating: a migration moves every row to a tenant, so it
    is an explicit step (``python -m app.migrate_tenancy``), never a side effect
    of opening a file.
    """


class TenantMismatchError(RuntimeError):
    """Single mode: the configured tenant is not the tenant that owns this file.

    Raised so an operator who forgets ``PAYPILOT_TENANT`` gets a refusal rather
    than an empty ledger that looks like "no data held".
    """


class AmbientTenantError(RuntimeError):
    """:func:`get_store` was called in ``PAYPILOT_TENANCY=multi``.

    In multi mode there is no ambient tenant. Code that touches tenant data must
    be handed a tenant-bound :class:`Store`.
    """


def can_transition(from_state: str, to_state: str) -> bool:
    """True when ``from_state -> to_state`` is a legal move."""
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Every table the tenant-scoped data layer may name. Dynamic SQL draws table
#: names only from here, never from caller input.
TENANT_TABLES = ("failures", "events", "messages", "transitions", "approvals", "early_paid")
_ALLOWED_TABLES = frozenset(TENANT_TABLES) | {"tenants"}

TENANT_STATUSES = ("active", "suspended", "offboarding", "offboarded")

#: Statuses in which a tenant is no longer served: tokens do not resolve, webhooks answer uniformly,
#: nothing is sent. ``offboarding`` is the state between "the operator started" and "the erase
#: finished", so a tenant is never live while its data is being exported or erased, and an erase
#: that fails part-way can be run again.
GONE_STATUSES = frozenset({"offboarding", "offboarded"})

#: Tables whose rows the migration moves into a tenant. ``early_paid`` and
#: ``tenants`` are new and start empty.
MIGRATED_TABLES = ("failures", "events", "messages", "transitions", "approvals")

#: Tables with an AUTOINCREMENT key, whose sequence the migration carries over.
_AUTOINCREMENT_TABLES = ("messages", "transitions", "approvals")

#: Event type under which the early-paid marker is recorded. Stripe never sends
#: this type, so a signed webhook cannot produce it.
EARLY_PAID_EVENT_TYPE = "invoice.paid.early"

# Each DDL template takes ``{name}`` so the migration can build ``<table>__new``
# with the identical definition before renaming it into place.
_TABLE_DDL = {
    "failures": """
CREATE TABLE IF NOT EXISTS {name} (
    tenant_id               TEXT NOT NULL,
    invoice_id              TEXT NOT NULL,
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
    subscription_id         TEXT,
    PRIMARY KEY (tenant_id, invoice_id)
);""",
    # Processed Stripe event ids. Survives restarts, which an in-process cache
    # does not, so a redelivered invoice.paid cannot be counted twice.
    "events": """
CREATE TABLE IF NOT EXISTS {name} (
    tenant_id    TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    invoice_id   TEXT,
    received_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id)
);""",
    "messages": """
CREATE TABLE IF NOT EXISTS {name} (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id            TEXT NOT NULL,
    invoice_id           TEXT NOT NULL,
    channel              TEXT NOT NULL,
    status               TEXT NOT NULL,
    provider_message_id  TEXT,
    attempt              INTEGER NOT NULL DEFAULT 1,
    error                TEXT,
    created_at           TEXT NOT NULL,
    sent_at              TEXT
);""",
    # Append-only history of every state move, so a disputed number can be traced
    # back to the events that produced it.
    "transitions": """
CREATE TABLE IF NOT EXISTS {name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    invoice_id  TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    at          TEXT NOT NULL
);""",
    # Human approval of outbound mail. One row per message awaiting (or having had)
    # a person's decision. ``body`` is the exact text that will leave, frozen at
    # creation, and ``body_sha256`` pins it, so what the approver read is what is
    # sent. The raw approval token is never stored, only its hash.
    "approvals": """
CREATE TABLE IF NOT EXISTS {name} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL,
    invoice_id      TEXT NOT NULL,
    channel         TEXT NOT NULL,
    status          TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    body_sha256     TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT,
    error           TEXT,
    external_id     TEXT,
    gone_since      TEXT
);""",
    # The early-paid marker. It was a salted key inside ``events``, regenerated per
    # process, so it was lost on every restart. A table keyed on the invoice has
    # no salt to lose.
    "early_paid": """
CREATE TABLE IF NOT EXISTS {name} (
    tenant_id  TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, invoice_id)
);""",
    # One row, written by the migration in the same transaction as the move: the
    # tenant that owns this file. A process bound to another tenant refuses to
    # open it as its default, so an empty ledger can never read as "no data held".
    "tenancy_meta": """
CREATE TABLE IF NOT EXISTS {name} (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);""",
    # Per-tenant configuration. One row per tenant: behaviour as JSON, secrets as a
    # Fernet blob under PAYPILOT_TENANT_KEY (app/tenant_config.py). Additive and
    # empty on creation, so no existing row is touched by opening an old file.
    "tenant_config": """
CREATE TABLE IF NOT EXISTS {name} (
    tenant_id      TEXT PRIMARY KEY,
    behaviour_json TEXT NOT NULL,
    secrets_enc    BLOB,
    updated_at     TEXT NOT NULL
);""",
    # Bearer tokens (app/principals.py). Platform-level: a token is looked up by its id BEFORE the
    # caller's tenant is known, so this table carries the tenant rather than being filtered by it.
    # Only a SHA-256 of the secret is stored; the raw token is shown once at creation.
    "api_tokens": """
CREATE TABLE IF NOT EXISTS {name} (
    token_id       TEXT PRIMARY KEY,
    tenant_id      TEXT,
    role           TEXT NOT NULL,
    secret_hash    TEXT NOT NULL,
    label          TEXT,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT,
    revoked_at     TEXT,
    revoked_reason TEXT
);""",
    "tenants": """
CREATE TABLE IF NOT EXISTS {name} (
    id                    TEXT PRIMARY KEY,
    slug                  TEXT NOT NULL UNIQUE,
    name                  TEXT NOT NULL,
    status                TEXT NOT NULL,
    status_reason         TEXT,
    config_json           TEXT,
    secrets_enc           TEXT,
    verified_from_domains TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);""",
}

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_failures_state ON failures(tenant_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_failures_customer ON failures(tenant_id, customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_failures_stripe_customer "
    "ON failures(tenant_id, stripe_customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_failures_subscription "
    "ON failures(tenant_id, subscription_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_invoice ON messages(tenant_id, invoice_id)",
    # Platform-level lookup for provider callbacks: the one index with no tenant.
    "CREATE INDEX IF NOT EXISTS idx_messages_provider_id ON messages(provider_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_transitions_invoice "
    "ON transitions(tenant_id, invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_invoice ON approvals(tenant_id, invoice_id)",
    # At most one open request per invoice PER TENANT: a Stripe retry storm must
    # not stack five approval emails in the approver's inbox. Global, this index
    # would also tell tenant B that tenant A has a request open for the same id.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_one_pending "
    "ON approvals(tenant_id, invoice_id) WHERE status = 'pending'",
)


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index that does not yet exist. Idempotent."""
    for name in _TABLE_DDL:
        conn.execute(_TABLE_DDL[name].format(name=name))
    for statement in _INDEX_DDL:
        conn.execute(statement)


def _is_legacy(conn: sqlite3.Connection) -> bool:
    """True when ``failures`` exists and predates the ``tenant_id`` column."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(failures)")}
    return bool(cols) and "tenant_id" not in cols


def _check_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(f"invalid tenant id {tenant_id!r}")
    return tenant_id


def configured_tenant() -> str:
    """The tenant single mode binds: ``PAYPILOT_TENANT``, or ``default`` when unset."""
    raw = os.getenv(ENV_TENANT)
    return DEFAULT_TENANT if raw is None else _check_tenant_id(raw)


def _guard_table(table: str) -> str:
    """Refuse any table name outside the allowlist before it reaches SQL."""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"table {table!r} is not a tenant table")
    return table


def early_paid_key(invoice_id: str) -> str:
    """Opaque key for the early-paid marker, kept for existing callers.

    The marker is no longer stored under this key. :meth:`Store.mark_event_seen`
    recognises :data:`EARLY_PAID_EVENT_TYPE` and writes the ``early_paid`` table,
    so the key is never a row in ``events`` and a webhook event id cannot forge
    or collide with it.
    """
    return f"{EARLY_PAID_EVENT_TYPE}:{invoice_id}"


def _now() -> str:
    """Current UTC instant as an ISO 8601 string (the storage format here)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------


class Database:
    """One SQLite file: its connection, its lock and its schema.

    Every :class:`Store` and the :class:`Platform` built on the same file share
    this object, so one writer lock serialises all tenants, as before. FastAPI
    runs sync endpoints in a threadpool, so the connection is opened with
    ``check_same_thread=False`` and every write is serialised behind the lock;
    WAL mode keeps concurrent readers from blocking.

    Refuses a pre-tenancy file with :class:`LegacySchemaError`. It never
    migrates one.
    """

    def __init__(
        self, path: str | Path | None = None, owner_tenant: str | None = None
    ) -> None:
        # The owner is validated BEFORE any directory or file is touched: an invalid
        # tenant must leave nothing behind, not a half built schema.
        owner = (
            _check_tenant_id(owner_tenant) if owner_tenant is not None else configured_tenant()
        )
        self.path = str(path or os.getenv("PAYPILOT_DB_PATH") or _DEFAULT_DB_PATH)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self.conn.row_factory = sqlite3.Row
            # Checked before any write-capable pragma, so a refused file is read and
            # nothing else: switching it to WAL would rewrite its header.
            if _is_legacy(self.conn):
                raise LegacySchemaError(
                    f"{self.path} has the pre-tenancy schema and has not been migrated. "
                    "Run `python -m app.migrate_tenancy` with PAYPILOT_LEGACY_TENANT set."
                )
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            # Schema and owner are one transaction: a file is never left with tables
            # and no owner. A file this process creates is owned by the tenant it was
            # created for, from its first byte.
            self.conn.execute("BEGIN IMMEDIATE")
            fresh = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'failures'"
            ).fetchone() is None
            _apply_schema(self.conn)
            if fresh:
                self.conn.execute(
                    "INSERT OR IGNORE INTO tenancy_meta (key, value) VALUES ('owner_tenant', ?)",
                    (owner,),
                )
            self.conn.commit()
        except BaseException:
            self.conn.close()
            raise

    def owner_tenant(self) -> str | None:
        """The tenant the migration recorded as owning this file, or None if unrecorded."""
        row = self.conn.execute(
            "SELECT value FROM tenancy_meta WHERE key = 'owner_tenant'"
        ).fetchone()
        return row["value"] if row else None

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class Store:
    """A tenant-bound view of one :class:`Database`.

    ``Store(path)`` with no tenant binds the configured tenant
    (``PAYPILOT_TENANT``, default ``default``), so every existing single-mode caller
    keeps working. In multi mode that form raises :class:`AmbientTenantError`: a
    tenant must be named. No method takes a tenant id as an argument: the tenant
    is fixed at construction, and every statement this object issues carries its
    predicate.
    """

    def __init__(
        self,
        path_or_db: str | Path | Database | None = None,
        tenant_id: str | None = None,
    ) -> None:
        mode = tenancy_mode()
        if tenant_id is None and mode == "multi":
            raise AmbientTenantError(
                "PAYPILOT_TENANCY=multi: Store() needs an explicit tenant_id"
            )
        # The bound tenant is validated before the file is opened, so an invalid
        # tenant creates nothing. A file created here is owned by the tenant bound.
        bound = _check_tenant_id(tenant_id) if tenant_id is not None else configured_tenant()
        if isinstance(path_or_db, Database):
            db = path_or_db
            owns = False
        else:
            db = Database(path_or_db, owner_tenant=bound)
            owns = True
        try:
            self.tenant_id = bound
            # Owner rule, decided here: in single mode a file has ONE owner and any
            # bind to another tenant is refused, explicit or not. In multi mode a file
            # holds several tenants by design, so the owner row is not enforced.
            owner = db.owner_tenant()
            if mode == "single" and owner is not None and owner != self.tenant_id:
                raise TenantMismatchError(
                    f"{db.path} is owned by tenant {owner!r} but this store binds "
                    f"{self.tenant_id!r}. Bind {owner!r} to read it."
                )
        except BaseException:
            if owns:
                db.close()
            raise
        self._db = db
        self._owns_db = owns
        self.path = self._db.path
        self._lock = self._db.lock
        self._conn = self._db.conn

    @property
    def database(self) -> Database:
        """The shared database this view is bound to (for tenant configuration reads)."""
        return self._db

    def close(self) -> None:
        if self._owns_db:
            self._db.close()

    # -- idempotency ------------------------------------------------------

    def was_paid_early(self, invoice_id: str) -> bool:
        """Whether an invoice.paid was seen before its payment_failed.

        Stripe makes no ordering guarantee, and the out-of-order case is the
        one where we would dun a customer who has already settled.
        """
        if not invoice_id:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM early_paid WHERE tenant_id = ? AND invoice_id = ?",
            (self.tenant_id, invoice_id),
        ).fetchone()
        return row is not None

    def mark_early_paid(self, invoice_id: str) -> bool:
        """Record that ``invoice_id`` was paid before its failure arrived.

        True only the first time. Survives a restart, which the salted marker in
        ``events`` did not.
        """
        if not invoice_id:
            return False
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO early_paid (tenant_id, invoice_id, seen_at) "
                "VALUES (?, ?, ?)",
                (self.tenant_id, invoice_id, _now()),
            )
            self._conn.commit()
            return cur.rowcount == 1

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
            self._conn.execute(
                "DELETE FROM events WHERE tenant_id = ? AND event_id = ?",
                (self.tenant_id, event_id),
            )
            self._conn.commit()

    def mark_event_seen(self, event_id: str, event_type: str, invoice_id: str | None = None) -> bool:
        """Record a Stripe event id; return True only the first time.

        The caller treats a False as "already handled, replay the ack and do
        nothing else". Insert-first (rather than check-then-insert) so two
        concurrent redeliveries can't both win the race.

        COMPATIBILITY PATH, deliberately narrow. ``app/loop.py`` still records the
        early-paid marker by calling this method with :data:`EARLY_PAID_EVENT_TYPE`,
        and that file is outside this milestone. Such a call is forwarded to
        :meth:`mark_early_paid`, so the marker never lives in ``events``. New code
        should call :meth:`mark_early_paid` directly. Stripe never sends that event
        type, so a signed webhook cannot reach this branch.
        """
        if event_type == EARLY_PAID_EVENT_TYPE and invoice_id:
            return self.mark_early_paid(invoice_id)
        if not event_id:
            # No id to dedupe on - let the caller proceed rather than drop work.
            return True
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events (tenant_id, event_id, event_type, invoice_id, "
                "received_at) VALUES (?, ?, ?, ?, ?)",
                (self.tenant_id, event_id, event_type, invoice_id, _now()),
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
                "SELECT state FROM failures WHERE tenant_id = ? AND invoice_id = ?",
                (self.tenant_id, invoice_id),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO failures (tenant_id, invoice_id, customer_id, amount_minor, "
                    "currency, attempt_count, failure_code, state, holdout, failed_at, "
                    "updated_at, stripe_customer_id, subscription_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.tenant_id,
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
            elif existing["state"] in TERMINAL_STATES:
                # A later retry of an invoice we already closed must not reopen
                # it; record the attempt, leave the terminal state alone. A
                # terminal row keeps its amount too. Preserving only the STATE
                # let a late payment_failed rewrite amount_minor on a recovered
                # invoice, so "value at risk" moved retroactively while "value
                # recovered" stayed fixed. Stripe guarantees no ordering, so late
                # events are normal, not exceptional.
                # COALESCE keeps an identifier we already learned if this event
                # happens not to carry it.
                self._conn.execute(
                    "UPDATE failures SET attempt_count = ?, failure_code = ?, "
                    "updated_at = ?, "
                    "stripe_customer_id = COALESCE(?, stripe_customer_id), "
                    "subscription_id = COALESCE(?, subscription_id) "
                    "WHERE tenant_id = ? AND invoice_id = ?",
                    (int(attempt_count), failure_code, now, stripe_customer_id,
                     subscription_id, self.tenant_id, invoice_id),
                )
            else:
                self._conn.execute(
                    "UPDATE failures SET attempt_count = ?, failure_code = ?, "
                    "amount_minor = ?, updated_at = ?, "
                    "stripe_customer_id = COALESCE(?, stripe_customer_id), "
                    "subscription_id = COALESCE(?, subscription_id) "
                    "WHERE tenant_id = ? AND invoice_id = ?",
                    (int(attempt_count), failure_code, int(amount_minor), now,
                     stripe_customer_id, subscription_id, self.tenant_id, invoice_id),
                )
            self._conn.commit()
        return self.get_failure(invoice_id)

    def get_failure(self, invoice_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM failures WHERE tenant_id = ? AND invoice_id = ?",
            (self.tenant_id, invoice_id),
        ).fetchone()
        return dict(row) if row else None

    def list_failures(self, state: str | None = None) -> list[dict]:
        if state:
            rows = self._conn.execute(
                "SELECT * FROM failures WHERE tenant_id = ? AND state = ? ORDER BY failed_at",
                (self.tenant_id, state),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM failures WHERE tenant_id = ? ORDER BY failed_at",
                (self.tenant_id,),
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
        if not subscription_id and not stripe_customer_id:
            return []
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        params: list = [self.tenant_id, *sorted(TERMINAL_STATES)]
        clauses = []
        if subscription_id:
            clauses.append("subscription_id = ?")
            params.append(subscription_id)
        if stripe_customer_id:
            clauses.append("stripe_customer_id = ?")
            params.append(stripe_customer_id)
        rows = self._conn.execute(
            "SELECT * FROM failures WHERE tenant_id = ? "
            f"AND state NOT IN ({placeholders}) AND ({' OR '.join(clauses)}) "
            "ORDER BY failed_at",
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
                "SELECT state FROM failures WHERE tenant_id = ? AND invoice_id = ?",
                (self.tenant_id, invoice_id),
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
                    "recovered_amount_minor = ? WHERE tenant_id = ? AND invoice_id = ?",
                    (to_state, now, now, recovered_amount_minor, self.tenant_id, invoice_id),
                )
            else:
                self._conn.execute(
                    "UPDATE failures SET state = ?, updated_at = ? "
                    "WHERE tenant_id = ? AND invoice_id = ?",
                    (to_state, now, self.tenant_id, invoice_id),
                )
            self._conn.execute(
                "INSERT INTO transitions (tenant_id, invoice_id, from_state, to_state, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.tenant_id, invoice_id, from_state, to_state, reason, now),
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
            "SELECT COUNT(DISTINCT invoice_id) AS n FROM transitions "
            "WHERE tenant_id = ? AND to_state = ?",
            (self.tenant_id, state),
        ).fetchone()
        return int(row["n"])

    def backdate(
        self,
        invoice_id: str,
        *,
        failed_at: str | None = None,
        recovered_at: str | None = None,
    ) -> None:
        """Rewrite an invoice's timestamps.

        For historical backfill when a client imports past invoices, and for
        building sample data. NOT part of the normal loop: the live path always
        stamps the real instant, because time-to-recovery is a number the
        dashboard reports and it must not be settable by an event payload.
        """
        sets, params = [], []
        if failed_at:
            sets.append("failed_at = ?")
            params.append(failed_at)
        if recovered_at:
            sets.append("recovered_at = ?")
            params.append(recovered_at)
        if not sets:
            return
        params.extend([self.tenant_id, invoice_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE failures SET {', '.join(sets)} "
                "WHERE tenant_id = ? AND invoice_id = ?",
                params,
            )
            self._conn.commit()

    def transitions_for(self, invoice_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM transitions WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",
            (self.tenant_id, invoice_id),
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
            try:
                cur = self._conn.execute(
                    "INSERT INTO messages (tenant_id, invoice_id, channel, status, "
                    "provider_message_id, attempt, error, created_at, sent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.tenant_id,
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
            except BaseException:
                # A failed COMMIT leaves the INSERT in the open transaction. A caller that retries
                # (the mailer does, after a 2xx) would then commit BOTH rows.
                self._conn.rollback()
                raise
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
                "sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END "
                "WHERE tenant_id = ? AND id = ?",
                (status, provider_message_id, error, status, _now(),
                 self.tenant_id, int(message_id)),
            )
            self._conn.commit()

    def messages_for(self, invoice_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",
            (self.tenant_id, invoice_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_message_by_provider_id(self, provider_message_id: str) -> dict | None:
        """Look up a delivery attempt by the provider's own id (bounce webhooks)."""
        if not provider_message_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM messages WHERE tenant_id = ? AND provider_message_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (self.tenant_id, provider_message_id),
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
            # Match either identifier column. An invoice may carry only the
            # local/demo customer id, and matching just the Stripe column made
            # the per-customer window silently disappear for those rows.
            clauses.append("(f.stripe_customer_id = ? OR f.customer_id = ?)")
            params.extend([stripe_customer_id, stripe_customer_id])
        if not clauses:
            return None
        row = self._conn.execute(
            "SELECT MAX(m.sent_at) AS last FROM messages m "
            "LEFT JOIN failures f ON f.invoice_id = m.invoice_id AND f.tenant_id = m.tenant_id "
            "WHERE m.tenant_id = ? AND m.status IN ('sent', 'bounced') "
            f"AND ({' OR '.join(clauses)})",
            [self.tenant_id, *params],
        ).fetchone()
        return row["last"] if row and row["last"] else None

    def sent_message_count(self, invoice_id: str) -> int:
        """How many touches actually went out for this invoice (sequence caps)."""
        # 'bounced' counts too. A bounce means we DID send: the message left,
        # the mailbox rejected it. Counting only 'sent' let a bounce reset the
        # cap, so a dead mailbox received more mail than a live one - the
        # opposite of what a bounce should cause, and a sender-reputation risk.
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE tenant_id = ? AND invoice_id = ? "
            "AND status IN ('sent', 'bounced')",
            (self.tenant_id, invoice_id),
        ).fetchone()
        return int(row["n"])

    # -- human approval ----------------------------------------------------

    def create_approval(
        self,
        *,
        invoice_id: str,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        token_hash: str,
        expires_at: str,
        external_id: str | None = None,
    ) -> int | None:
        """Open one approval request. ``None`` when one is already pending.

        The partial unique index is the arbiter, not a read-then-insert, so two
        concurrent webhook deliveries cannot both open a request. The index is
        per tenant, so a pending request in another tenant neither blocks this
        one nor reveals itself by refusing.
        """
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO approvals (tenant_id, invoice_id, channel, status, recipient, "
                    "subject, body, body_sha256, token_hash, created_at, expires_at, "
                    "external_id) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.tenant_id, invoice_id, channel, recipient, subject, body,
                     body_sha256, token_hash, _now(), expires_at, external_id),
                )
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return None
            self._conn.commit()
            return int(cur.lastrowid)

    def get_approval(self, approval_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE tenant_id = ? AND id = ?",
            (self.tenant_id, int(approval_id)),
        ).fetchone()
        return dict(row) if row else None

    def approvals_for(self, invoice_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",
            (self.tenant_id, invoice_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_approvals(self, status: str | None = None, limit: int = 100) -> list[dict]:
        """Approvals newest first, WITHOUT the body or token hash.

        This feeds an admin listing; the body is customer-facing text carrying
        a personal link and the hash is a credential check value. Neither
        belongs in a list view.
        """
        cols = ("id, invoice_id, channel, status, recipient, subject, body_sha256, "
                "created_at, expires_at, resolved_at, resolved_by, error")
        if status:
            rows = self._conn.execute(
                f"SELECT {cols} FROM approvals WHERE tenant_id = ? AND status = ? "
                "ORDER BY id DESC LIMIT ?",
                (self.tenant_id, status, int(limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {cols} FROM approvals WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                (self.tenant_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_approval(self, approval_id: int, *, by: str, now_iso: str | None = None) -> bool:
        """Atomically move ``pending -> approved``. True for exactly one caller.

        Expiry is part of the same statement, so an approval cannot be claimed
        in the gap between an expiry check and the update. Two taps, a double
        click and a mail scanner replaying a POST all lose after the first.
        """
        now = now_iso or _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status = 'approved', resolved_at = ?, "
                "resolved_by = ? WHERE tenant_id = ? AND id = ? AND status = 'pending' "
                "AND expires_at > ?",
                (now, by, self.tenant_id, int(approval_id), now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def resolve_approval(
        self,
        approval_id: int,
        status: str,
        *,
        by: str | None = None,
        error: str | None = None,
        only_from: tuple[str, ...] = ("pending", "approved"),
    ) -> bool:
        """Set a terminal status. Refuses to rewrite an already-final row."""
        ph = ",".join("?" for _ in only_from)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status = ?, resolved_at = COALESCE(resolved_at, ?), "
                "resolved_by = COALESCE(?, resolved_by), error = ? "
                f"WHERE tenant_id = ? AND id = ? AND status IN ({ph})",
                (status, _now(), by, error, self.tenant_id, int(approval_id), *only_from),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def expire_approvals(self, now_iso: str | None = None) -> list[dict]:
        """Mark every pending approval past its deadline as expired; return them."""
        now = now_iso or _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, invoice_id FROM approvals WHERE tenant_id = ? "
                "AND status = 'pending' AND expires_at <= ?",
                (self.tenant_id, now),
            ).fetchall()
            self._conn.execute(
                "UPDATE approvals SET status = 'expired', resolved_at = ?, "
                "resolved_by = 'system' WHERE tenant_id = ? AND status = 'pending' "
                "AND expires_at <= ?",
                (now, self.tenant_id, now),
            )
            self._conn.commit()
        return [dict(r) for r in rows]

    def expire_all_pending_approvals(self, by: str = "system") -> list[dict]:
        """Expire every pending approval now, deadline or not (tenant suspension). Returns them."""
        now = _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, invoice_id FROM approvals WHERE tenant_id = ? AND status = 'pending'",
                (self.tenant_id,),
            ).fetchall()
            self._conn.execute(
                "UPDATE approvals SET status = 'expired', resolved_at = ?, resolved_by = ? "
                "WHERE tenant_id = ? AND status = 'pending'",
                (now, by, self.tenant_id),
            )
            self._conn.commit()
        return [dict(r) for r in rows]

    def set_approval_external_id(self, approval_id: int, external_id: str) -> None:
        """Remember the mailbox's id for the draft (Gmail draft id, IMAP marker)."""
        with self._lock:
            self._conn.execute(
                "UPDATE approvals SET external_id = ? WHERE tenant_id = ? AND id = ?",
                (external_id, self.tenant_id, int(approval_id)),
            )
            self._conn.commit()

    def mark_approval_gone(self, approval_id: int, gone: bool) -> str | None:
        """Record when a mailbox draft was first seen missing (None = present)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT gone_since FROM approvals WHERE tenant_id = ? AND id = ?",
                (self.tenant_id, int(approval_id)),
            ).fetchone()
            current = row["gone_since"] if row else None
            new = (current or _now()) if gone else None
            self._conn.execute(
                "UPDATE approvals SET gone_since = ? WHERE tenant_id = ? AND id = ?",
                (new, self.tenant_id, int(approval_id)),
            )
            self._conn.commit()
            return new

    # -- GDPR: subject access, erasure, retention -------------------------

    def _invoice_ids_for_customer(self, customer_id: str) -> list[str]:
        """Every invoice_id belonging to a customer, matched on EITHER id column.

        A failed invoice may carry only the local/demo ``customer_id`` or, once
        Stripe has resolved it, the real ``stripe_customer_id``. A GDPR request
        names one of them and must reach the rows keyed by the other, so both
        columns are matched - the same reason ``last_send_at`` matches both.
        """
        if not customer_id:
            return []
        rows = self._conn.execute(
            "SELECT invoice_id FROM failures WHERE tenant_id = ? "
            "AND (customer_id = ? OR stripe_customer_id = ?)",
            (self.tenant_id, customer_id, customer_id),
        ).fetchall()
        return [r["invoice_id"] for r in rows]

    def export_customer(self, customer_id: str) -> dict:
        """All ledger data held for one customer (GDPR right of access).

        Returns the ``failures`` rows plus every child ``messages``,
        ``transitions`` and ``events`` row linked by ``invoice_id``. Empty lists
        (not an error) when nothing is held, so a caller can prove "we hold no
        data for this subject".
        """
        invoice_ids = self._invoice_ids_for_customer(customer_id)
        failures, messages, transitions, events, approvals = [], [], [], [], []
        early_paid = []
        for inv in invoice_ids:
            approvals.extend(self.approvals_for(inv))
            failures.append(self.get_failure(inv))
            messages.extend(self.messages_for(inv))
            transitions.extend(self.transitions_for(inv))
            events.extend(
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM events WHERE tenant_id = ? AND invoice_id = ? "
                    "ORDER BY received_at",
                    (self.tenant_id, inv),
                ).fetchall()
            )
            early_paid.extend(
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM early_paid WHERE tenant_id = ? AND invoice_id = ?",
                    (self.tenant_id, inv),
                ).fetchall()
            )
        return {
            "customer_id": customer_id,
            "invoice_ids": invoice_ids,
            "failures": [f for f in failures if f],
            "messages": messages,
            "transitions": transitions,
            "events": events,
            "approvals": approvals,
            "early_paid": early_paid,
        }

    def erase_customer(self, customer_id: str) -> dict:
        """Delete every ledger row for one customer (GDPR right to erasure).

        Removes the ``failures`` rows and all child ``messages``/``transitions``/
        ``events`` rows linked by ``invoice_id``, in one transaction. Returns the
        per-table count deleted, so the caller (and its test) can prove the rows
        are gone rather than assuming it.
        """
        return self._delete_invoices(self._invoice_ids_for_customer(customer_id))

    def export_tenant(self) -> dict:
        """Everything this tenant's ledger holds, for the offboarding export."""
        out: dict = {"tenant_id": self.tenant_id}
        with self._lock:
            for table in TENANT_TABLES:
                rows = self._conn.execute(
                    f"SELECT * FROM {_guard_table(table)} WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchall()
                out[table] = [dict(r) for r in rows]
        return out

    def erase_tenant(self) -> dict:
        """Delete every ledger row this tenant owns, in one transaction. Returns per-table counts."""
        counts: dict = {}
        with self._lock:
            try:
                for table in TENANT_TABLES:
                    cur = self._conn.execute(
                        f"DELETE FROM {_guard_table(table)} WHERE tenant_id = ?", (self.tenant_id,)
                    )
                    counts[table] = cur.rowcount
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return counts

    def expired_invoice_ids(
        self, before_iso: str, states: frozenset[str] | None = None
    ) -> list[str]:
        """Invoice ids eligible for a retention purge (read-only, for dry runs).

        Only TERMINAL invoices (recovered/churned/exhausted by default) whose
        ``updated_at`` predates ``before_iso`` - an open failure is still being
        worked and is never eligible.
        """
        eligible = sorted(states or TERMINAL_STATES)
        state_ph = ",".join("?" for _ in eligible)
        rows = self._conn.execute(
            f"SELECT invoice_id FROM failures WHERE tenant_id = ? "
            f"AND state IN ({state_ph}) AND updated_at < ?",
            (self.tenant_id, *eligible, before_iso),
        ).fetchall()
        return [r["invoice_id"] for r in rows]

    def purge_expired(
        self, *, before_iso: str, states: frozenset[str] | None = None
    ) -> dict:
        """Delete closed records whose last update predates ``before_iso``.

        Only TERMINAL invoices (recovered/churned/exhausted by default) are
        eligible: an open failure is still being worked and must not be purged.
        ``updated_at`` is the close instant for a terminal row, so it is the
        retention clock. Child ``messages``/``transitions``/``events`` rows go
        with their invoice. Returns the per-table count deleted.
        """
        deleted = self._delete_invoices(self.expired_invoice_ids(before_iso, states))
        # Early-paid markers follow their own clock: a marker older than the cutoff
        # is dropped unless its invoice is still open. An open invoice's late
        # payment_failed is exactly what the marker exists to suppress.
        open_states = sorted(set(ALLOWED_TRANSITIONS) - TERMINAL_STATES)
        open_ph = ",".join("?" for _ in open_states)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM early_paid WHERE tenant_id = ? AND seen_at < ? "
                f"AND invoice_id NOT IN (SELECT invoice_id FROM failures "
                f"WHERE tenant_id = ? AND state IN ({open_ph}))",
                (self.tenant_id, before_iso, self.tenant_id, *open_states),
            )
            self._conn.commit()
            deleted["early_paid"] += cur.rowcount
        return deleted

    def _delete_invoices(self, invoice_ids: list[str]) -> dict:
        """Delete the given invoices and their children, for this tenant only."""
        deleted = {
            "failures": 0, "messages": 0, "transitions": 0, "events": 0,
            "approvals": 0, "early_paid": 0,
        }
        if not invoice_ids:
            return deleted
        placeholders = ",".join("?" for _ in invoice_ids)
        with self._lock:
            for table in ("messages", "transitions", "events", "approvals", "early_paid"):
                cur = self._conn.execute(
                    f"DELETE FROM {_guard_table(table)} WHERE tenant_id = ? "
                    f"AND invoice_id IN ({placeholders})",
                    [self.tenant_id, *invoice_ids],
                )
                deleted[table] = cur.rowcount
            cur = self._conn.execute(
                f"DELETE FROM {_guard_table('failures')} WHERE tenant_id = ? "
                f"AND invoice_id IN ({placeholders})",
                [self.tenant_id, *invoice_ids],
            )
            deleted["failures"] = cur.rowcount
            self._conn.commit()
        return deleted


class Platform:
    """Operations that span tenants. Never reachable from a :class:`Store`.

    Built from a :class:`Database`. The resolvers return a tenant id and nothing
    else, because the caller has no principal yet: the caller must then pass its
    capability check (signed link) or signature check before anything is read.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def register_tenant(self, tenant_id: str, slug: str, name: str) -> None:
        """Create a tenant row in state ``active``. Refuses a duplicate id or slug."""
        _check_tenant_id(tenant_id)
        now = _now()
        with self._db.lock:
            self._db.conn.execute(
                "INSERT INTO tenants (id, slug, name, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?)",
                (tenant_id, slug, name, now, now),
            )
            self._db.conn.commit()

    def tenant_status(self, tenant_id: str) -> str | None:
        row = self._db.conn.execute(
            "SELECT status FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        return row["status"] if row else None

    def delete_tenant_config(self, tenant_id: str) -> int:
        """Drop a tenant's stored configuration row (offboarding). Returns rows deleted."""
        with self._db.lock:
            cur = self._db.conn.execute(
                "DELETE FROM tenant_config WHERE tenant_id = ?", (tenant_id,)
            )
            self._db.conn.commit()
            return cur.rowcount

    def tenants_with_pending_approvals(self) -> list[str]:
        """Every tenant with an open approval request, for the background sweep."""
        rows = self._db.conn.execute(
            "SELECT DISTINCT tenant_id FROM approvals WHERE status = 'pending' "
            "ORDER BY tenant_id"
        ).fetchall()
        return [r["tenant_id"] for r in rows]

    # -- tenants ----------------------------------------------------------

    def tenant_row(self, tenant_id: str) -> dict | None:
        row = self._db.conn.execute(
            "SELECT id, slug, name, status, status_reason, created_at, updated_at "
            "FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_tenants(self) -> list[dict]:
        rows = self._db.conn.execute(
            "SELECT id, slug, name, status, status_reason, created_at, updated_at "
            "FROM tenants ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_tenant_status(self, tenant_id: str, status: str, reason: str | None) -> bool:
        """Move a tenant to ``active``, ``suspended`` or ``offboarded``. False if unknown."""
        if status not in TENANT_STATUSES:
            raise ValueError(f"unknown tenant status {status!r}")
        with self._db.lock:
            cur = self._db.conn.execute(
                "UPDATE tenants SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                (status, reason, _now(), tenant_id),
            )
            self._db.conn.commit()
            return cur.rowcount == 1

    # -- tokens -----------------------------------------------------------

    def insert_token(
        self, *, token_id: str, tenant_id: str | None, role: str, secret_hash: str,
        label: str | None, created_by: str,
    ) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "INSERT INTO api_tokens (token_id, tenant_id, role, secret_hash, label, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_id, tenant_id, role, secret_hash, label, created_by, _now()),
            )
            self._db.conn.commit()

    def token_row(self, token_id: str) -> dict | None:
        row = self._db.conn.execute(
            "SELECT token_id, tenant_id, role, secret_hash, label, created_by, created_at, "
            "last_used_at, revoked_at, revoked_reason FROM api_tokens WHERE token_id = ?",
            (token_id,),
        ).fetchone()
        return dict(row) if row else None

    def touch_token(self, token_id: str) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE token_id = ?", (_now(), token_id)
            )
            self._db.conn.commit()

    def revoke_token(self, token_id: str, reason: str) -> bool:
        """Revoke one token. False if unknown or already revoked."""
        with self._db.lock:
            cur = self._db.conn.execute(
                "UPDATE api_tokens SET revoked_at = ?, revoked_reason = ? "
                "WHERE token_id = ? AND revoked_at IS NULL",
                (_now(), reason, token_id),
            )
            self._db.conn.commit()
            return cur.rowcount == 1

    def revoke_tenant_tokens(self, tenant_id: str, reason: str) -> int:
        with self._db.lock:
            cur = self._db.conn.execute(
                "UPDATE api_tokens SET revoked_at = ?, revoked_reason = ? "
                "WHERE tenant_id = ? AND revoked_at IS NULL",
                (_now(), reason, tenant_id),
            )
            self._db.conn.commit()
            return cur.rowcount

    def list_tokens(self, tenant_id: str | None = None) -> list[dict]:
        """Token metadata for one tenant (or, with None, the platform admins). Never the hash."""
        where, args = ("tenant_id IS NULL", ()) if tenant_id is None else ("tenant_id = ?", (tenant_id,))
        rows = self._db.conn.execute(
            "SELECT token_id, tenant_id, role, label, created_by, created_at, last_used_at, "
            f"revoked_at, revoked_reason FROM api_tokens WHERE {where} ORDER BY created_at, token_id",
            args,
        ).fetchall()
        return [dict(r) for r in rows]

    def has_platform_admin_token(self) -> bool:
        """True if any platform_admin token was ever minted (revoked or not): bootstrap is one-shot."""
        return self._db.conn.execute(
            "SELECT 1 FROM api_tokens WHERE role = 'platform_admin' LIMIT 1"
        ).fetchone() is not None

    def approval_tenant(self, approval_id: int) -> str | None:
        """The tenant that owns an approval row, or None. Nothing else is returned."""
        row = self._db.conn.execute(
            "SELECT tenant_id FROM approvals WHERE id = ?", (int(approval_id),)
        ).fetchone()
        return row["tenant_id"] if row else None

    def message_tenant(self, provider_message_id: str) -> str | None:
        """The tenant that sent a message with this provider id, or None."""
        if not provider_message_id:
            return None
        row = self._db.conn.execute(
            "SELECT tenant_id FROM messages WHERE provider_message_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (provider_message_id,),
        ).fetchone()
        return row["tenant_id"] if row else None


# ---------------------------------------------------------------------------
# Singleton seam
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_store: Store | None = None


def tenancy_mode() -> str:
    """``PAYPILOT_TENANCY``: ``single`` (default) or ``multi``. Anything else is an error."""
    mode = os.getenv("PAYPILOT_TENANCY", "single").strip().lower()
    if mode not in ("single", "multi"):
        raise ValueError(f"PAYPILOT_TENANCY must be 'single' or 'multi', not {mode!r}")
    return mode


def get_store() -> Store:
    """Return the process-wide :class:`Store` bound to the configured tenant.

    The configured tenant is ``PAYPILOT_TENANT`` (default ``default``). The store is
    built on first use and refuses (:class:`TenantMismatchError`) when the file is
    owned by a different tenant.

    The single seam the API and nodes go through, so tests monkeypatch this one
    function (mirroring ``app.nodes.get_llm`` / ``app.ingest.get_retriever``).
    In multi mode there is no ambient tenant, so this raises
    :class:`AmbientTenantError`.
    """
    global _store
    if tenancy_mode() == "multi":
        raise AmbientTenantError(
            "PAYPILOT_TENANCY=multi: there is no ambient store. Bind a Store to a tenant."
        )
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def tenant_gate(store) -> str:
    """``active``, ``blocked`` (suspended or going away) or ``unreadable`` for a store's tenant.

    A store double with no database has no tenant row and is ``active``. Any other failure to read
    the status is ``unreadable``: callers must not send on it, and must not WRITE a permanent
    outcome on it either, because the failure may be a lock that clears in a second.
    """
    try:
        status = Platform(store.database).tenant_status(store.tenant_id)
    except AttributeError:
        return "active"
    except Exception:  # noqa: BLE001
        return "unreadable"
    return "blocked" if status == "suspended" or status in GONE_STATUSES else "active"


_db_lock = threading.Lock()
_database: Database | None = None
_tenant_stores: dict[str, Store] = {}


def get_database() -> Database:
    """The process-wide database that multi mode serves every tenant from.

    Reuses the registered store's database when one exists (tests, and a single-mode process that
    is switched to multi), so there is one connection and one writer lock per file.
    """
    global _database
    with _db_lock:
        if _database is None:
            with _store_lock:
                reg = _store
            _database = reg.database if reg is not None else Database()
        return _database


def get_platform() -> Platform:
    return Platform(get_database())


def store_for(tenant_id: str) -> Store:
    """A tenant-bound store on the shared database, cached per tenant. Multi mode's only way in."""
    _check_tenant_id(tenant_id)
    with _db_lock:
        cached = _tenant_stores.get(tenant_id)
    if cached is not None:
        return cached
    store = Store(get_database(), tenant_id=tenant_id)
    with _db_lock:
        return _tenant_stores.setdefault(tenant_id, store)


def reset_database() -> None:
    """Forget the shared database and the per-tenant stores. Test helper."""
    global _database
    with _db_lock:
        _database = None
        _tenant_stores.clear()


def reset_store(store: Store | None = None) -> None:
    """Replace the singleton. Test helper; also used to reopen a moved DB."""
    global _store
    with _store_lock:
        _store = store
    reset_database()


__all__ = [
    "ALLOWED_TRANSITIONS",
    "AmbientTenantError",
    "DEFAULT_TENANT",
    "Database",
    "EARLY_PAID_EVENT_TYPE",
    "InvalidTransition",
    "LegacySchemaError",
    "MIGRATED_TABLES",
    "Platform",
    "STATE_CHURNED",
    "STATE_CLICKED",
    "STATE_EXHAUSTED",
    "STATE_FAILED",
    "STATE_MESSAGED",
    "STATE_RECOVERED",
    "Store",
    "TENANT_TABLES",
    "TERMINAL_STATES",
    "UnknownInvoice",
    "can_transition",
    "early_paid_key",
    "GONE_STATUSES",
    "TENANT_STATUSES",
    "get_database",
    "tenant_gate",
    "get_platform",
    "get_store",
    "reset_database",
    "reset_store",
    "store_for",
]
