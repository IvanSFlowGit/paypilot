# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""PostgreSQL behind :class:`app.store.Store`, with row-level security doing the isolating.

STATUS: WRITTEN, NOT RUN AGAINST A REAL POSTGRES. It is off unless ``PAYPILOT_DB_BACKEND=postgres``,
so the SQLite path and the whole offline suite are untouched. The translation layer is unit tested
against a fake driver (tests/test_pg_backend.py). ``scripts/pg_rls_check.py`` is the real proof: it
applies ``app/pg_schema.sql`` to a throwaway cluster and tries to read across tenants as the tenant
role. Until that has passed on a machine that can start Postgres, isolation on this backend is a claim.

UNVERIFIED ASSUMPTIONS ABOUT pg8000.native, all in this one file so a wrong one is one place to fix:
``conn.run(sql, **named)`` returns a list of rows (or None), ``:name`` placeholders, ``conn.columns``
is a list of dicts with a ``name`` key, ``conn.row_count`` is the affected-row count, and a server error
is an exception whose first argument is a dict carrying the SQLSTATE under ``"C"``. The adapter manages
its own transactions with ``START TRANSACTION`` / ``COMMIT`` / ``ROLLBACK`` and so does not depend on
the driver's autocommit default.

HOW ISOLATION WORKS HERE. Each tenant gets its own connection, opened as ``paypilot_tenant`` (a role
with no BYPASSRLS and no access to platform tables). The first statement of every transaction is
``set_config('paypilot.tenant_id', <tenant>, true)``, transaction-local, so it cannot leak to the next
transaction on a pooled or reused connection. The policy in ``pg_schema.sql`` compares ``tenant_id``
with that setting. The application's own ``tenant_id = ?`` predicates stay: they are the first fence,
RLS is the second, and it holds if a predicate is ever forgotten. It does NOT hold against code that
runs arbitrary SQL as the tenant role: every tenant shares that role and may call set_config itself.
Per-tenant login roles with policies on current_user would be needed for that, and are not built.
Known limit: one server connection is kept per tenant ever seen and never idle-expired.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app import decision_audit

BACKEND_ENV = "PAYPILOT_DB_BACKEND"
SCHEMA_PATH = Path(__file__).with_name("pg_schema.sql")

#: Inserts whose new id the callers read (``cur.lastrowid``). Postgres has no lastrowid, so these get
#: ``RETURNING id``.
_RETURNING_TABLES = ("messages", "transitions", "approvals")
_RETURNING_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+(" + "|".join(_RETURNING_TABLES) + r")\b", re.IGNORECASE
)
_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_WRITE_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE)\b", re.IGNORECASE)
_BEGIN_IMMEDIATE_RE = re.compile(r"^\s*BEGIN\s+IMMEDIATE\s*;?\s*$", re.IGNORECASE)

#: SQLSTATEs that mean "try again", mapped to the exception the callers already treat as transient.
_TRANSIENT_STATES = frozenset({"40001", "40P01", "55P03", "57014"})
#: Class 23 is integrity-constraint violation (23505 is unique). SQLite reports all of them the same way.
_INTEGRITY_CLASS = "23"

#: Key for the advisory lock that stands in for SQLite's BEGIN IMMEDIATE on tenant_config merges.
_WRITE_LOCK_KEY = "paypilot.tenant_config"


def translate(sql: str, params: tuple | list | dict | None = None) -> tuple[str, dict]:
    """Turn one SQLite-dialect statement into Postgres named-parameter form.

    * ``?`` becomes ``:p0``, ``:p1``... outside single-quoted literals (a ``?`` in a literal is data).
    * ``INSERT OR IGNORE`` becomes ``INSERT ... ON CONFLICT DO NOTHING``.
    * Inserts into messages/transitions/approvals gain ``RETURNING id``.
    """
    out: list[str] = []
    values: dict = {}
    seq = list(params) if isinstance(params, (tuple, list)) else []
    n = 0
    in_literal = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if in_literal and sql[i : i + 2] == "''":
                out.append("''")
                i += 2
                continue
            in_literal = not in_literal
            out.append(ch)
        elif ch == "?" and not in_literal:
            if n >= len(seq):
                raise ValueError("more ? placeholders than parameters")
            values[f"p{n}"] = seq[n]
            out.append(f":p{n}")
            n += 1
        else:
            out.append(ch)
        i += 1
    if n != len(seq):
        raise ValueError("more parameters than ? placeholders")
    text = "".join(out).rstrip().rstrip(";")
    if _OR_IGNORE_RE.match(text):
        text = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", text, count=1, flags=re.I)
        text += " ON CONFLICT DO NOTHING"
    elif _RETURNING_RE.match(text) and "RETURNING" not in text.upper():
        text += " RETURNING id"
    return text, values


class PgRow(tuple):
    """A result row readable by position or by column name, like ``sqlite3.Row``."""

    _names: tuple[str, ...] = ()

    def __new__(cls, names: tuple[str, ...], values):
        row = super().__new__(cls, values)
        row._names = names
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return super().__getitem__(self._names.index(key))
            except ValueError:
                raise IndexError(f"No item with that key: {key}") from None
        return super().__getitem__(key)

    def keys(self) -> list[str]:
        return list(self._names)


class PgCursor:
    """What ``execute`` returns: rows already fetched, plus ``rowcount`` and ``lastrowid``."""

    def __init__(self, rows: list[PgRow], rowcount: int, lastrowid: int | None) -> None:
        self._rows = rows
        self._pos = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> PgRow | None:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[PgRow]:
        rest = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


def _sqlstate(exc: BaseException) -> str | None:
    arg = exc.args[0] if exc.args else None
    if isinstance(arg, dict):
        code = arg.get("C")
        return str(code) if code else None
    return getattr(exc, "sqlstate", None)


def _map_error(exc: BaseException) -> BaseException:
    state = _sqlstate(exc)
    if state and state.startswith(_INTEGRITY_CLASS):
        return sqlite3.IntegrityError(str(exc))
    if state in _TRANSIENT_STATES:
        return sqlite3.OperationalError(f"database is locked: {exc}")
    return exc


class PgConnection:
    """A sqlite3-connection-shaped wrapper over one pg8000.native connection, bound to one tenant.

    ``tenant_id=None`` is the platform connection: no setting is made and the role it connects as
    carries the cross-tenant policy. A tenant connection sets ``paypilot.tenant_id`` first thing in
    every transaction it opens.
    """

    #: How long a thread waits for another thread's open transaction on the SAME connection.
    LOCK_TIMEOUT = 30.0

    def __init__(
        self, native, tenant_id: str | None = None, reconnect=None, set_guc: bool = True
    ) -> None:
        self._native = native
        self.tenant_id = tenant_id
        self._set_guc = set_guc
        self._reconnect = reconnect
        self._in_tx = False
        self._dirty = False
        # One pg8000 connection is not thread-safe and FastAPI runs sync routes in a threadpool.
        # The lock is held for each call AND for the whole life of a transaction (taken in _begin,
        # released when the transaction ends), so a read on another thread can neither interleave
        # on the socket nor commit a half-written transaction. RLock: the owner thread re-enters.
        self._lock = threading.RLock()
        self.last_used = time.monotonic()

    def release_if_idle(self, max_idle: float) -> bool:
        """Close the server connection if nobody has used it for ``max_idle`` seconds and no
        transaction is open. The wrapper stays valid: the next call reconnects. Never blocks."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._in_tx or self._native is None:
                return False
            if time.monotonic() - self.last_used < max_idle:
                return False
            try:
                self._native.close()
            except Exception:  # noqa: BLE001 - already dead is as good as closed
                pass
            self._native = None
            return True
        finally:
            self._lock.release()

    def _acquire(self) -> None:
        if not self._lock.acquire(timeout=self.LOCK_TIMEOUT):
            raise sqlite3.OperationalError("database is locked: another transaction is still open")

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    def _ensure_native(self) -> None:
        if self._native is None:
            if self._reconnect is None:
                raise sqlite3.OperationalError("database connection is closed")
            self._native = self._reconnect()

    def _end_tx(self) -> None:
        """Mark the transaction over and release the hold taken in _begin (owner thread only)."""
        if self._in_tx:
            self._in_tx = False
            self._dirty = False
            self._lock.release()

    def _lost(self, exc: BaseException) -> None:
        """A connection-class failure (SQLSTATE 08xxx or a driver interface error): drop the dead
        connection so the next call reconnects instead of failing until the process restarts."""
        state = _sqlstate(exc)
        if (state and state.startswith("08")) or type(exc).__name__ == "InterfaceError":
            try:
                if self._native is not None:
                    self._native.close()
            except Exception:  # noqa: BLE001
                pass
            self._native = None

    def _begin(self) -> None:
        self._ensure_native()
        self._acquire()  # held until the transaction ends
        self._in_tx = True
        self._dirty = False
        try:
            self._native.run("START TRANSACTION")
        except BaseException:
            self._end_tx()
            raise
        if self.tenant_id is not None and self._set_guc:
            # true = transaction-local: gone at COMMIT/ROLLBACK, so a reused connection cannot carry
            # a previous tenant's value into the next transaction.
            self._native.run("SELECT set_config('paypilot.tenant_id', :t, true)", t=self.tenant_id)

    def _abort(self) -> None:
        try:
            if self._native is not None:
                self._native.run("ROLLBACK")
        except Exception as exc:  # noqa: BLE001 - the connection may already be gone
            self._lost(exc)
        self._end_tx()

    def execute(self, sql: str, params=()) -> PgCursor:
        self._acquire()
        try:
            return self._execute(sql, params)
        finally:
            self._lock.release()

    def _execute(self, sql: str, params=()) -> PgCursor:
        self.last_used = time.monotonic()
        if _BEGIN_IMMEDIATE_RE.match(sql):
            # SQLite's write lock before a read-merge-write. The equivalent here is a transaction
            # that holds an advisory lock until it ends, so two processes cannot interleave.
            if self._in_tx:
                self.commit()
            try:
                self._begin()
                # Bounded wait: any session can take a session-level advisory lock on a guessable key,
                # so an unbounded wait here would let one tenant stall the platform's config writes.
                self._native.run("SET LOCAL lock_timeout = '10s'")
                self._native.run(
                    "SELECT pg_advisory_xact_lock(hashtext(:k))",
                    k=f"{_WRITE_LOCK_KEY}:{self.tenant_id or 'platform'}",
                )
            except Exception as exc:
                self._lost(exc)
                self._abort()
                raise _map_error(exc) from exc
            self._dirty = True
            return PgCursor([], 0, None)
        text, values = translate(sql, params)
        is_write = bool(_WRITE_RE.match(text))
        try:
            if not self._in_tx:
                self._begin()
            raw = self._native.run(text, **values)
            columns = getattr(self._native, "columns", None) or []
            names = tuple(
                (c["name"].decode() if isinstance(c["name"], bytes) else c["name"])
                for c in columns
            )
            rows = [PgRow(names, r) for r in (raw or [])]
            count = getattr(self._native, "row_count", -1)
        except Exception as exc:
            self._lost(exc)
            self._abort()
            raise _map_error(exc) from exc
        lastrowid = None
        if rows and _RETURNING_RE.match(text) and "id" in names:
            lastrowid = int(rows[0]["id"])
        if is_write:
            self._dirty = True
        elif not self._dirty:
            # A bare read opened a transaction only to carry the setting. Close it now, or the
            # connection sits idle-in-transaction until the next write.
            self.commit()
        return PgCursor(rows, count, lastrowid)

    def commit(self) -> None:
        self._acquire()
        try:
            if not self._in_tx:
                return
            try:
                self._native.run("COMMIT")
            except Exception as exc:
                self._lost(exc)
                self._abort()
                raise _map_error(exc) from exc
            self._end_tx()
        finally:
            self._lock.release()

    def rollback(self) -> None:
        self._acquire()
        try:
            if self._in_tx:
                self._abort()
        finally:
            self._lock.release()

    def close(self) -> None:
        self._acquire()
        try:
            if self._in_tx:
                self._abort()
            if self._native is not None:
                self._native.close()
                self._native = None
        finally:
            self._lock.release()


def _role_env(role: str) -> dict:
    """libpq-style settings for one role: PGHOST/PGPORT/PGDATABASE/PGSSLROOTCERT shared, user per role."""
    env = {k: v for k, v in os.environ.items() if k.startswith("PG")}
    prefix = f"PAYPILOT_PG_{role.upper()}_"
    env["PGUSER"] = os.environ.get(prefix + "USER", "")
    env["PGPASSWORD"] = os.environ.get(prefix + "PASSWORD", "")
    missing = [k for k in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD") if not env.get(k)]
    if missing:
        raise RuntimeError(f"Postgres backend: {role} connection needs {', '.join(missing)}")
    return env


def _default_connect(role: str, tenant: str | None = None):
    """Shared-role connection, or (M9) the tenant's own login with its derived password."""
    env = _role_env(role) if tenant is None else _tenant_login_env(tenant)
    return decision_audit.open_connection(env)


def _tenant_login_env(tenant: str) -> dict:
    from app import pg_roles

    env = {k: v for k, v in os.environ.items() if k.startswith("PG")}
    env["PGUSER"] = pg_roles.role_name(tenant)
    env["PGPASSWORD"] = pg_roles.derive_password(tenant)
    missing = [k for k in ("PGHOST", "PGDATABASE") if not env.get(k)]
    if missing:
        raise RuntimeError(f"Postgres backend: tenant login needs {', '.join(missing)}")
    return env


class PgDatabase:
    """The Postgres counterpart of :class:`app.store.Database`.

    ``conn``/``lock`` are the PLATFORM connection (Platform and tenant-config reads use them).
    ``conn_for(tenant)`` / ``lock_for(tenant)`` give each tenant its own connection and lock, so a
    tenant session holds the tenant role and nothing wider. ``connect(role)`` is injectable for tests.
    """

    path = "postgres"

    def __init__(
        self, connect: Callable[..., object] | None = None, tenant_roles: bool | None = None
    ) -> None:
        from app import pg_roles

        self._connect = connect or _default_connect
        # M9: each tenant logs in as its own role and the database reads the tenant from that name.
        self.tenant_roles = pg_roles.roles_enabled() if tenant_roles is None else tenant_roles
        self.lock = threading.Lock()
        self.conn = PgConnection(
            self._connect("platform"), tenant_id=None, reconnect=lambda: self._connect("platform")
        )
        self._tenants: dict[str, tuple[PgConnection, threading.Lock]] = {}
        self._guard = threading.Lock()
        self._failed: dict[str, float] = {}
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        if connect is None:  # a real deployment; tests inject a connector and call reap() themselves
            self._reaper = threading.Thread(target=self._reap_loop, name="pg-idle-reaper", daemon=True)
            self._reaper.start()

    #: A tenant connection unused this long is closed; it reconnects on its next use.
    IDLE_SECONDS = 300.0
    REAP_EVERY = 60.0

    def reap(self, max_idle: float | None = None) -> int:
        """Close idle tenant (and platform) connections. Returns how many were closed."""
        limit = self.IDLE_SECONDS if max_idle is None else max_idle
        with self._guard:
            conns = [c for c, _l in self._tenants.values()]
        conns.append(self.conn)
        return sum(1 for c in conns if c.release_if_idle(limit))

    def _reap_loop(self) -> None:
        while not self._stop.wait(self.REAP_EVERY):
            try:
                self.reap()
            except Exception:  # noqa: BLE001 - housekeeping never takes the app down
                pass

    def conn_for(self, tenant_id: str) -> PgConnection:
        return self._tenant(tenant_id)[0]

    def lock_for(self, tenant_id: str) -> threading.Lock:
        return self._tenant(tenant_id)[1]

    #: After a failed tenant login (role not provisioned yet, wrong key), refuse quickly for this long
    #: rather than making every request open a network connection and stall behind the others.
    FAIL_COOLDOWN = 5.0

    def _tenant(self, tenant_id: str):
        with self._guard:
            entry = self._tenants.get(tenant_id)
            failed_at = self._failed.get(tenant_id)
        if entry is not None:
            return entry
        if failed_at is not None and time.monotonic() - failed_at < self.FAIL_COOLDOWN:
            raise RuntimeError("tenant database login unavailable; retry shortly")
        if self.tenant_roles:
            def make(t=tenant_id):
                return self._connect("tenant", t)
        else:
            def make():
                return self._connect("tenant")
        # Connect OUTSIDE the lock: a login can take seconds and must not stall other tenants.
        try:
            conn = PgConnection(make(), tenant_id=tenant_id, reconnect=make,
                                set_guc=not self.tenant_roles)
        except Exception:
            with self._guard:
                self._failed[tenant_id] = time.monotonic()
            raise
        with self._guard:
            self._failed.pop(tenant_id, None)
            existing = self._tenants.get(tenant_id)
            if existing is None:
                entry = (conn, threading.Lock())
                self._tenants[tenant_id] = entry
                return entry
        conn.close()  # another thread connected first; use theirs
        return existing

    def owner_tenant(self) -> str | None:
        row = self.conn.execute("SELECT value FROM tenancy_meta WHERE key = 'owner_tenant'").fetchone()
        return row["value"] if row else None

    def close(self) -> None:
        self._stop.set()
        with self.lock:
            self.conn.close()
        with self._guard:
            for conn, _lock in self._tenants.values():
                conn.close()
            self._tenants.clear()


def split_statements(sql: str) -> list[str]:
    """Split a schema file on ``;`` outside comments, single-quoted literals and dollar-quoted bodies.

    ``decision_audit.split_sql_statements`` knows single quotes only; this schema has a DO block and
    function bodies in ``$$ ... $$``, whose semicolons are not statement ends.
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None  # "'" or the dollar tag, e.g. "$$"
    while i < n:
        ch = sql[i]
        if quote is None:
            if sql.startswith("--", i):
                j = sql.find("\n", i)
                i = n if j == -1 else j
                continue
            if ch == "'":
                quote = "'"
            elif ch == "$":
                m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
                if m:
                    quote = m.group(0)
                    buf.append(quote)
                    i += len(quote)
                    continue
            elif ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    out.append(stmt)
                buf = []
                i += 1
                continue
        elif quote == "'":
            if ch == "'":
                if sql.startswith("''", i):
                    buf.append("''")
                    i += 2
                    continue
                quote = None
        elif sql.startswith(quote, i):
            buf.append(quote)
            i += len(quote)
            quote = None
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def apply_schema(native) -> int:
    """Run ``pg_schema.sql`` on an OWNER connection, statement by statement. Returns the statement count."""
    statements = split_statements(SCHEMA_PATH.read_text(encoding="utf-8"))
    for stmt in statements:
        try:
            native.run(stmt)
        except Exception as exc:
            head = " ".join(stmt.split())[:110]
            raise RuntimeError(f"schema statement failed: {head!r}: {exc}") from exc
    return len(statements)


def backend_is_postgres() -> bool:
    return os.getenv(BACKEND_ENV, "").strip().lower() == "postgres"
