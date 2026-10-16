"""The stability wrapper, with the two cases that decide whether it is safe.

The two that earn this file:

test_a_timeout_on_a_non_retryable_call_is_unknown_rather_than_failed - because
a caller that reads "failed" re-issues, and re-issuing a single-use code burns
the consent.

test_it_alerts_on_the_second_consecutive_failure_and_not_the_first - with its
own negative half, because a wrapper that alerts on every blip trains the
reader to skim the message that mattered.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.bank import http as bank_http


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def state(tmp_path):
    return bank_http._FailureState.load(tmp_path / "failures.json")


@pytest.fixture()
def no_sleep():
    slept: list[float] = []
    return slept, slept.append


# --------------------------------------------------------------------------
# The happy path, so nothing below can pass by the wrapper being broken
# --------------------------------------------------------------------------

def test_a_200_is_returned_as_the_providers_own_response(state):
    with _client(lambda r: httpx.Response(200, json={"accounts": []})) as client:
        response = bank_http.request(
            "GET", "https://bank.example/accounts",
            provider="fake", retry=True, client=client, state=state,
        )
    assert response.status_code == 200
    assert response.json() == {"accounts": []}


# --------------------------------------------------------------------------
# retry is explicit, because there is no safe default for both call shapes
# --------------------------------------------------------------------------

def test_retry_has_no_default_and_must_be_a_bool():
    with pytest.raises(TypeError, match="no safe default"):
        bank_http.request("GET", "https://bank.example/x", provider="fake", retry=None)
    with pytest.raises(TypeError):
        bank_http.request("GET", "https://bank.example/x", provider="fake", retry=1)


def test_retry_is_a_required_keyword():
    """Forgetting it must not reach the dangerous case."""
    with pytest.raises(TypeError):
        bank_http.request("GET", "https://bank.example/x", provider="fake")


def test_a_retryable_call_tries_the_bounded_number_of_times_with_backoff(state, no_sleep):
    slept, sleeper = no_sleep
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            503,
            text=json.dumps({"detail": "upstream down"}),
            headers={"content-type": "application/json"},
        )

    with _client(handler) as client:
        with pytest.raises(bank_http.BankHTTPError) as exc_info:
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=True, client=client, state=state,
                sleep=sleeper, max_attempts=3, backoff_s=0.5,
            )
    assert exc_info.value.attempts == 3
    assert exc_info.value.status == 503
    assert "upstream down" in (exc_info.value.body or "")
    assert len(calls) == 3
    # Backoff grows rather than hammering: 0.5 then 1.0.
    assert slept == [0.5, 1.0]


def test_a_non_retryable_call_is_attempted_exactly_once(state, no_sleep):
    slept, sleeper = no_sleep
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            503,
            text=json.dumps({"detail": "upstream down"}),
            headers={"content-type": "application/json"},
        )

    with _client(handler) as client:
        with pytest.raises(bank_http.BankHTTPError) as exc_info:
            bank_http.request(
                "POST", "https://bank.example/token",
                provider="fake", retry=False, client=client, state=state,
                sleep=sleeper,
            )
    assert len(calls) == 1
    assert exc_info.value.attempts == 1
    assert slept == []


# --------------------------------------------------------------------------
# The timeout rule. Unknown is not failed.
# --------------------------------------------------------------------------

def test_a_timeout_on_a_non_retryable_call_is_unknown_rather_than_failed(state):
    """The request may have landed. A caller must read state back, not re-send."""
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with _client(handler) as client:
        with pytest.raises(bank_http.ProviderUnreachable) as exc_info:
            bank_http.request(
                "POST", "https://bank.example/token",
                provider="fake", retry=False, client=client, state=state,
            )
    assert exc_info.value.outcome_unknown is True


def test_a_timeout_on_a_retryable_read_is_a_known_failure(state, no_sleep):
    """A read that never answered changed nothing, so there is nothing unknown."""
    _, sleeper = no_sleep

    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with _client(handler) as client:
        with pytest.raises(bank_http.ProviderUnreachable) as exc_info:
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=True, client=client, state=state,
                sleep=sleeper,
            )
    assert exc_info.value.outcome_unknown is False


# --------------------------------------------------------------------------
# Quota is a refusal, never a retry
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", sorted(bank_http.QUOTA_STATUS))
def test_quota_and_rate_refuse_immediately_without_retrying(status, state, no_sleep):
    slept, sleeper = no_sleep
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            status,
            text=json.dumps({"error": "rate limited", "code": "429"}),
            headers={"content-type": "application/json"},
        )

    with _client(handler) as client:
        with pytest.raises(bank_http.QuotaExhausted) as exc_info:
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=True, client=client, state=state,
                sleep=sleeper,
            )
    assert len(calls) == 1, "a quota refusal must not be retried"
    assert slept == []
    assert exc_info.value.status == status
    assert "rate limited" in (exc_info.value.body or "")
    # The refusal names the provider's own words, so a reader can tell a
    # spent allowance from a per-second limit without opening the provider.


def test_429_is_not_in_the_retryable_set():
    """Stated as an assertion because the two sets are easy to merge later."""
    assert 429 not in bank_http.RETRYABLE_STATUS
    assert 429 in bank_http.QUOTA_STATUS
    assert not (bank_http.RETRYABLE_STATUS & bank_http.QUOTA_STATUS)


# --------------------------------------------------------------------------
# Status AND body, because a status code never names the fault
# --------------------------------------------------------------------------

def test_a_failure_carries_the_providers_own_body_not_only_its_status(state):
    body = json.dumps({"detail": "Required scope: accounts:read", "code": "IM001"})
    with _client(lambda r: httpx.Response(403, text=body)) as client:
        with pytest.raises(bank_http.BankHTTPError) as exc_info:
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=False, client=client, state=state,
            )
    assert exc_info.value.status == 403
    assert "Required scope: accounts:read" in (exc_info.value.body or "")


def test_a_non_json_body_contributes_its_shape_and_none_of_its_content(state, caplog):
    """The case that killed the first version of this wrapper.

    It ran a card-run scrubber over the body and the docstring claimed names
    and addresses were masked. This exact body put an email in the log.
    """
    long_body = "customer Ivan Skachek <ivan@example.com> not found " + ("x" * 2000)
    with caplog.at_level(logging.WARNING, logger="paypilot.bank.http"):
        with _client(
            lambda r: httpx.Response(
                404, text=long_body, headers={"content-type": "text/html"}
            )
        ) as client:
            with pytest.raises(bank_http.BankHTTPError) as exc_info:
                bank_http.request(
                    "GET", "https://bank.example/accounts",
                    provider="fake", retry=False, client=client, state=state,
                )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ivan@example.com" not in logged
    assert "Ivan Skachek" not in logged
    assert "not JSON" in logged
    assert len(logged) < 1000
    # The exception carries the same value, so a handler that renders it
    # cannot leak what the log refused to hold.
    assert "ivan@example.com" not in (exc_info.value.body or "")


def test_a_json_body_contributes_only_its_diagnostic_keys(state):
    """holder is not on the list, so it is dropped unread."""
    body = json.dumps(
        {
            "detail": "Required scope: accounts:read",
            "code": "IM001",
            "holder": "Ivan Skachek",
            "iban": "GB33BUKB20201555555555",
        }
    )
    with _client(
        lambda r: httpx.Response(
            403, text=body, headers={"content-type": "application/json"}
        )
    ) as client:
        with pytest.raises(bank_http.BankHTTPError) as exc_info:
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=False, client=client, state=state,
            )
    kept = exc_info.value.body or ""
    assert "Required scope: accounts:read" in kept
    assert "IM001" in kept
    assert "Ivan Skachek" not in kept
    assert "GB33BUKB20201555555555" not in kept


def test_a_key_a_future_provider_invents_is_dropped_rather_than_allowed(state):
    """The whole point of a positive list: the unanticipated shape yields nothing."""
    body = json.dumps({"problemNarrative": "account 12345678 belongs to Ivan"})
    with _client(
        lambda r: httpx.Response(
            400, text=body, headers={"content-type": "application/json"}
        )
    ) as client:
        with pytest.raises(bank_http.BankHTTPError) as exc_info:
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=False, client=client, state=state,
            )
    assert exc_info.value.body == "<application/json, no diagnostic key present>"


def test_an_email_inside_a_kept_diagnostic_value_is_still_scrubbed(state):
    """Second assertion at a second layer: a detail string may name a person."""
    body = json.dumps({"detail": "no mandate for ivan@example.com"})
    with _client(
        lambda r: httpx.Response(
            404, text=body, headers={"content-type": "application/json"}
        )
    ) as client:
        with pytest.raises(bank_http.BankHTTPError) as exc_info:
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=False, client=client, state=state,
            )
    kept = exc_info.value.body or ""
    assert "ivan@example.com" not in kept
    assert "{{EMAIL}}" in kept
    assert "no mandate for" in kept


def test_a_nested_error_list_is_harvested_but_bounded(state):
    body = json.dumps({"errors": [{"code": "E1", "detail": "first"},
                                  {"code": "E2", "detail": "second"}]})
    with _client(
        lambda r: httpx.Response(
            400, text=body, headers={"content-type": "application/json"}
        )
    ) as client:
        with pytest.raises(bank_http.BankHTTPError) as exc_info:
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=False, client=client, state=state,
            )
    kept = exc_info.value.body or ""
    assert "E1" in kept and "first" in kept
    assert len(kept) <= bank_http.BODY_LOG_CHARS


def test_the_log_line_carries_a_correlation_id_and_the_path_only(state, caplog):
    """A full URL can carry a token in a query string."""
    with caplog.at_level(logging.WARNING, logger="paypilot.bank.http"):
        with _client(lambda r: httpx.Response(500, text="boom")) as client:
            with pytest.raises(bank_http.BankHTTPError) as exc_info:
                bank_http.request(
                    "GET", "https://bank.example/accounts?access_token=SECRETVALUE",
                    provider="fake", retry=False, client=client, state=state,
                )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert exc_info.value.correlation_id in logged
    assert "SECRETVALUE" not in logged
    assert "/accounts" in logged


# --------------------------------------------------------------------------
# Alert on the second, with the negative half
# --------------------------------------------------------------------------

def _fail_once(state, events):
    with _client(lambda r: httpx.Response(500, text="boom")) as client:
        with pytest.raises(bank_http.BankHTTPError):
            bank_http.request(
                "GET", "https://bank.example/accounts",
                provider="fake", retry=False, client=client, state=state,
            )


def test_it_alerts_on_the_second_consecutive_failure_and_not_the_first(
    state, monkeypatch
):
    events: list[dict] = []
    monkeypatch.setattr(
        bank_http, "audit_security_event", lambda **kw: events.append(kw) or kw
    )

    _fail_once(state, events)
    assert events == [], "one blip clears itself more often than it persists"

    _fail_once(state, events)
    assert len(events) == 1
    assert events[0]["event"] == "bank_call_failed"
    assert events[0]["severity"] == "error"
    assert "2 consecutive failures" in events[0]["detail"]
    assert "500" in events[0]["detail"]


def test_a_success_resets_the_counter_so_two_old_blips_do_not_alert(
    state, monkeypatch
):
    events: list[dict] = []
    monkeypatch.setattr(
        bank_http, "audit_security_event", lambda **kw: events.append(kw) or kw
    )

    _fail_once(state, events)
    with _client(lambda r: httpx.Response(200, json={})) as client:
        bank_http.request(
            "GET", "https://bank.example/accounts",
            provider="fake", retry=False, client=client, state=state,
        )
    assert state.counts.get("fake") is None

    _fail_once(state, events)
    assert events == [], "the counter is consecutive, not cumulative"


def test_the_counter_is_per_provider(state, monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        bank_http, "audit_security_event", lambda **kw: events.append(kw) or kw
    )
    for provider in ("alpha", "beta"):
        with _client(lambda r: httpx.Response(500, text="boom")) as client:
            with pytest.raises(bank_http.BankHTTPError):
                bank_http.request(
                    "GET", "https://bank.example/accounts",
                    provider=provider, retry=False, client=client, state=state,
                )
    assert events == [], "one failure each is not two for either"


def test_the_counter_survives_a_restart(tmp_path, monkeypatch):
    """In memory, a process that restarts between failures never reaches two."""
    path = tmp_path / "failures.json"
    events: list[dict] = []
    monkeypatch.setattr(
        bank_http, "audit_security_event", lambda **kw: events.append(kw) or kw
    )

    first = bank_http._FailureState.load(path)
    _fail_once(first, events)
    assert events == []

    # A different object reading the same file: the restart.
    second = bank_http._FailureState.load(path)
    assert second.counts.get("fake") == 1
    _fail_once(second, events)
    assert len(events) == 1


def test_an_unreadable_state_file_reads_as_no_failures_rather_than_a_guess(tmp_path):
    path = tmp_path / "failures.json"
    path.write_text("{not json")
    assert bank_http._FailureState.load(path).counts == {}
