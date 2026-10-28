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
RLS is the second, and it holds if a predicate is ever forgotten.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
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

    def __init__(self, native, tenant_id: str | None = None) -> None:
        self._native = native
        self.tenant_id = tenant_id
        self._in_tx = False
        self._dirty = False

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    def _begin(self) -> None:
        self._native.run("START TRANSACTION")
        self._in_tx = True
        self._dirty = False
        if self.tenant_id is not None:
            # true = transaction-local: gone at COMMIT/ROLLBACK, so a reused connection cannot carry
            # a previous tenant's value into the next transaction.
            self._native.run("SELECT set_config('paypilot.tenant_id', :t, true)", t=self.tenant_id)

    def _abort(self) -> None:
        try:
            self._native.run("ROLLBACK")
        except Exception:  # noqa: BLE001 - the connection may already be gone; the caller sees the first error
            pass
        self._in_tx = False
        self._dirty = False

    def execute(self, sql: str, params=()) -> PgCursor:
        if _BEGIN_IMMEDIATE_RE.match(sql):
            # SQLite's write lock before a read-merge-write. The equivalent here is a transaction
            # that holds an advisory lock until it ends, so two processes cannot interleave.
            if self._in_tx:
                self.commit()
            try:
                self._begin()
                self._native.run("SELECT pg_advisory_xact_lock(hashtext(:k))", k=_WRITE_LOCK_KEY)
            except Exception as exc:
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
        if not self._in_tx:
            return
        try:
            self._native.run("COMMIT")
        except Exception as exc:
            self._abort()
            raise _map_error(exc) from exc
        self._in_tx = False
        self._dirty = False

    def rollback(self) -> None:
        if self._in_tx:
            self._abort()

    def close(self) -> None:
        try:
            self.rollback()
        finally:
            self._native.close()


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


def _default_connect(role: str):
    return decision_audit.open_connection(_role_env(role))


class PgDatabase:
    """The Postgres counterpart of :class:`app.store.Database`.

    ``conn``/``lock`` are the PLATFORM connection (Platform and tenant-config reads use them).
    ``conn_for(tenant)`` / ``lock_for(tenant)`` give each tenant its own connection and lock, so a
    tenant session holds the tenant role and nothing wider. ``connect(role)`` is injectable for tests.
    """

    path = "postgres"

    def __init__(self, connect: Callable[[str], object] | None = None) -> None:
        self._connect = connect or _default_connect
        self.lock = threading.Lock()
        self.conn = PgConnection(self._connect("platform"), tenant_id=None)
        self._tenants: dict[str, tuple[PgConnection, threading.Lock]] = {}
        self._guard = threading.Lock()

    def conn_for(self, tenant_id: str) -> PgConnection:
        return self._tenant(tenant_id)[0]

    def lock_for(self, tenant_id: str) -> threading.Lock:
        return self._tenant(tenant_id)[1]

    def _tenant(self, tenant_id: str):
        with self._guard:
            entry = self._tenants.get(tenant_id)
            if entry is None:
                entry = (PgConnection(self._connect("tenant"), tenant_id=tenant_id), threading.Lock())
                self._tenants[tenant_id] = entry
            return entry

    def owner_tenant(self) -> str | None:
        row = self.conn.execute("SELECT value FROM tenancy_meta WHERE key = 'owner_tenant'").fetchone()
        return row["value"] if row else None

    def close(self) -> None:
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
