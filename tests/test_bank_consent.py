"""The consent store: no plaintext, no cross-tenant read, three states.

The load-bearing test is test_the_token_never_appears_in_the_database_file.
Every other test here reads the store through its own API, which would pass
just as happily if the encrypt call were removed, so only reading the raw
bytes off disk can tell encryption from the appearance of it.

The second layer is test_every_read_in_the_module_carries_the_client_predicate,
which greps the module source. The behavioural tests prove the two reads
that exist today are scoped; that one catches the third read somebody adds
next month without the predicate.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.bank import consent as cs

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# A token-shaped string that is not a credential: it is generated nowhere and
# means nothing to any provider. The test that matters searches the database
# file for it, so it has to be long and unlikely enough that a hit is real.
FAKE_ACCESS = "access-zzqq-7731-not-a-real-token"
FAKE_REFRESH = "refresh-zzqq-8842-not-a-real-token"


@pytest.fixture
def key() -> Fernet:
    return Fernet(Fernet.generate_key())


@pytest.fixture
def store(tmp_path, key) -> cs.SqliteConsentStore:
    return cs.SqliteConsentStore(tmp_path / "consents.db", fernet=key)


def _put(store, **over):
    args = dict(
        consent_id="c1",
        client_id="client-a",
        provider="fake",
        access_token=FAKE_ACCESS,
        refresh_token=FAKE_REFRESH,
        access_expires_at=NOW + HOUR,
        consent_expires_at=NOW + 90 * DAY,
        scopes=("accounts", "transactions"),
        now=NOW,
    )
    args.update(over)
    store.put(**args)
    return args


# --------------------------------------------------------------------------
# At rest
# --------------------------------------------------------------------------

def test_a_consent_round_trips(store):
    _put(store)
    got = store.get(consent_id="c1", client_id="client-a")
    assert got is not None
    assert got.access_token == FAKE_ACCESS
    assert got.refresh_token == FAKE_REFRESH
    assert got.scopes == ("accounts", "transactions")
    assert got.access_expires_at == NOW + HOUR


def test_the_token_never_appears_in_the_database_file(tmp_path, key):
    """The only test here that can tell encryption from the look of it."""
    path = tmp_path / "consents.db"
    st = cs.SqliteConsentStore(path, fernet=key)
    _put(st)
    raw = pathlib.Path(path).read_bytes()
    assert FAKE_ACCESS.encode() not in raw
    assert FAKE_REFRESH.encode() not in raw
    # Positive control: something that IS stored in the clear is present, so a
    # pass above cannot be an empty or unwritten file.
    assert b"client-a" in raw


def test_an_absent_refresh_token_round_trips_as_none(store):
    _put(store, refresh_token=None)
    got = store.get(consent_id="c1", client_id="client-a")
    assert got is not None and got.refresh_token is None


def test_storing_the_same_consent_twice_updates_rather_than_duplicates(store):
    _put(store)
    _put(store, access_token="access-second-value-not-real", now=NOW + HOUR)
    rows = store.for_client(client_id="client-a")
    assert len(rows) == 1
    assert rows[0].access_token == "access-second-value-not-real"


# --------------------------------------------------------------------------
# No key, no store
# --------------------------------------------------------------------------

def test_an_unset_key_refuses_and_says_it_is_unset(tmp_path, monkeypatch):
    """Matched on the phrase UNIQUE to the unset branch, not on the variable.

    Both refusal branches name the variable, so matching on the name alone
    passed whether the unset check fired or the value fell through to the
    malformed branch. Mutating `if not raw:` to `if False:` passed all 32
    tests and that is how this was found. The two cases have different
    fixes, generate a key against repair the one you have, so an error that
    cannot tell them apart is an error that sends the reader to the wrong
    remedy.
    """
    monkeypatch.delenv(cs.CONSENT_FERNET_ENV, raising=False)
    with pytest.raises(cs.ConsentKeyMissing, match="is not set"):
        cs.SqliteConsentStore(tmp_path / "x.db")
    try:
        cs.SqliteConsentStore(tmp_path / "x.db")
    except cs.ConsentKeyMissing as exc:
        assert cs.CONSENT_FERNET_ENV in str(exc)
        assert "is set but" not in str(exc)


def test_a_malformed_key_refuses_rather_than_falling_back(tmp_path, monkeypatch):
    monkeypatch.setenv(cs.CONSENT_FERNET_ENV, "not-a-valid-fernet-value")
    with pytest.raises(cs.ConsentKeyMissing, match="is set but is not valid"):
        cs.SqliteConsentStore(tmp_path / "x.db")


def test_a_key_from_the_environment_is_used_when_present(tmp_path, monkeypatch):
    """Control. Without it, both refusals above could be a store that never works."""
    monkeypatch.setenv(cs.CONSENT_FERNET_ENV, Fernet.generate_key().decode())
    st = cs.SqliteConsentStore(tmp_path / "x.db")
    _put(st)
    assert st.get(consent_id="c1", client_id="client-a") is not None


def test_a_rotated_key_raises_rather_than_reporting_no_consent(tmp_path, key):
    path = tmp_path / "consents.db"
    _put(cs.SqliteConsentStore(path, fernet=key))
    other = cs.SqliteConsentStore(path, fernet=Fernet(Fernet.generate_key()))
    with pytest.raises(cs.ConsentError, match="cannot be decrypted"):
        other.get(consent_id="c1", client_id="client-a")


def test_the_decryption_refusal_does_not_carry_the_token(tmp_path, key):
    path = tmp_path / "consents.db"
    _put(cs.SqliteConsentStore(path, fernet=key))
    other = cs.SqliteConsentStore(path, fernet=Fernet(Fernet.generate_key()))
    try:
        other.get(consent_id="c1", client_id="client-a")
    except cs.ConsentError as exc:
        assert FAKE_ACCESS not in str(exc)
    else:  # pragma: no cover
        pytest.fail("expected a refusal")


# --------------------------------------------------------------------------
# client_id, on every row and in every read
# --------------------------------------------------------------------------

def test_another_clients_consent_is_not_returned(store):
    _put(store)
    assert store.get(consent_id="c1", client_id="client-b") is None


def test_a_wrong_client_and_an_absent_consent_are_indistinguishable(store):
    _put(store)
    wrong_client = store.get(consent_id="c1", client_id="client-b")
    absent = store.get(consent_id="c-nope", client_id="client-a")
    assert wrong_client is absent is None


def test_for_client_returns_only_that_clients_rows(store):
    _put(store)
    _put(store, consent_id="c2", client_id="client-b")
    assert [c.consent_id for c in store.for_client(client_id="client-a")] == ["c1"]
    assert [c.consent_id for c in store.for_client(client_id="client-b")] == ["c2"]


def test_a_read_without_a_client_id_is_a_type_error(store):
    with pytest.raises(TypeError):
        store.get(consent_id="c1")


@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_a_missing_or_non_string_client_id_is_refused(store, bad):
    with pytest.raises(cs.ConsentError, match="non-empty string"):
        store.for_client(client_id=bad)


def test_an_empty_access_token_is_refused(store):
    with pytest.raises(cs.ConsentError, match="empty access token"):
        _put(store, access_token="")


def test_every_read_in_the_module_carries_the_client_predicate():
    """Second layer. Catches the third read added without the predicate."""
    src = pathlib.Path(cs.__file__).read_text()
    selects = re.findall(r"SELECT \* FROM consents[^\"]*", src)
    assert selects, "no reads found; this check has stopped checking"
    for sql in selects:
        assert "client_id = ?" in sql, sql


# --------------------------------------------------------------------------
# The three states
# --------------------------------------------------------------------------

def _consent(**over) -> cs.Consent:
    args = dict(
        consent_id="c1",
        client_id="client-a",
        provider="fake",
        access_token=FAKE_ACCESS,
        refresh_token=None,
        access_expires_at=NOW + HOUR,
        consent_expires_at=NOW + 90 * DAY,
        scopes=(),
        last_error=None,
        unreachable_since=None,
    )
    args.update(over)
    return cs.Consent(**args)


def test_a_live_consent_with_a_live_token_needs_nothing():
    verdict = _consent().state(NOW)
    assert verdict.state == "live"
    assert verdict.needs_refresh is False


def test_an_expired_token_under_a_live_consent_is_a_refresh_and_not_a_lapse():
    """The whole reason the two expiries are separate columns."""
    verdict = _consent(access_expires_at=NOW - HOUR).state(NOW)
    assert verdict.state == "live"
    assert verdict.needs_refresh is True


def test_an_expired_consent_reads_expired():
    verdict = _consent(consent_expires_at=NOW - DAY).state(NOW)
    assert verdict.state == "expired"
    assert "authorise again" in verdict.reason


def test_a_provider_that_never_stated_an_expiry_reads_unknown():
    verdict = _consent(consent_expires_at=None).state(NOW)
    assert verdict.state == "unknown"
    assert "did not state" in verdict.reason


def test_an_unreachable_provider_reads_unknown_and_never_expired():
    """A provider we could not reach has not revoked anything.

    consent_expires_at is in the FUTURE here on purpose: that is the input
    where unknown and live diverge. With an already-lapsed expiry both
    readings would answer expired and the test would prove nothing.
    """
    verdict = _consent(
        consent_expires_at=NOW + 90 * DAY,
        unreachable_since=NOW - HOUR,
        last_error="connect timeout",
    ).state(NOW)
    assert verdict.state == "unknown"
    assert "connect timeout" in verdict.reason


def test_a_known_lapse_outranks_unreachability():
    """An expiry already observed is certain; a later outage does not undo it."""
    verdict = _consent(
        consent_expires_at=NOW - DAY,
        unreachable_since=NOW - HOUR,
        last_error="connect timeout",
    ).state(NOW)
    assert verdict.state == "expired"


def test_unreachable_with_no_recorded_reason_still_says_so():
    verdict = _consent(unreachable_since=NOW - HOUR, last_error=None).state(NOW)
    assert verdict.state == "unknown"
    assert "no reason recorded" in verdict.reason


def test_every_state_returned_is_one_of_the_declared_three():
    cases = [
        _consent(),
        _consent(consent_expires_at=NOW - DAY),
        _consent(consent_expires_at=None),
        _consent(unreachable_since=NOW - HOUR),
    ]
    seen = {c.state(NOW).state for c in cases}
    assert seen <= set(cs.STATES)
    assert seen == set(cs.STATES), f"a declared state is never produced: {seen}"


# --------------------------------------------------------------------------
# Recording unreachability
# --------------------------------------------------------------------------

def test_recording_unreachability_moves_the_state_to_unknown(store):
    _put(store)
    store.record_unreachable(
        consent_id="c1", client_id="client-a", reason="connect timeout", now=NOW
    )
    got = store.get(consent_id="c1", client_id="client-a")
    assert got is not None and got.state(NOW).state == "unknown"


def test_the_first_failure_keeps_its_timestamp(store):
    """How long has this been going on beats when did it last fail."""
    _put(store)
    store.record_unreachable(
        consent_id="c1", client_id="client-a", reason="first", now=NOW
    )
    store.record_unreachable(
        consent_id="c1", client_id="client-a", reason="second", now=NOW + 5 * HOUR
    )
    got = store.get(consent_id="c1", client_id="client-a")
    assert got is not None
    assert got.unreachable_since == NOW
    assert got.last_error == "second"


def test_a_successful_refresh_clears_the_unreachable_record(store):
    _put(store)
    store.record_unreachable(
        consent_id="c1", client_id="client-a", reason="timeout", now=NOW
    )
    _put(store, now=NOW + HOUR)
    got = store.get(consent_id="c1", client_id="client-a")
    assert got is not None
    assert got.unreachable_since is None and got.last_error is None
    assert got.state(NOW).state == "live"


def test_recording_unreachability_is_client_scoped(store):
    _put(store)
    store.record_unreachable(
        consent_id="c1", client_id="client-b", reason="timeout", now=NOW
    )
    got = store.get(consent_id="c1", client_id="client-a")
    assert got is not None and got.unreachable_since is None


def test_one_client_cannot_overwrite_another_clients_consent_id(store):
    """A blocked upsert is rowcount 0 in SQLite, which is not an error.

    A write that silently does nothing is the worst outcome available: the
    caller believes it stored a consent and did not, and the owner's row is
    intact so nothing downstream ever disagrees.
    """
    _put(store)
    with pytest.raises(cs.ConsentError, match="held by another client"):
        _put(store, client_id="client-b", access_token="access-thief-not-real")
    got = store.get(consent_id="c1", client_id="client-a")
    assert got is not None and got.access_token == FAKE_ACCESS
    assert store.for_client(client_id="client-b") == ()
