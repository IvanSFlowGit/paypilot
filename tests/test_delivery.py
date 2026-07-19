"""Tests for recovery links, the link allowlist, and email delivery.

This is the phase where text becomes something a real person receives, so the
tests are weighted toward the refusals: sending when sending is off, mailing an
address nobody cleared, and letting an injected link ride out in a message body.

No network. The Resend call is monkeypatched at ``httpx.post`` and the Stripe
SDK at the ``get_stripe`` seam.
"""

from __future__ import annotations

import pytest

from app import loop, mailer, stripe_client
from app.safety import PAYMENT_UPDATE_URL, find_foreign_urls, message_violations
from app.store import STATE_FAILED, STATE_MESSAGED

PORTAL_URL = "https://billing.stripe.com/p/session/test_abc123"
INVOICE_URL = "https://invoice.stripe.com/i/acct_1/test_xyz"


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    return monkeypatch


@pytest.fixture(autouse=True)
def clean_mail_env(monkeypatch):
    """Start every test from "sending off, nobody allowlisted"."""
    for var in (
        "PAYPILOT_SEND_EMAIL",
        "PAYPILOT_ALLOWED_RECIPIENTS",
        "RESEND_API_KEY",
        "PAYPILOT_ALLOWED_LINK_HOSTS",
        "STRIPE_API_KEY",
        "STRIPE_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Link allowlist
# ---------------------------------------------------------------------------

def test_stripe_hosted_links_are_allowed():
    assert find_foreign_urls(f"Update here: {PORTAL_URL}") == []
    assert find_foreign_urls(f"Pay here: {INVOICE_URL}") == []


def test_lookalike_host_is_not_allowed():
    """Exact host match, never a suffix match."""
    evil = "https://billing.stripe.com.evil.test/p/session/x"
    assert find_foreign_urls(f"click {evil}") == [evil]


def test_foreign_link_still_fails_the_guard():
    assert find_foreign_urls("visit http://evil.example now") == ["http://evil.example"]


def test_host_allowlist_can_be_narrowed_to_client_domains(monkeypatch):
    monkeypatch.setenv("PAYPILOT_ALLOWED_LINK_HOSTS", "billing.acme.test")
    assert find_foreign_urls(f"see {PORTAL_URL}") == [PORTAL_URL]
    assert find_foreign_urls("see https://billing.acme.test/portal") == []


def test_exact_mode_rejects_any_other_url_on_an_allowed_host():
    """What the outbound gate uses: this link, not any link on the host."""
    other = "https://billing.stripe.com/p/session/someone_else"
    assert message_violations(f"go to {other}", (PORTAL_URL,), allow_hosts=False)
    assert message_violations(f"go to {PORTAL_URL}", (PORTAL_URL,), allow_hosts=False) == []


def test_default_allowed_url_still_passes():
    assert find_foreign_urls(f"Update: {PAYMENT_UPDATE_URL}") == []


# ---------------------------------------------------------------------------
# Recovery link resolution
# ---------------------------------------------------------------------------

def test_recovery_link_prefers_the_portal_session(monkeypatch):
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: PORTAL_URL)
    assert stripe_client.recovery_link(
        stripe_customer_id="cus_1", hosted_invoice_url=INVOICE_URL
    ) == PORTAL_URL


def test_recovery_link_falls_back_to_the_hosted_invoice(monkeypatch):
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: None)
    assert stripe_client.recovery_link(
        stripe_customer_id="cus_1", hosted_invoice_url=INVOICE_URL
    ) == INVOICE_URL


def test_recovery_link_falls_back_to_the_static_url(monkeypatch):
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: None)
    assert stripe_client.recovery_link() == PAYMENT_UPDATE_URL


def test_a_link_from_stripe_is_still_checked_against_the_allowlist(monkeypatch):
    """Trusting an upstream response is how an allowlist gets bypassed."""
    monkeypatch.setattr(
        stripe_client, "create_portal_session", lambda cid: "https://evil.test/phish"
    )
    assert stripe_client.recovery_link(stripe_customer_id="cus_1") == PAYMENT_UPDATE_URL


def test_portal_session_errors_degrade_rather_than_raise(monkeypatch):
    class _Boom:
        class billing_portal:
            class Session:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("no portal configuration")

    monkeypatch.setattr(stripe_client, "get_stripe", lambda: _Boom)
    assert stripe_client.create_portal_session("cus_1") is None


def test_no_stripe_key_means_no_portal_call(monkeypatch):
    assert stripe_client.get_stripe() is None
    assert stripe_client.create_portal_session("cus_1") is None


# ---------------------------------------------------------------------------
# Mailer guards
# ---------------------------------------------------------------------------

def _send(store, to="customer@example.test"):
    return mailer.send_dunning_email(
        invoice_id="in_1", to=to, subject="s", body="b", store=store
    )


def test_sending_is_off_by_default(isolated_store, monkeypatch):
    called = []
    monkeypatch.setattr("httpx.post", lambda *a, **k: called.append(1))

    result = _send(isolated_store)
    assert result["status"] == mailer.STATUS_DRY_RUN
    assert called == [], "dry run must not touch the provider"
    assert isolated_store.messages_for("in_1")[0]["status"] == "dry_run"


def test_unset_allowlist_blocks_every_live_send(isolated_store, monkeypatch):
    """An unset variable means nobody, not everybody."""
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    called = []
    monkeypatch.setattr("httpx.post", lambda *a, **k: called.append(1))

    result = _send(isolated_store)
    assert result["status"] == mailer.STATUS_SUPPRESSED
    assert result["error"] == "recipient_not_allowlisted"
    assert called == []


def test_recipient_off_the_allowlist_is_blocked(isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "me@mine.test")
    called = []
    monkeypatch.setattr("httpx.post", lambda *a, **k: called.append(1))

    result = _send(isolated_store, to="stranger@elsewhere.test")
    assert result["status"] == mailer.STATUS_SUPPRESSED
    assert called == []


def test_allowlisted_recipient_sends(isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "Me@Mine.test")

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_abc"}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())

    result = _send(isolated_store, to="me@mine.test")  # case-insensitive match
    assert result["status"] == mailer.STATUS_SENT
    assert result["provider_message_id"] == "rs_abc"
    assert isolated_store.sent_message_count("in_1") == 1


def test_missing_api_key_is_a_blocked_send_not_a_crash(isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")

    result = _send(isolated_store)
    assert result["status"] == mailer.STATUS_SUPPRESSED
    assert result["error"] == "missing_resend_api_key"


def test_wildcard_allowlist_must_be_written_out(monkeypatch):
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")
    assert mailer.is_allowed_recipient("anyone@anywhere.test") is True


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

def test_server_errors_are_retried(isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")
    monkeypatch.setattr(mailer, "_BACKOFF_SECONDS", 0.0)

    calls = []

    class _Resp:
        status_code = 500
        content = b""

    def _post(*a, **k):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr("httpx.post", _post)

    result = _send(isolated_store)
    assert result["status"] == mailer.STATUS_FAILED
    assert len(calls) == mailer._MAX_ATTEMPTS


def test_client_errors_are_not_retried(isolated_store, monkeypatch):
    """A 422 means the request is wrong; repeating it just annoys the provider."""
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")

    calls = []

    class _Resp:
        status_code = 422
        content = b""

    def _post(*a, **k):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr("httpx.post", _post)

    result = _send(isolated_store)
    assert result["status"] == mailer.STATUS_FAILED
    assert len(calls) == 1


def test_bounce_marks_the_message_and_drops_the_touch(isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "*")

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_bounce"}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    _send(isolated_store)
    assert isolated_store.sent_message_count("in_1") == 1

    assert mailer.mark_bounced("rs_bounce", isolated_store) is True
    assert isolated_store.sent_message_count("in_1") == 0


def test_bounce_for_an_unknown_message_is_a_no_op(isolated_store):
    assert mailer.mark_bounced("rs_never_seen", isolated_store) is False


# ---------------------------------------------------------------------------
# Composition and the outbound gate
# ---------------------------------------------------------------------------

def test_composed_body_carries_exactly_one_link():
    body = loop.compose_email_body("Hi there, your card expired.", PORTAL_URL)
    assert body.count(PORTAL_URL) == 1
    assert message_violations(body, (PORTAL_URL,), allow_hosts=False) == []


def test_injected_link_in_the_draft_blocks_the_send(no_key, isolated_store, monkeypatch):
    """The regression that matters: a foreign URL that reached the drafted copy
    must stop the email, not ride out on it."""
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: PORTAL_URL)
    sent = []
    monkeypatch.setattr(
        loop, "send_dunning_email", lambda **kw: sent.append(kw) or {"status": "sent"}
    )

    poisoned = {"message": "Hi, pay at http://evil.test/steal now."}
    result = loop.deliver_recovery(
        invoice_id="in_1",
        recovery=poisoned,
        row={"customer_email": "me@mine.test", "failure_code": "card_expired"},
        store=isolated_store,
    )

    assert result["status"] == "suppressed"
    assert result["error"] == "failed_output_guard"
    assert sent == [], "no send may happen after a guard failure"
    assert isolated_store.messages_for("in_1")[0]["status"] == "suppressed"


def test_no_recipient_address_suppresses_the_send(no_key, isolated_store, monkeypatch):
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: PORTAL_URL)
    result = loop.deliver_recovery(
        invoice_id="in_1",
        recovery={"message": "Hi there."},
        row={"customer_email": None, "customer_id": None, "failure_code": "card_expired"},
        store=isolated_store,
    )
    assert result["status"] == "suppressed"
    assert result["error"] == "no_recipient_address"


# ---------------------------------------------------------------------------
# State only advances on a real send
# ---------------------------------------------------------------------------

def _failed_event(invoice_id="in_1"):
    return {
        "id": "evt_1",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "object": "invoice",
                "id": invoice_id,
                "customer": "cus_XYZ",
                "subscription": "sub_XYZ",
                "customer_email": "me@mine.test",
                "amount_due": 4900,
                "currency": "eur",
                "attempt_count": 1,
                "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
                "metadata": {"paypilot_customer_id": "cust_001"},
            }
        },
    }


def test_dry_run_leaves_the_invoice_at_failed(no_key, isolated_store, monkeypatch):
    """Claiming we messaged someone we did not is what this ledger exists to
    prevent."""
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: PORTAL_URL)
    result = loop.handle_payment_failed(_failed_event(), isolated_store)

    assert result["delivery"]["status"] == mailer.STATUS_DRY_RUN
    assert isolated_store.get_failure("in_1")["state"] == STATE_FAILED


def test_a_real_send_advances_to_messaged(no_key, isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "me@mine.test")
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: PORTAL_URL)

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_ok"}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())

    result = loop.handle_payment_failed(_failed_event(), isolated_store)
    assert result["delivery"]["status"] == mailer.STATUS_SENT
    assert isolated_store.get_failure("in_1")["state"] == STATE_MESSAGED
    assert result["delivery"]["link"] == PORTAL_URL


def test_a_suppressed_send_leaves_the_invoice_at_failed(no_key, isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    # Allowlist deliberately excludes the customer's address.
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "someone.else@mine.test")
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: PORTAL_URL)

    loop.handle_payment_failed(_failed_event(), isolated_store)
    assert isolated_store.get_failure("in_1")["state"] == STATE_FAILED
