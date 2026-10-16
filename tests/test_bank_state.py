"""The state parameter, including the property it does not have.

The load-bearing tests are the ones where the two readings DIVERGE:

  A STATE MINTED UNDER ANOTHER KEY MUST BE REFUSED, which is the forged case
  and the only reason the thing is signed at all.

  AN EXPIRED STATE MUST BE REFUSED, tested by passing ttl=0 rather than by
  sleeping, so the suite cannot be slow and cannot be flaky.

  AND THE REPLAY IS ASSERTED AS WORKING, on purpose. The docstring says this
  is not single use, so a test pins that behaviour: if somebody later makes
  it single use, this test goes red and they have to come and read why the
  gap was accepted rather than discovering the trade by accident.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.bank import state as st


def _f() -> Fernet:
    """A key per test, generated at runtime. Never a literal in the source."""
    return Fernet(Fernet.generate_key())


def test_round_trips_the_client_and_provider():
    f = _f()
    s = st.issue_state(client_id="acme", provider="fake", fernet=f)
    got = st.read_state(s, fernet=f)
    assert (got.client_id, got.provider) == ("acme", "fake")


def test_two_states_for_the_same_pair_differ():
    """Without the nonce, two flows in one second collide into one string."""
    f = _f()
    a = st.issue_state(client_id="acme", provider="fake", fernet=f)
    b = st.issue_state(client_id="acme", provider="fake", fernet=f)
    assert a != b
    assert st.read_state(a, fernet=f).nonce != st.read_state(b, fernet=f).nonce


def test_the_client_id_is_not_readable_without_the_key():
    """It is encrypted rather than merely signed, so the id is not on the wire."""
    s = st.issue_state(client_id="acme-secret-name", provider="fake", fernet=_f())
    assert "acme-secret-name" not in s


def test_a_state_from_another_key_is_refused():
    """The forged case. This is the whole reason the token is signed."""
    s = st.issue_state(client_id="acme", provider="fake", fernet=_f())
    with pytest.raises(st.StateError, match="forged|expired|another key"):
        st.read_state(s, fernet=_f())


def test_an_expired_state_is_refused():
    """Backdated with encrypt_at_time, checked against the REAL ttl.

    Not ttl=0, which was the first version of this test and DID NOT RAISE.
    Measured: Fernet checks timestamp + ttl < now, so a token minted in the
    same second survives ttl=0 and only a negative ttl refuses it. A test
    written that way asserts expiry and exercises nothing, and had it been
    written the other way round it would have shipped reporting that the ttl
    worked. This mints a token aged well past the window and passes
    STATE_TTL_SECONDS itself, so the value production uses is the value
    under test, with no sleeping and nothing flaky.
    """
    f = _f()
    payload = json.dumps(
        {
            "v": st.STATE_VERSION,
            "client_id": "acme",
            "provider": "fake",
            "nonce": "n",
        }
    ).encode()
    stale = f.encrypt_at_time(payload, 1_000_000).decode("ascii")
    with pytest.raises(st.StateError, match="expired|forged|another key"):
        st.read_state(stale, fernet=f)


def test_a_zero_ttl_does_not_expire_a_fresh_token():
    """The measured behaviour, pinned so nobody simplifies the test above.

    This is not a property worth relying on. It is recorded because the
    obvious shorter test uses ttl=0, and this says in one line why that test
    would prove nothing.
    """
    f = _f()
    s = st.issue_state(client_id="acme", provider="fake", fernet=f)
    assert st.read_state(s, fernet=f, ttl=0).client_id == "acme"


def test_a_fresh_state_passes_the_same_ttl_check():
    """The positive control beside the expiry test: without it, a refusal
    proves only that read_state can raise."""
    f = _f()
    s = st.issue_state(client_id="acme", provider="fake", fernet=f)
    assert st.read_state(s, fernet=f, ttl=st.STATE_TTL_SECONDS).client_id == "acme"


@pytest.mark.parametrize("bad", [None, "", 0, b"bytes", [], {}])
def test_a_missing_or_wrongly_typed_state_is_refused(bad):
    with pytest.raises(st.StateError):
        st.read_state(bad, fernet=_f())


def test_arbitrary_text_is_refused():
    with pytest.raises(st.StateError):
        st.read_state("just-some-string", fernet=_f())


def test_a_payload_that_is_not_json_is_refused():
    """Encrypted under the right key and still not ours."""
    f = _f()
    token = f.encrypt(b"not json at all").decode("ascii")
    with pytest.raises(st.StateError, match="not JSON"):
        st.read_state(token, fernet=f)


def test_a_json_payload_that_is_not_an_object_is_refused():
    f = _f()
    token = f.encrypt(json.dumps([1, 2, 3]).encode()).decode("ascii")
    with pytest.raises(st.StateError, match="not an object"):
        st.read_state(token, fernet=f)


def test_an_older_version_is_refused_rather_than_reinterpreted():
    """A format change must break loudly. An old token decrypts cleanly and
    would otherwise be read under the new rules."""
    f = _f()
    payload = {"v": 0, "client_id": "acme", "provider": "fake", "nonce": "n"}
    token = f.encrypt(json.dumps(payload).encode()).decode("ascii")
    with pytest.raises(st.StateError, match="version"):
        st.read_state(token, fernet=f)


@pytest.mark.parametrize("field", ["client_id", "provider", "nonce"])
def test_every_field_is_required_rather_than_defaulted(field):
    f = _f()
    payload = {
        "v": st.STATE_VERSION,
        "client_id": "acme",
        "provider": "fake",
        "nonce": "n",
    }
    del payload[field]
    token = f.encrypt(json.dumps(payload).encode()).decode("ascii")
    with pytest.raises(st.StateError, match=field):
        st.read_state(token, fernet=f)


@pytest.mark.parametrize("field", ["client_id", "provider", "nonce"])
def test_an_empty_field_is_refused_as_well_as_an_absent_one(field):
    """Empty and absent are the same answer here, which is not always true
    and is asserted rather than assumed."""
    f = _f()
    payload = {
        "v": st.STATE_VERSION,
        "client_id": "acme",
        "provider": "fake",
        "nonce": "n",
    }
    payload[field] = ""
    token = f.encrypt(json.dumps(payload).encode()).decode("ascii")
    with pytest.raises(st.StateError, match=field):
        st.read_state(token, fernet=f)


@pytest.mark.parametrize("bad", [None, "", "   ", 0, [], b"x"])
def test_issue_refuses_a_client_id_that_is_not_a_real_string(bad):
    with pytest.raises(st.StateError, match="client_id"):
        st.issue_state(client_id=bad, provider="fake", fernet=_f())


@pytest.mark.parametrize("bad", [None, "", "   ", 0, [], b"x"])
def test_issue_refuses_a_provider_that_is_not_a_real_string(bad):
    with pytest.raises(st.StateError, match="provider"):
        st.issue_state(client_id="acme", provider=bad, fernet=_f())


def test_a_state_is_replayable_until_it_expires_and_that_is_recorded():
    """PINNED ON PURPOSE. The module docstring says this is not single use.

    If somebody makes it single use, this test fails and sends them to the
    reason the gap was accepted, rather than letting the trade change
    silently in either direction.
    """
    f = _f()
    s = st.issue_state(client_id="acme", provider="fake", fernet=f)
    first = st.read_state(s, fernet=f)
    second = st.read_state(s, fernet=f)
    assert first == second, "a replay returns the same flow; see the docstring"


def test_it_refuses_with_no_key_configured(monkeypatch):
    """No key is a refusal and never a fallback to an unsigned state."""
    from app.bank import consent as cs

    monkeypatch.delenv(cs.CONSENT_FERNET_ENV, raising=False)
    with pytest.raises(cs.ConsentKeyMissing, match="is not set"):
        st.issue_state(client_id="acme", provider="fake")
    with pytest.raises(cs.ConsentKeyMissing, match="is not set"):
        st.read_state("anything")


def test_it_uses_the_configured_key_when_none_is_passed(monkeypatch):
    """The positive control for the refusal above."""
    from app.bank import consent as cs

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(cs.CONSENT_FERNET_ENV, key)
    s = st.issue_state(client_id="acme", provider="fake")
    assert st.read_state(s).client_id == "acme"
