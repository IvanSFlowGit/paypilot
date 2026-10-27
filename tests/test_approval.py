"""Human approval of outbound mail: nothing leaves until a person says so.

No network. Resend is replaced at ``httpx.post`` and records every payload, so
each test can assert exactly WHO was mailed, which is the whole point: the
approver gets a request, the customer gets nothing until a decision.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import approval, loop, mailer, stripe_client
from app.store import STATE_FAILED, STATE_MESSAGED, STATE_RECOVERED

PORTAL = "https://billing.stripe.com/p/session/test_abc123"
CUSTOMER = "me@mine.test"
APPROVER = "owner@merchant.test"
BASE = "https://pay.merchant.test"


def _event(invoice_id="in_1"):
    return {
        "id": f"evt_{invoice_id}",
        "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": "cus_XYZ",
            "subscription": "sub_XYZ", "customer_email": CUSTOMER,
            "amount_due": 4900, "currency": "eur", "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


class _Resp:
    status_code = 200
    content = b"{}"

    @staticmethod
    def json():
        return {"id": "rs_ok"}


@pytest.fixture
def mail(monkeypatch):
    """Live sending configured, approval email mode, every Resend call recorded."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", CUSTOMER)
    monkeypatch.setenv("PAYPILOT_APPROVER_EMAILS", APPROVER)
    monkeypatch.setenv("PAYPILOT_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "0")
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid: PORTAL)
    sent: list[dict] = []

    def _post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _Resp()

    monkeypatch.setattr("httpx.post", _post)
    return sent


def _approval_link(sent):
    """The URL in the approver's email, as a person would tap it."""
    body = next(p["text"] for p in sent if APPROVER in p["to"])
    m = re.search(r"(https://pay\.merchant\.test/approve/(\d+)\?t=([0-9a-f]+))", body)
    assert m, body
    return m.group(1), int(m.group(2)), m.group(3)


def _customer_mails(sent):
    return [p for p in sent if CUSTOMER in p["to"]]


@pytest.fixture
def client():
    from app.api import app

    return TestClient(app)


# -- mode resolution ---------------------------------------------------------


def test_default_mode_is_a_human(monkeypatch):
    assert approval.approval_mode() == approval.MODE_EMAIL


def test_a_typo_never_turns_unattended_sending_on(monkeypatch):
    for value in ("of", "OFFF", "false", "0", "no", "disabled", " "):
        monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", value)
        assert approval.approval_mode() == approval.MODE_EMAIL, value


def test_off_must_be_written_out(monkeypatch):
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", " Off ")
    assert approval.approval_mode() == approval.MODE_OFF


def test_public_base_url_must_be_https(monkeypatch):
    monkeypatch.setenv("PAYPILOT_PUBLIC_BASE_URL", "http://pay.merchant.test")
    assert approval.public_base_url() == ""
    monkeypatch.setenv("PAYPILOT_PUBLIC_BASE_URL", "http://localhost:8000/")
    assert approval.public_base_url() == "http://localhost:8000"
    monkeypatch.setenv("PAYPILOT_PUBLIC_BASE_URL", "javascript:alert(1)")
    assert approval.public_base_url() == ""


# -- opening a request -------------------------------------------------------


def test_a_failed_payment_asks_the_approver_and_mails_the_customer_nothing(
    mail, isolated_store
):
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["status"] == "pending_approval"
    assert _customer_mails(mail) == [], "the customer must not be mailed before approval"
    assert len([p for p in mail if APPROVER in p["to"]]) == 1
    assert isolated_store.get_failure("in_1")["state"] == STATE_FAILED
    assert isolated_store.sent_message_count("in_1") == 0


def test_the_approval_email_carries_the_exact_text_and_one_link(mail, isolated_store):
    loop.handle_payment_failed(_event(), isolated_store)
    body = next(p["text"] for p in mail if APPROVER in p["to"])
    row = isolated_store.approvals_for("in_1")[0]

    assert row["body"] in body, "the approver must see the exact text that will be sent"
    assert PORTAL in body
    assert CUSTOMER in body
    assert len(re.findall(r"https://pay\.merchant\.test/approve/", body)) == 1


def test_the_raw_token_is_never_stored(mail, isolated_store):
    loop.handle_payment_failed(_event(), isolated_store)
    _, _, token = _approval_link(mail)
    row = isolated_store.approvals_for("in_1")[0]

    assert token not in str(row)
    assert row["token_hash"] == approval.hash_token(token)


def test_no_approver_means_nothing_is_sent_and_nobody_is_asked(
    mail, isolated_store, monkeypatch
):
    monkeypatch.delenv("PAYPILOT_APPROVER_EMAILS")
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["status"] == "suppressed"
    assert result["delivery"]["error"] == "no_approver_configured"
    assert mail == []


def test_no_public_url_means_nothing_is_sent(mail, isolated_store, monkeypatch):
    monkeypatch.delenv("PAYPILOT_PUBLIC_BASE_URL")
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["error"] == "no_public_base_url"
    assert mail == []


def test_an_undeliverable_request_fails_closed(mail, isolated_store, monkeypatch):
    class _Down:
        status_code = 500
        content = b""

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Down())
    monkeypatch.setenv("PAYPILOT_SEND_MAX_ATTEMPTS", "1")
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["error"] == "approval_request_undeliverable"
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_FAILED


def test_sending_off_asks_nobody(isolated_store, monkeypatch, mail):
    monkeypatch.delenv("PAYPILOT_SEND_EMAIL")
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["status"] == mailer.STATUS_DRY_RUN
    assert mail == []
    assert isolated_store.approvals_for("in_1") == []


def test_a_recipient_nobody_cleared_asks_nobody(isolated_store, monkeypatch, mail):
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", "someone-else@x.test")
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["error"] == "recipient_not_allowlisted"
    assert mail == []


def test_a_retry_storm_does_not_stack_requests(mail, isolated_store):
    loop.handle_payment_failed(_event(), isolated_store)
    again = _event()
    again["id"] = "evt_retry"
    again["data"]["object"]["attempt_count"] = 2
    result = loop.handle_payment_failed(again, isolated_store)

    assert result["delivery"]["status"] == "pending_approval"
    assert result["delivery"]["duplicate"] is True
    assert len([p for p in mail if APPROVER in p["to"]]) == 1


# -- the page: a link is not a click -----------------------------------------


def test_opening_the_link_sends_nothing(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    url, aid, token = _approval_link(mail)
    before = len(mail)

    for _ in range(3):  # a mail scanner, a previewer, and the person
        r = client.get(f"/approve/{aid}", params={"t": token})
        assert r.status_code == 200

    assert len(mail) == before
    assert _customer_mails(mail) == []
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_the_page_shows_the_message_and_never_leaks_the_token_elsewhere(
    mail, isolated_store, client
):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    r = client.get(f"/approve/{aid}", params={"t": token})
    row = isolated_store.approvals_for("in_1")[0]

    assert "Send this email" in r.text
    assert CUSTOMER in r.text
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert "form-action 'self'" in r.headers["content-security-policy"]
    assert "script" not in r.headers["content-security-policy"]
    # The token appears only in the hidden field the approver needs.
    assert r.text.count(token) == 1
    assert row["body"].split("\n")[0].split(",")[0][:20] in r.text


def test_the_page_escapes_everything(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    isolated_store._conn.execute(
        "UPDATE approvals SET subject = ?, recipient = ? WHERE id = ?",
        ("<script>alert(1)</script>", '"><img src=x onerror=alert(2)>', aid),
    )
    isolated_store._conn.commit()
    r = client.get(f"/approve/{aid}", params={"t": token})

    assert "<script>alert" not in r.text
    assert "<img src=x" not in r.text


def test_a_wrong_token_and_a_wrong_id_look_identical(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)

    wrong_token = client.get(f"/approve/{aid}", params={"t": "0" * 48})
    wrong_id = client.get("/approve/9999", params={"t": token})
    no_token = client.get(f"/approve/{aid}")

    assert wrong_token.status_code == wrong_id.status_code == no_token.status_code == 404
    assert wrong_token.text == wrong_id.text == no_token.text


# -- deciding ----------------------------------------------------------------


def test_approving_sends_exactly_one_email_to_the_customer(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)

    r = client.post(f"/approve/{aid}", content=f"t={token}&action=approve",
                    headers={"content-type": "application/x-www-form-urlencoded"})

    assert r.status_code == 200 and "Sent" in r.text
    mails = _customer_mails(mail)
    assert len(mails) == 1
    assert mails[0]["text"] == isolated_store.approvals_for("in_1")[0]["body"]
    assert isolated_store.get_failure("in_1")["state"] == STATE_MESSAGED
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_SENT
    assert isolated_store.sent_message_count("in_1") == 1


def test_a_double_tap_sends_once(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    form = {"content": f"t={token}&action=approve",
            "headers": {"content-type": "application/x-www-form-urlencoded"}}

    first = client.post(f"/approve/{aid}", **form)
    second = client.post(f"/approve/{aid}", **form)

    assert "Sent" in first.text
    assert "Already decided" in second.text
    assert len(_customer_mails(mail)) == 1


def test_skipping_sends_nothing_and_is_remembered(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    r = client.post(f"/approve/{aid}", content=f"t={token}&action=skip",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert "Skipped" in r.text and _customer_mails(mail) == []

    # Stripe retries; the person already answered, so they are not asked again.
    again = _event()
    again["id"] = "evt_next"
    again["data"]["object"]["attempt_count"] = 2
    result = loop.handle_payment_failed(again, isolated_store)
    assert result["delivery"]["error"] == "approver_skipped"
    assert len([p for p in mail if APPROVER in p["to"]]) == 1


def test_a_wrong_token_cannot_approve(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, _ = _approval_link(mail)
    r = client.post(f"/approve/{aid}", content="t=" + "1" * 48 + "&action=approve",
                    headers={"content-type": "application/x-www-form-urlencoded"})

    assert r.status_code == 404
    assert _customer_mails(mail) == []
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_an_unknown_action_does_nothing(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    client.post(f"/approve/{aid}", content=f"t={token}&action=send",
                headers={"content-type": "application/x-www-form-urlencoded"})

    assert _customer_mails(mail) == []
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_a_customer_who_paid_meanwhile_is_not_mailed(mail, isolated_store):
    """Hours pass between the request and the tap. The worst message this
    product can send is a dunning email to someone who already paid."""
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    isolated_store.transition("in_1", STATE_RECOVERED, reason="invoice.paid")

    result = approval.decide(aid, token, "approve", isolated_store)

    assert result["outcome"] == "blocked"
    assert result["reason"] == "invoice_already_recovered"
    assert _customer_mails(mail) == []
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_BLOCKED


def test_the_touch_cap_is_rechecked_at_approval_time(mail, isolated_store, monkeypatch):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    monkeypatch.setenv("PAYPILOT_MAX_TOUCHES", "1")
    isolated_store.record_message(invoice_id="in_1", status="sent")

    result = approval.decide(aid, token, "approve", isolated_store)

    assert result["outcome"] == "blocked" and result["reason"] == "max_touches_reached"
    assert _customer_mails(mail) == []


def test_the_cooldown_is_rechecked_at_approval_time(mail, isolated_store, monkeypatch):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    monkeypatch.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "24")
    isolated_store.record_message(invoice_id="in_other", status="sent")
    isolated_store._conn.execute(
        "UPDATE failures SET stripe_customer_id = 'cus_XYZ' WHERE invoice_id = 'in_1'"
    )
    isolated_store.record_failure(
        invoice_id="in_other", customer_id="cust_001", amount_minor=100, currency="eur",
        attempt_count=1, failure_code="expired_card", stripe_customer_id="cus_XYZ",
        subscription_id="sub_XYZ",
    )
    isolated_store.record_message(invoice_id="in_other", status="sent")

    result = approval.decide(aid, token, "approve", isolated_store)

    assert result["outcome"] == "blocked" and result["reason"] == "cooldown_active"


def test_a_body_changed_after_the_request_is_not_sent(mail, isolated_store):
    """What the approver read is what is sent, or nothing is."""
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    isolated_store._conn.execute(
        "UPDATE approvals SET body = body || ' extra' WHERE id = ?", (aid,)
    )
    isolated_store._conn.commit()

    result = approval.decide(aid, token, "approve", isolated_store)

    assert result["outcome"] == "blocked"
    assert result["reason"] == "body_changed_since_request"
    assert _customer_mails(mail) == []


def test_an_expired_request_cannot_be_approved(mail, isolated_store):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    isolated_store._conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", (past, aid))
    isolated_store._conn.commit()

    result = approval.decide(aid, token, "approve", isolated_store)

    assert result["outcome"] == "expired"
    assert _customer_mails(mail) == []
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_EXPIRED


def test_the_sweep_expires_and_records_why_nothing_was_sent(mail, isolated_store):
    loop.handle_payment_failed(_event(), isolated_store)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    isolated_store._conn.execute("UPDATE approvals SET expires_at = ?", (past,))
    isolated_store._conn.commit()

    out = approval.sweep(isolated_store)

    assert out["expired"] == 1
    assert any(m["error"] == "approval_expired" for m in isolated_store.messages_for("in_1"))
    assert _customer_mails(mail) == []


def test_a_provider_failure_after_approval_is_recorded_not_hidden(
    mail, isolated_store, monkeypatch
):
    loop.handle_payment_failed(_event(), isolated_store)
    _, aid, token = _approval_link(mail)

    class _Rejected:
        status_code = 422
        content = b""

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Rejected())
    result = approval.decide(aid, token, "approve", isolated_store)

    assert result["outcome"] == "failed"
    assert isolated_store.get_failure("in_1")["state"] == STATE_FAILED
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_FAILED


# -- the unattended path stays explicit --------------------------------------


def test_off_sends_without_asking(mail, isolated_store, monkeypatch):
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "off")
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["status"] == mailer.STATUS_SENT
    assert len(_customer_mails(mail)) == 1
    assert isolated_store.approvals_for("in_1") == []


# -- disclosure is only true when a person reviewed it ----------------------


def test_the_reviewed_claim_is_made_only_when_someone_reviews(monkeypatch):
    monkeypatch.setenv("PAYPILOT_AI_DISCLOSURE", "1")
    assert "reviewed before it was sent" in loop.ai_disclosure()

    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "off")
    assert "reviewed" not in loop.ai_disclosure()
    assert loop.ai_disclosure() == loop.UNREVIEWED_AI_DISCLOSURE


# -- the admin surface -------------------------------------------------------


def test_the_listing_never_exposes_bodies_or_hashes(mail, isolated_store, client):
    loop.handle_payment_failed(_event(), isolated_store)
    r = client.get("/approvals")

    assert r.status_code == 200
    row = r.json()["approvals"][0]
    assert "body" not in row and "token_hash" not in row
    assert row["status"] == "pending"


def test_the_listing_needs_the_admin_token_when_one_is_set(
    mail, isolated_store, client, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "secret-admin")
    assert client.get("/approvals").status_code in (401, 403)
    assert client.post("/approvals/sweep").status_code in (401, 403)
    ok = client.get("/approvals", headers={"authorization": "Bearer secret-admin"})
    assert ok.status_code == 200


# -- erasure reaches approvals ----------------------------------------------


def test_erasing_a_customer_removes_their_approval_rows(mail, isolated_store):
    loop.handle_payment_failed(_event(), isolated_store)
    assert isolated_store.approvals_for("in_1")

    deleted = isolated_store.erase_customer("cus_XYZ")

    assert deleted["approvals"] == 1
    assert isolated_store.approvals_for("in_1") == []
    assert isolated_store.export_customer("cus_XYZ")["approvals"] == []


# -- the preflight an operator runs before switching a merchant on ------------


def _preflight():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "approval_preflight.py"
    spec = importlib.util.spec_from_file_location("approval_preflight", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ready_env(monkeypatch):
    for k, v in {
        "PAYPILOT_ALLOWED_RECIPIENTS": CUSTOMER, "RESEND_API_KEY": "re_test",
        "STRIPE_WEBHOOK_SECRET": "whsec_x", "ADMIN_TOKEN": "adm",
        "PAYPILOT_APPROVER_EMAILS": APPROVER, "PAYPILOT_PUBLIC_BASE_URL": BASE,
    }.items():
        monkeypatch.setenv(k, v)


def test_preflight_passes_a_complete_email_setup(monkeypatch, capsys):
    _ready_env(monkeypatch)
    assert _preflight().main([]) == 0
    assert "READY" in capsys.readouterr().out


def test_preflight_names_every_missing_requirement(monkeypatch, capsys):
    assert _preflight().main([]) == 1
    out = capsys.readouterr().out
    for needle in ("PAYPILOT_ALLOWED_RECIPIENTS", "RESEND_API_KEY", "STRIPE_WEBHOOK_SECRET",
                   "ADMIN_TOKEN", "PAYPILOT_APPROVER_EMAILS", "PAYPILOT_PUBLIC_BASE_URL"):
        assert needle in out


@pytest.mark.parametrize("drop", ["PAYPILOT_APPROVER_EMAILS", "PAYPILOT_PUBLIC_BASE_URL",
                                  "RESEND_API_KEY", "ADMIN_TOKEN"])
def test_preflight_fails_when_any_single_requirement_is_removed(monkeypatch, drop):
    _ready_env(monkeypatch)
    monkeypatch.delenv(drop)
    assert _preflight().main([]) == 1


def test_preflight_will_not_call_unattended_sending_ready(monkeypatch, capsys):
    _ready_env(monkeypatch)
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "off")
    assert _preflight().main([]) == 1
    assert "no person approving" in capsys.readouterr().out


def test_preflight_gmail_mode_needs_the_google_credentials(monkeypatch, capsys):
    _ready_env(monkeypatch)
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "gmail")
    assert _preflight().main([]) == 1
    assert "refresh token" in capsys.readouterr().out


def test_preflight_live_checks_report_could_not_run_not_a_pass(monkeypatch, capsys):
    _ready_env(monkeypatch)
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "gmail")
    assert _preflight().main(["--gmail-live"]) == 2
    monkeypatch.delenv("PAYPILOT_APPROVER_EMAILS")
    assert _preflight().main(["--send-test"]) == 2


def test_preflight_imap_mode_names_what_is_missing_and_guesses_no_port(monkeypatch, capsys):
    _ready_env(monkeypatch)
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "imap")
    assert _preflight().main([]) == 1
    out = capsys.readouterr().out
    assert "PAYPILOT_IMAP_HOST" in out and "Proton Bridge port" in out
    for k, v in (("HOST", "127.0.0.1"), ("PORT", "1143"), ("USER", "u"), ("PASSWORD", "p")):
        monkeypatch.setenv(f"PAYPILOT_IMAP_{k}", v)
    assert _preflight().main([]) == 0
    monkeypatch.setenv("PAYPILOT_IMAP_SECURITY", "none")
    assert _preflight().main([]) == 1


def test_preflight_imap_live_reports_could_not_run_not_a_pass(monkeypatch):
    _ready_env(monkeypatch)
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "imap")
    assert _preflight().main(["--imap-live"]) == 2


def test_the_unknown_mode_typo_still_lands_on_a_human(monkeypatch):
    for value in ("IMAP ", "imap"):
        monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", value)
        assert approval.approval_mode() == approval.MODE_IMAP
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "imaps")
    assert approval.approval_mode() == approval.MODE_EMAIL
