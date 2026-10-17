"""The three bank handlers, then the same three over HTTP.

The handler tests are where the branches get exercised, because a handler is
a pure function and needs no server. The HTTP tests exist for the things only
the wiring can be wrong about: WHICH route authenticates WITH WHAT, the status
codes as a caller sees them, and whether the routes appear in the published
OpenAPI document. A route hidden from the schema does not exist commercially,
so that is an assertion rather than a note.

WHAT IS ASSERTED ON PURPOSE AND WOULD LOOK LIKE A BUG TO A SKIM:

  THE CALLBACK TAKES NO BEARER TOKEN. The browser arrives from the bank. The
  state is the credential on that hop, and a test pins that the route does
  not demand a token, because adding one would break every real callback.

  AN UNATTRIBUTED EVENT IS 202 AND NOT 200. Nothing maps an account to a
  client. 202 plus a reason is the loud version; 200 would be the silent
  drop, and the status code is the only place a caller can see which.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import api as api_module
from app.bank import consent as cs
from app.bank import fake
from app.bank import routes as br
from app.bank import state as bank_state

FIXED = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
TOKEN = "test-decision-token-value"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A key and a database per test. Keys are generated, never literals."""
    monkeypatch.setenv(cs.CONSENT_FERNET_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv(cs.CONSENT_DB_ENV, str(tmp_path / "consents.db"))
    monkeypatch.setenv(fake.ENABLE_ENV, "1")
    monkeypatch.setenv("DECISION_API_TOKEN", json.dumps({TOKEN: "acme"}))
    # api.py caches both stores in module globals, so a test that does not
    # clear them inherits the previous test's database.
    monkeypatch.setattr(api_module, "_consent_store", None, raising=False)
    yield
    monkeypatch.setattr(api_module, "_consent_store", None, raising=False)


@pytest.fixture
def store(env):
    return cs.SqliteConsentStore()


def _signed(body: bytes) -> dict:
    return {fake.SIGNATURE_HEADER: fake.sign_body(body)}


# --- start ------------------------------------------------------------------


def test_start_returns_the_providers_own_authorize_url(env):
    """The state is compared AFTER parsing the query, never as a substring.

    A Fernet token ends in padding, which percent-encodes in a query string,
    so `state in url` is false on a correctly built URL. Resolve the value
    from the system rather than matching the literal you expected.
    """
    status, payload = br.handle_consent_start(
        provider="fake", client_id="acme", redirect_uri="https://app.invalid/cb"
    )
    assert status == 200
    url = payload["authorize_url"]
    assert url.startswith(fake.FakeBank.ENDPOINTS["authorize"])
    q = parse_qs(urlparse(url).query)
    assert q["state"] == [payload["state"]]
    assert q["redirect_uri"] == ["https://app.invalid/cb"]
    assert q["response_type"] == ["code"]


def test_start_binds_the_state_to_the_token_holder_and_not_a_parameter(env):
    """The client id comes from the bearer token. A caller cannot name itself."""
    _, payload = br.handle_consent_start(
        provider="fake", client_id="acme", redirect_uri="https://app.invalid/cb"
    )
    assert bank_state.read_state(payload["state"]).client_id == "acme"


def test_start_refuses_with_no_client_id(env):
    status, payload = br.handle_consent_start(
        provider="fake", client_id=None, redirect_uri="https://app.invalid/cb"
    )
    assert status == 401 and payload["error"] == "unauthorised"


@pytest.mark.parametrize("bad", [None, "", "   ", 0, []])
def test_start_refuses_a_missing_redirect_uri(env, bad):
    status, _ = br.handle_consent_start(
        provider="fake", client_id="acme", redirect_uri=bad
    )
    assert status == 400


def test_start_on_an_unknown_provider_is_404(env):
    status, payload = br.handle_consent_start(
        provider="no-such-bank", client_id="acme", redirect_uri="https://app.invalid/cb"
    )
    assert status == 404 and payload["error"] == "unknown_provider"


def test_an_unavailable_provider_is_503_and_not_404(env, monkeypatch):
    """Different answers for 'no such provider' and 'exists, not available
    here'. A caller can act on the difference and a 404 would send them
    hunting for a name that is right."""
    monkeypatch.delenv(fake.ENABLE_ENV, raising=False)
    status, payload = br.handle_consent_start(
        provider="fake", client_id="acme", redirect_uri="https://app.invalid/cb"
    )
    assert status == 503 and payload["error"] == "provider_unavailable"


def test_start_with_no_consent_key_is_503_rather_than_an_unsigned_state(
    env, monkeypatch
):
    monkeypatch.delenv(cs.CONSENT_FERNET_ENV, raising=False)
    status, payload = br.handle_consent_start(
        provider="fake", client_id="acme", redirect_uri="https://app.invalid/cb"
    )
    assert status == 503 and payload["error"] == "not_configured"


# --- callback ---------------------------------------------------------------


def _start_then(store, *, client_id="acme"):
    _, payload = br.handle_consent_start(
        provider="fake", client_id=client_id, redirect_uri="https://app.invalid/cb"
    )
    return payload["state"]


def test_the_callback_files_a_live_consent(store):
    state = _start_then(store)
    status, payload = br.handle_consent_callback(
        state=state, code="auth-code-1", store=store, now=FIXED
    )
    assert status == 200
    assert payload["state"] == "live"
    assert payload["needs_refresh"] is False


def test_the_consent_lands_under_the_client_from_the_state(store):
    state = _start_then(store, client_id="acme")
    _, payload = br.handle_consent_callback(
        state=state, code="auth-code-1", store=store, now=FIXED
    )
    assert store.get(consent_id=payload["consent_id"], client_id="acme") is not None
    assert store.get(consent_id=payload["consent_id"], client_id="other") is None


def test_the_callback_returns_no_token_and_no_expiry(store):
    """The caller started the flow and needs to know it worked, not what we
    are holding. A token in a response body is a token in a log."""
    state = _start_then(store)
    _, payload = br.handle_consent_callback(
        state=state, code="auth-code-1", store=store, now=FIXED
    )
    blob = json.dumps(payload)
    assert "fake-access-" not in blob
    assert "fake-refresh-" not in blob
    assert "access_expires_at" not in payload


@pytest.mark.parametrize("bad", ["", "nonsense", None, 0])
def test_a_bad_state_is_refused_before_the_code_is_touched(store, bad):
    status, payload = br.handle_consent_callback(
        state=bad, code="auth-code-1", store=store, now=FIXED
    )
    assert status == 400 and payload["error"] == "invalid_state"


def test_a_state_minted_under_another_key_is_refused(store):
    """The forged case, end to end through the handler."""
    other = bank_state.issue_state(
        client_id="attacker", provider="fake", fernet=Fernet(Fernet.generate_key())
    )
    status, _ = br.handle_consent_callback(
        state=other, code="auth-code-1", store=store, now=FIXED
    )
    assert status == 400


def test_the_refusal_does_not_say_which_kind_of_bad_state_it_was(store):
    """Expired, forged and malformed get one answer. Telling them apart tells
    whoever sent it which one it was."""
    answers = set()
    for bad in ("", "nonsense", bank_state.issue_state(
        client_id="x", provider="fake", fernet=Fernet(Fernet.generate_key())
    )):
        _, payload = br.handle_consent_callback(
            state=bad, code="c", store=store, now=FIXED
        )
        answers.add(payload["detail"])
    assert len(answers) == 1, f"the refusal leaks the reason: {answers}"


@pytest.mark.parametrize("bad", ["", "   ", None, 0])
def test_a_missing_code_is_refused(store, bad):
    state = _start_then(store)
    status, _ = br.handle_consent_callback(
        state=state, code=bad, store=store, now=FIXED
    )
    assert status == 400


def test_a_write_that_does_not_read_back_is_a_500(store, monkeypatch):
    """Assert the effect, never the operation. put() reporting success and
    get() finding nothing must not return 200."""
    state = _start_then(store)
    monkeypatch.setattr(store, "get", lambda **kw: None)
    status, payload = br.handle_consent_callback(
        state=state, code="auth-code-1", store=store, now=FIXED
    )
    assert status == 500 and payload["error"] == "not_stored"


def test_a_naive_now_is_refused_rather_than_assumed_utc(store):
    state = _start_then(store)
    with pytest.raises(cs.ConsentError, match="aware"):
        br.handle_consent_callback(
            state=state,
            code="auth-code-1",
            store=store,
            now=datetime(2026, 3, 4, 12, 0),
        )


# --- webhook ----------------------------------------------------------------


def test_a_verified_return_event_is_202_unattributed_with_its_decision(env):
    body = b"rail=sepa;code=AC04;amount_minor=4500;currency=EUR;account=FAKE-ACC-1"
    status, payload = br.handle_bank_webhook(
        provider="fake", body=body, headers=_signed(body), now=FIXED
    )
    assert status == 202, "200 here would be the silent drop"
    assert payload["reason"] == "unattributed"
    assert payload["decision"]["matched"] is True
    assert payload["decision"]["goes_again"]


def test_an_unverified_body_is_401_and_is_never_parsed(env, monkeypatch):
    """Verify, THEN parse. Parsing first means an unverified body has
    already reached the code that reads it."""
    seen = []
    monkeypatch.setattr(
        fake.FakeBank,
        "parse_return_event",
        lambda self, **kw: seen.append(kw) or None,
    )
    body = b"rail=sepa;code=AC04"
    status, _ = br.handle_bank_webhook(provider="fake", body=body, headers={})
    assert status == 401
    assert seen == [], "the body was parsed before the signature verified"


def test_a_feed_entry_with_no_return_code_is_200_and_says_so(env):
    body = b"type=balance;amount_minor=100"
    status, payload = br.handle_bank_webhook(
        provider="fake", body=body, headers=_signed(body)
    )
    assert status == 200
    assert payload["handled"] is False
    assert payload["reason"] == "no_return_code"


def test_a_rail_needing_context_abstains_rather_than_guessing(env):
    """Bacs: the same symbol means different things in different reports, so
    an abstention is the answer and a decision would be a guess."""
    body = b"rail=bacs;code=0;amount_minor=100;account=FAKE-ACC-1"
    status, payload = br.handle_bank_webhook(
        provider="fake", body=body, headers=_signed(body)
    )
    assert status == 202
    assert payload["decision"]["abstains"] is True
    assert "report" in payload["decision"]["abstain_reason"]


def test_an_unknown_rail_abstains_and_names_what_it_does_know(env):
    """It abstains rather than erroring, and the reason names the rail it was
    given AND a rail it knows, so the caller can see it was a lookup miss."""
    body = b"rail=notarail;code=X1;amount_minor=100"
    status, payload = br.handle_bank_webhook(
        provider="fake", body=body, headers=_signed(body)
    )
    assert status == 202
    d = payload["decision"]
    assert d["abstains"] is True
    assert "notarail" in d["abstain_reason"]
    assert "ach" in d["abstain_reason"], "it does not say which rails it knows"


def test_the_two_abstentions_do_not_give_the_same_reason(env):
    """THE DISCRIMINATING TEST. An unknown rail and a bacs code both abstain
    with the same two keys, so every per-case assertion above passes under a
    mutation that collapses them into one generic abstention. Only comparing
    the reasons catches that: a missing rail is a lookup miss and a missing
    Bacs report is a refusal to guess, and a caller acts differently on each.
    """
    reasons = {}
    for name, body in (
        ("unknown", b"rail=notarail;code=X1;amount_minor=100"),
        ("bacs", b"rail=bacs;code=0;amount_minor=100;account=FAKE-ACC-1"),
    ):
        _, payload = br.handle_bank_webhook(
            provider="fake", body=body, headers=_signed(body)
        )
        reasons[name] = payload["decision"]["abstain_reason"]
    assert reasons["unknown"] != reasons["bacs"]
    assert "report" in reasons["bacs"]
    assert "report" not in reasons["unknown"]


def test_an_unmapped_code_carries_its_fallback_reason(env):
    body = b"rail=ach;code=R99;amount_minor=100"
    status, payload = br.handle_bank_webhook(
        provider="fake", body=body, headers=_signed(body)
    )
    assert status == 202
    assert payload["decision"]["matched"] is False
    assert payload["decision"]["fallback_reason"]


def test_an_unknown_provider_does_not_confirm_which_providers_exist(env):
    status, payload = br.handle_bank_webhook(
        provider="no-such-bank", body=b"x", headers={}
    )
    assert status == 404
    assert payload["detail"] == "no such provider"
    assert "no-such-bank" not in json.dumps(payload)


# --- the same three over HTTP ----------------------------------------------


def test_all_three_routes_are_in_the_published_schema(env):
    """A route hidden from the schema does not exist commercially."""
    spec = api_module.app.openapi()
    for path in (
        "/bank/consent/start",
        "/bank/consent/callback",
        "/webhooks/bank/{provider}",
    ):
        assert path in spec["paths"], f"{path} is not published"


def test_start_over_http_needs_a_bearer_token(env):
    client = TestClient(api_module.app)
    r = client.get(
        "/bank/consent/start",
        params={"provider": "fake", "redirect_uri": "https://app.invalid/cb"},
    )
    assert r.status_code == 401


def test_start_over_http_with_a_token_returns_a_url(env):
    client = TestClient(api_module.app)
    r = client.get(
        "/bank/consent/start",
        params={"provider": "fake", "redirect_uri": "https://app.invalid/cb"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["authorize_url"].startswith("https://bank.invalid/")


def test_the_callback_over_http_takes_no_bearer_token(env):
    """PINNED ON PURPOSE. The browser arrives from the bank with no token, so
    demanding one would break every real callback. The state is the
    credential, and a bad state is a 400 rather than a 401."""
    client = TestClient(api_module.app)
    start = client.get(
        "/bank/consent/start",
        params={"provider": "fake", "redirect_uri": "https://app.invalid/cb"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    state = start.json()["state"]
    r = client.get(
        "/bank/consent/callback", params={"state": state, "code": "auth-code-1"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "live"


def test_the_callback_over_http_refuses_a_bad_state_with_400(env):
    client = TestClient(api_module.app)
    r = client.get(
        "/bank/consent/callback", params={"state": "nonsense", "code": "c"}
    )
    assert r.status_code == 400


def test_the_webhook_over_http_verifies_the_signature(env):
    client = TestClient(api_module.app)
    body = b"rail=sepa;code=AC04;amount_minor=4500;account=FAKE-ACC-1"
    good = client.post("/webhooks/bank/fake", content=body, headers=_signed(body))
    assert good.status_code == 202, good.text
    bad = client.post("/webhooks/bank/fake", content=body, headers={})
    assert bad.status_code == 401


def test_the_webhook_over_http_refuses_an_oversized_body(env):
    client = TestClient(api_module.app)
    big = b"x" * (api_module.DECISION_MAX_BODY_BYTES + 1)
    r = client.post("/webhooks/bank/fake", content=big, headers=_signed(big))
    assert r.status_code == 413


def test_the_whole_flow_runs_offline_with_no_account(env):
    """Start, callback, webhook, decision. No network, no credentials, no
    aggregator contract. This is what the fake exists for."""
    client = TestClient(api_module.app)
    start = client.get(
        "/bank/consent/start",
        params={"provider": "fake", "redirect_uri": "https://app.invalid/cb"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert start.status_code == 200
    cb = client.get(
        "/bank/consent/callback",
        params={"state": start.json()["state"], "code": "auth-code-1"},
    )
    assert cb.status_code == 200 and cb.json()["state"] == "live"
    body = b"rail=sepa;code=AC04;amount_minor=4500;account=FAKE-ACC-1"
    hook = client.post("/webhooks/bank/fake", content=body, headers=_signed(body))
    assert hook.status_code == 202
    assert hook.json()["decision"]["what_has_to_change_first"]
