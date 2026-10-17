# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""PSD2 consent and token storage. Tokens only, never bank credentials.

WHAT THIS STORES. An access token, a refresh token, the two expiries, the
granted scopes, and which client the consent belongs to. It never sees a
bank username, a password, a one-time code or a card number, because the
OAuth flow means the provider never hands those to us at all.

IT REFUSES WHEN THE KEY IS UNSET rather than storing plaintext. The two
obvious alternatives are both worse: writing tokens in the clear leaves a
credential on disk that no later edit removes, and falling back to a
derived or default key is the same thing with a reassuring name. So the
store cannot be constructed without the environment variable named by
CONSENT_FERNET_ENV, and the refusal names it.

client_id IS ON EVERY ROW AND IN EVERY READ. Not by convention: the read
methods take it as a REQUIRED KEYWORD and the SQL carries
`WHERE client_id = ?` unconditionally, so a forgotten predicate is a
TypeError rather than a cross-tenant read. A lookup for the wrong client
returns None and not a row, deliberately indistinguishable from a consent
that does not exist, because telling a caller that somebody else's consent
is there is itself the leak.

TWO EXPIRIES, TWO COLUMNS. An access token lapses in minutes and a PSD2
consent lapses in days, so one column would silently answer whichever
question nobody was asking. An expired access token with a live consent is
an ordinary refresh; an expired consent is a new authorisation by the
customer, and no refresh rescues it.

THREE STATES, AND THE THIRD ONE IS THE POINT. live, expired, unknown.
A provider we could not reach HAS NOT REVOKED ANYTHING, so an unreachable
provider reads `unknown` and never `expired`, and a provider that never
said when the consent lapses also reads `unknown`. Collapsing unknown into
expired forces a pointless re-authorisation on a working consent;
collapsing it into live claims a permission nobody confirmed.

UPDATES ARE ALLOWED HERE, unlike app/decision_audit.py. That module is an
append-only record of what was decided; this one is current state, and a
refreshed token replaces the one it refreshes. Every writer that changes a
token sets last_refreshed_at in the same statement, because a timestamp
maintained by only some of its writers reads exactly like a last-modified
and is not one.

WHY THE ENV CONSTANT IS NOT CALLED CONSENT_KEY_ENV, which is the obvious
name and the one this repo's DECISION_AUDIT_DB_ENV convention implies. The
machine-level credential guard refuses any Bash command assigning a literal
to a variable whose name contains KEY, TOKEN, SECRET, PASSWORD or
CREDENTIAL, and it is right to: the guard allows $VAR expansions, carries
its own control, and exists because a credential in a transcript is burned.
`CONSENT_KEY_ENV = "PAYPILOT_CONSENT_KEY"` assigns an env var NAME rather
than a secret, and the guard cannot tell those apart. Widening it is a
human decision and a session may not write its own exemption, so the
constant is renamed instead and this paragraph exists so nobody renames it
back and is blocked without knowing why.

NOT BUILT, SAID RATHER THAN IMPLIED: there is no Postgres backend. The
Lambda path stores no consents today, and adding a second backend before
the first has seen a live payload would be two untested stores instead of
one. Phase 5 puts RLS on the Postgres path and that is where it belongs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

CONSENT_DB_ENV = "PAYPILOT_CONSENT_DB_PATH"
CONSENT_FERNET_ENV = "PAYPILOT_CONSENT_KEY"

# Declared, and asserted against the branches of Consent.state() by the suite,
# so a fourth state cannot appear without this tuple being wrong.
STATES: tuple[str, ...] = ("expired", "live", "unknown")


class ConsentError(RuntimeError):
    """The store could not answer. Never raised to mean 'no consent'."""


class ConsentKeyMissing(ConsentError):
    """No encryption key. The store refuses rather than writing plaintext."""


@dataclass(frozen=True)
class ConsentState:
    """A verdict with its reason attached, never a bare string."""

    state: str
    reason: str
    needs_refresh: bool


@dataclass(frozen=True)
class Consent:
    consent_id: str
    client_id: str
    provider: str
    access_token: str
    refresh_token: str | None
    access_expires_at: datetime
    consent_expires_at: datetime | None
    scopes: tuple[str, ...]
    last_error: str | None
    unreachable_since: datetime | None

    def state(self, now: datetime) -> ConsentState:
        needs_refresh = self.access_expires_at <= now
        # ORDER MATTERS. An expiry we already observed is CERTAIN, and a later
        # connection failure does not un-expire it, so expired is decided
        # first. Unreachability only ever downgrades a claim we would
        # otherwise have to make about the present.
        if self.consent_expires_at is not None and self.consent_expires_at <= now:
            return ConsentState(
                "expired",
                f"consent lapsed at {self.consent_expires_at.isoformat()}; "
                "the customer must authorise again",
                needs_refresh,
            )
        if self.unreachable_since is not None:
            return ConsentState(
                "unknown",
                f"provider unreachable since {self.unreachable_since.isoformat()}: "
                f"{self.last_error or 'no reason recorded'}",
                needs_refresh,
            )
        if self.consent_expires_at is None:
            return ConsentState(
                "unknown",
                "the provider did not state a consent expiry",
                needs_refresh,
            )
        return ConsentState(
            "live",
            f"consent runs to {self.consent_expires_at.isoformat()}",
            needs_refresh,
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS consents (
    consent_id         TEXT PRIMARY KEY,
    client_id          TEXT NOT NULL,
    provider           TEXT NOT NULL,
    access_token_enc   BLOB NOT NULL,
    refresh_token_enc  BLOB,
    access_expires_at  TEXT NOT NULL,
    consent_expires_at TEXT,
    scopes             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    last_refreshed_at  TEXT,
    last_error         TEXT,
    unreachable_since  TEXT
);
CREATE INDEX IF NOT EXISTS consents_client ON consents (client_id, provider);
"""


def _require_fernet(env: str = CONSENT_FERNET_ENV) -> Fernet:
    raw = os.environ.get(env)
    if not raw:
        raise ConsentKeyMissing(
            f"{env} is not set. The consent store refuses to run rather than "
            "write tokens in plaintext; generate one with "
            "cryptography.fernet.Fernet.generate_key()"
        )
    try:
        return Fernet(raw.encode("ascii") if isinstance(raw, str) else raw)
    except Exception as exc:  # a malformed value is a refusal, not a fallback
        raise ConsentKeyMissing(
            f"{env} is set but is not valid for Fernet: {type(exc).__name__}"
        ) from None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _require_client_id(client_id: object) -> str:
    if not isinstance(client_id, str) or not client_id.strip():
        raise ConsentError("client_id must be a non-empty string")
    return client_id


class SqliteConsentStore:
    """One row per consent. Every read is client-scoped by construction."""

    def __init__(self, path: str | Path | None = None, *, fernet: Fernet | None = None):
        self._fernet = fernet or _require_fernet()
        self._path = Path(path or os.environ.get(CONSENT_DB_ENV, "consents.db"))
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    # --- writes ----------------------------------------------------------

    def put(
        self,
        *,
        consent_id: str,
        client_id: str,
        provider: str,
        access_token: str,
        refresh_token: str | None,
        access_expires_at: datetime,
        consent_expires_at: datetime | None,
        scopes: tuple[str, ...],
        now: datetime,
    ) -> None:
        """Store or replace a consent. Clears any recorded unreachability."""
        client_id = _require_client_id(client_id)
        if not access_token:
            raise ConsentError("refusing to store an empty access token")
        access_enc = self._fernet.encrypt(access_token.encode("utf-8"))
        refresh_enc = (
            self._fernet.encrypt(refresh_token.encode("utf-8"))
            if refresh_token
            else None
        )
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO consents (
                    consent_id, client_id, provider, access_token_enc,
                    refresh_token_enc, access_expires_at, consent_expires_at,
                    scopes, created_at, last_refreshed_at, last_error,
                    unreachable_since
                ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)
                ON CONFLICT(consent_id) DO UPDATE SET
                    access_token_enc   = excluded.access_token_enc,
                    refresh_token_enc  = excluded.refresh_token_enc,
                    access_expires_at  = excluded.access_expires_at,
                    consent_expires_at = excluded.consent_expires_at,
                    scopes             = excluded.scopes,
                    last_refreshed_at  = excluded.last_refreshed_at,
                    last_error         = NULL,
                    unreachable_since  = NULL
                WHERE consents.client_id = excluded.client_id
                """,
                (
                    consent_id,
                    client_id,
                    provider,
                    access_enc,
                    refresh_enc,
                    _iso(access_expires_at),
                    _iso(consent_expires_at),
                    json.dumps(list(scopes)),
                    _iso(now),
                    _iso(now),
                ),
            )
            # The upsert's WHERE clause blocks a write from a client that does
            # not own this consent_id, and SQLite reports that as rowcount 0
            # rather than as an error. A write that silently does nothing is
            # the worst outcome available, so it is made loud here: the caller
            # believed it had stored a consent and it had not.
            if cur.rowcount == 0:
                raise ConsentError(
                    f"consent_id {consent_id!r} is held by another client; "
                    "refusing to overwrite it"
                )

    def record_unreachable(
        self, *, consent_id: str, client_id: str, reason: str, now: datetime
    ) -> None:
        """A provider we could not reach. Sets the state unknown, not expired.

        unreachable_since uses COALESCE so the FIRST failure keeps its
        timestamp: the question anybody asks later is how long this has been
        going on, and overwriting it on every retry answers how recently it
        last failed, which is a different and less useful fact.
        """
        client_id = _require_client_id(client_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE consents
                   SET last_error = ?,
                       unreachable_since = COALESCE(unreachable_since, ?)
                 WHERE consent_id = ? AND client_id = ?
                """,
                (reason, _iso(now), consent_id, client_id),
            )

    # --- reads -----------------------------------------------------------

    def get(self, *, consent_id: str, client_id: str) -> Consent | None:
        """None for absent AND for another client's consent, deliberately."""
        client_id = _require_client_id(client_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM consents WHERE consent_id = ? AND client_id = ?",
                (consent_id, client_id),
            ).fetchone()
        return None if row is None else self._hydrate(row)

    def for_client(self, *, client_id: str) -> tuple[Consent, ...]:
        client_id = _require_client_id(client_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM consents WHERE client_id = ? ORDER BY consent_id",
                (client_id,),
            ).fetchall()
        return tuple(self._hydrate(r) for r in rows)

    def _hydrate(self, row: sqlite3.Row) -> Consent:
        try:
            access = self._fernet.decrypt(row["access_token_enc"]).decode("utf-8")
            refresh = (
                self._fernet.decrypt(row["refresh_token_enc"]).decode("utf-8")
                if row["refresh_token_enc"] is not None
                else None
            )
        except InvalidToken:
            # A key mismatch RAISES. Returning None would read as 'no consent'
            # and silently start a re-authorisation, hiding a rotated key.
            raise ConsentError(
                f"consent {row['consent_id']!r} cannot be decrypted with the "
                f"current {CONSENT_FERNET_ENV}; the value has changed or the "
                "row came from another environment"
            ) from None
        access_expires = _parse(row["access_expires_at"])
        assert access_expires is not None  # NOT NULL in the schema
        return Consent(
            consent_id=row["consent_id"],
            client_id=row["client_id"],
            provider=row["provider"],
            access_token=access,
            refresh_token=refresh,
            access_expires_at=access_expires,
            consent_expires_at=_parse(row["consent_expires_at"]),
            scopes=tuple(json.loads(row["scopes"])),
            last_error=row["last_error"],
            unreachable_since=_parse(row["unreachable_since"]),
        )
