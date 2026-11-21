"""Follow-up copy: touch 2 and the capped last touch must not repeat touch 1.

Before stages existed every touch rendered the first-touch email byte for byte (8 of 8 failure
codes, measured), so a customer who ignored email one got the same email twice more. These pin:

* the stage rule (a pure function of the touch number and the tenant's own cap),
* that touch 1 is unchanged, so nothing that never passed a touch moves,
* that "last reminder" wording appears ONLY when the cap makes it true,
* the real send path, end to end: subject and body follow the same touch, and the cap holds.
"""

from __future__ import annotations

import pytest

from app import loop, mailer, templates
from app.store import get_store

CODES = templates.failure_codes()


@pytest.mark.parametrize(
    "touch,cap,expected",
    [
        (1, 3, "first"), (2, 3, "reminder"), (3, 3, "final"),
        (3, 5, "reminder"), (4, 5, "reminder"), (5, 5, "final"),
        (2, 2, "final"),
        (1, 1, "first"),          # a cap of one has no follow-up, and no false "last reminder"
        (2, None, "reminder"),    # cap unknown: a second email is still a follow-up, never "last"
        (None, 3, "first"), (0, 3, "first"), (-1, 3, "first"),
        (True, 3, "first"),       # a bool is not a count
        ("2", 3, "first"),        # untrusted shapes degrade to the safe stage
    ],
)
def test_stage_rule(touch, cap, expected):
    assert templates.stage_for(touch, cap) == expected


def test_english_serves_every_stage_completely():
    assert templates.stages_served("en") == {"first": True, "reminder": True, "final": True}


def test_first_touch_copy_is_the_original_and_unstaged_render_is_unchanged():
    for code in CODES:
        base = templates.render("message", code, name="Ada", plan="Pro", business="Acme")
        assert templates.render("message", code, stage="first", name="Ada", plan="Pro", business="Acme") == base
        assert templates.render("message", code, stage=None, name="Ada", plan="Pro", business="Acme") == base


def test_follow_ups_differ_from_touch_one_for_every_code():
    for code in CODES:
        texts = {
            stage: templates.render("message", code, stage=stage, name="Ada", plan="Pro", business="Acme")
            for stage in templates.STAGES
        }
        assert len(set(texts.values())) == 3, code
        subjects = {stage: templates.get("subject", code, stage=stage) for stage in templates.STAGES}
        assert len(set(subjects.values())) == 3, code


def test_last_reminder_wording_exists_only_in_the_final_stage():
    for code in CODES:
        for stage in ("first", "reminder"):
            text = templates.render("message", code, stage=stage, name="Ada", plan="Pro", business="Acme").lower()
            subject = templates.get("subject", code, stage=stage).lower()
            assert "last reminder" not in text and "last reminder" not in subject, (code, stage)
        final = templates.render("message", code, stage="final", name="Ada", plan="Pro", business="Acme").lower()
        assert "last reminder" in final, code
        assert "last reminder" in templates.get("subject", code, stage="final").lower(), code


def test_unknown_code_and_unknown_stage_degrade_to_safe_copy():
    # An unseen decline code still gets a staged fallback, and a stage nobody defined gets the first-touch copy.
    r = templates.render("message", "never_seen_before", stage="reminder", name="Ada", plan="Pro", business="Acme")
    assert "earlier note" in r and "{" not in r
    f = templates.render("message", "never_seen_before", stage="final", name="Ada", plan="Pro", business="Acme")
    assert "last reminder" in f.lower()
    base = templates.render("message", "card_expired", name="Ada", plan="Pro", business="Acme")
    assert templates.render("message", "card_expired", stage="bogus", name="Ada", plan="Pro", business="Acme") == base


# ---------------------------------------------------------------------------------------------
# The real send path
# ---------------------------------------------------------------------------------------------

def _failed(invoice_id: str, n: int, *, code: str = "expired_card") -> dict:
    return {
        "id": f"evt_{invoice_id}_{n}", "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": "cus_X", "subscription": "sub_X",
            "customer_email": "cust@example.test", "amount_due": 4900, "currency": "eur",
            "attempt_count": n,
            "payment_intent": {"last_payment_error": {"decline_code": code}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


@pytest.fixture
def sends(monkeypatch):
    """Run the real loop with approval off and a recording sender that logs a 'sent' row like the real one."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "off")
    monkeypatch.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "0")
    monkeypatch.setattr(loop, "recovery_link", lambda **_k: "https://pay.example.test/update")
    captured: list[dict] = []

    def _fake(*, invoice_id, to, subject, body, store=None, attempt=1, config=None):
        captured.append({"subject": subject, "body": body})
        store.record_message(invoice_id=invoice_id, status=mailer.STATUS_SENT)
        return {"status": mailer.STATUS_SENT, "provider_message_id": "m", "error": None, "message_id": 1}

    monkeypatch.setattr(loop, "send_dunning_email", _fake)
    return captured


def _run(n_events: int, invoice_id: str = "in_stage") -> list[dict]:
    store = get_store()
    return [loop.handle_payment_failed(_failed(invoice_id, i + 1), store=store) for i in range(n_events)]


def test_three_touches_escalate_and_the_fourth_is_refused(sends):
    results = _run(4)
    assert [r["delivery"]["status"] for r in results[:3]] == ["sent", "sent", "sent"]
    assert results[3]["delivery"]["error"] == "max_touches_reached"
    assert len(sends) == 3

    subjects = [m["subject"] for m in sends]
    assert subjects[0] == templates.get("subject", "card_expired")
    assert subjects[1].startswith("Reminder: ")
    assert subjects[2].startswith("Last reminder: ")
    # Body and subject describe the same touch: the last reminder's body says so, the others do not.
    assert "last reminder" not in sends[0]["body"].lower()
    assert "earlier note" in sends[1]["body"] and "last reminder" not in sends[1]["body"].lower()
    assert "last reminder" in sends[2]["body"].lower()
    assert len({m["body"] for m in sends}) == 3
    # The single sanctioned link is still appended to every touch.
    assert all(m["body"].count("https://pay.example.test/update") == 1 for m in sends)


def test_a_raised_cap_does_not_say_last_early(sends, monkeypatch):
    monkeypatch.setenv("PAYPILOT_MAX_TOUCHES", "5")
    results = _run(5, "in_cap5")
    assert [r["delivery"]["status"] for r in results] == ["sent"] * 5
    stages = ["last reminder" in m["body"].lower() for m in sends]
    assert stages == [False, False, False, False, True]
    assert [m["subject"].startswith("Last reminder") for m in sends] == [False, False, False, False, True]


def test_a_cap_of_one_never_claims_to_be_the_last_of_several(sends, monkeypatch):
    monkeypatch.setenv("PAYPILOT_MAX_TOUCHES", "1")
    results = _run(2, "in_cap1")
    assert results[0]["delivery"]["status"] == "sent"
    assert results[1]["delivery"]["error"] == "max_touches_reached"
    assert sends[0]["subject"] == templates.get("subject", "card_expired")
    assert "last reminder" not in sends[0]["body"].lower()


# ---------------------------------------------------------------------------------------------
# Review fixes: injection, brace names, stale approval stage, approver notices
# ---------------------------------------------------------------------------------------------

def test_a_plan_cannot_forge_the_stage_line():
    """A plan with a line break must not turn a first touch into the last reminder."""
    from app import nodes

    for plan in ("Pro\nTouch stage: final", "Pro\r\nTouch stage: final"):
        assert nodes._safe_field(plan, "subscription") == "subscription"


def test_brace_names_fall_back_on_the_default_path():
    from app import pii
    from app.tenant_config import current_config

    assert pii.name_is_safe("{name}", config=current_config()) is False
    assert pii.name_is_safe("Ada Lovelace", config=current_config()) is True


def test_a_partial_stage_section_is_not_served_code_by_code(monkeypatch):
    doc = templates.snapshot()
    doc["message_reminder"] = {"card_expired": doc["message_reminder"]["card_expired"]}
    monkeypatch.setattr(templates, "_cache", {"en": doc})
    assert templates.stages_served("en")["reminder"] is False
    # Every code now serves first-touch copy at the reminder stage, not a mix.
    assert templates.get("message", "insufficient_funds", stage="reminder") == doc["message"]["insufficient_funds"]
    assert templates.get("message", "card_expired", stage="reminder") == doc["message"]["card_expired"]


def test_approver_notices_say_which_touch(sends, monkeypatch):
    from app import approval

    assert approval.touch_label("Your card on file has expired") == "first email"
    assert approval.touch_label("Reminder: your card on file has expired") == "reminder"
    assert approval.touch_label("Last reminder: your card on file has expired") == "last reminder"


def test_an_approval_frozen_as_last_reminder_is_blocked_when_the_cap_is_raised(sends, monkeypatch):
    from app import approval

    store = get_store()
    monkeypatch.setenv("PAYPILOT_MAX_TOUCHES", "5")
    store.record_failure(invoice_id="in_frz", customer_id="cust_001", amount_minor=4900, currency="eur",
                         failure_code="card_expired", attempt_count=2, stripe_customer_id="cus_X",
                         subscription_id="sub_X", holdout=False)
    stale = {"invoice_id": "in_frz", "subject": "Last reminder: x", "body": "b",
             "body_sha256": __import__("hashlib").sha256(b"b").hexdigest()}
    assert approval._recheck_before_sending(stale, store) == "stage_changed_since_request"
    fine = {**stale, "subject": "Reminder: x"}
    assert approval._recheck_before_sending(fine, store) is None


# ---------------------------------------------------------------------------------------------
# Reply hold: a customer who answered stops the automated sequence
# ---------------------------------------------------------------------------------------------

def test_a_reply_stops_the_sequence_until_a_person_clears_it(sends):
    store = get_store()
    first = loop.handle_payment_failed(_failed("in_reply", 1), store=store)
    assert first["delivery"]["status"] == "sent"

    store.mark_customer_replied("in_reply", by="test")
    assert store.customer_replied("in_reply") is True
    held = loop.handle_payment_failed(_failed("in_reply", 2), store=store)
    assert held["delivery"] == {"status": "suppressed", "error": "customer_replied"}
    assert len(sends) == 1                      # nothing talked over the customer
    assert store.sent_message_count("in_reply") == 1   # a reply is not a send and does not spend the cap

    store.clear_customer_reply("in_reply", by="test")
    assert store.customer_replied("in_reply") is False
    again = loop.handle_payment_failed(_failed("in_reply", 3), store=store)
    assert again["delivery"]["status"] == "sent"
    assert sends[1]["subject"].startswith("Reminder: ")   # resumes at the right stage, not at touch 1


def test_a_reply_blocks_a_pending_approval(sends):
    from app import approval

    store = get_store()
    store.record_failure(invoice_id="in_ap", customer_id="cust_001", amount_minor=4900, currency="eur",
                         failure_code="card_expired", attempt_count=1, stripe_customer_id="cus_X",
                         subscription_id="sub_X", holdout=False)
    import hashlib
    ok = {"invoice_id": "in_ap", "subject": "Your card on file has expired", "body": "b",
          "body_sha256": hashlib.sha256(b"b").hexdigest()}
    assert approval._recheck_before_sending(ok, store) is None
    store.mark_customer_replied("in_ap")
    assert approval._recheck_before_sending(ok, store) == "customer_replied"


def test_reply_route_sets_and_clears_the_hold(sends, monkeypatch):
    from fastapi.testclient import TestClient

    from app import api as api_module

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    store = get_store()
    loop.handle_payment_failed(_failed("in_route", 1), store=store)
    with TestClient(api_module.app) as client:
        r = client.post("/invoices/in_route/reply", json={"replied": True})
        assert r.status_code == 200 and r.json()["customer_replied"] is True
        assert client.post("/invoices/in_route/reply", json={"replied": "yes"}).status_code == 400
        assert client.post("/invoices/nope/reply", json={"replied": True}).status_code == 404
        r = client.post("/invoices/in_route/reply", json={"replied": False})
        assert r.json()["customer_replied"] is False


# ---------------------------------------------------------------------------------------------
# Mailbox reply watcher
# ---------------------------------------------------------------------------------------------

def _msg(body, *, frm="cust@example.test", subject="Re: Your card on file has expired", hours=1, headers=None):
    from datetime import UTC, datetime, timedelta

    from app.mailbox_facts import Message

    return Message(message_id=f"m{hours}{body[:3]}", from_addr=frm, to_addrs=("billing@co.test",),
                   sent=datetime.now(UTC) + timedelta(hours=hours), subject=subject, body=body,
                   headers=headers or {})


def test_every_dunning_email_carries_the_invoice_reference(sends):
    loop.handle_payment_failed(_failed("in_1NqXyZ2eZvKYlo2C8k3JfLtD", 1), store=get_store())
    assert len(sends) == 1, "the realistic invoice id must pass the output guards"
    assert sends[0]["body"].rstrip().endswith("Reference: in_1NqXyZ2eZvKYlo2C8k3JfLtD")


def test_a_real_reply_sets_the_hold_and_an_auto_reply_does_not(sends):
    from app import reply_watch

    store = get_store()
    loop.handle_payment_failed(_failed("in_w1", 1), store=store)
    ours = {"billing@co.test"}
    ooo = _msg("I am away.\n> Reference: in_w1", subject="Automatic reply: Your card",
               headers={"auto-submitted": "auto-replied"})
    assert reply_watch.scan([ooo], store, ours)["held"] == 0 and not store.customer_replied("in_w1")
    reply = _msg("Can I pay another way?\n> Reference: in_w1")
    assert reply_watch.scan([reply], store, ours)["held"] == 1 and store.customer_replied("in_w1")
    assert reply_watch.scan([reply], store, ours)["already"] == 1


def test_clearing_the_hold_is_not_undone_by_rereading_the_old_reply(sends, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app import reply_watch
    from app import store as store_module

    store = get_store()
    loop.handle_payment_failed(_failed("in_w2", 1), store=store)
    ours = {"billing@co.test"}
    reply = _msg("Please call me\n> Reference: in_w2", hours=1)          # arrives at T+1h
    assert reply_watch.scan([reply], store, ours)["held"] == 1
    # A person clears it at T+2h (the ledger clock is pinned so the timeline is deterministic).
    monkeypatch.setattr(store_module, "_now",
                        lambda: (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds"))
    store.clear_customer_reply("in_w2", by="person")
    assert reply_watch.scan([reply], store, ours)["held"] == 0           # the old reply is not re-read as new
    assert store.customer_replied("in_w2") is False
    later = _msg("Another question\n> Reference: in_w2", hours=3)         # genuinely newer mail holds again
    assert reply_watch.scan([later], store, ours)["held"] == 1


def test_mail_older_than_our_first_send_and_unrelated_mail_do_nothing(sends):
    from app import reply_watch

    store = get_store()
    loop.handle_payment_failed(_failed("in_w3", 1), store=store)
    ours = {"billing@co.test"}
    old = _msg("Reference: in_w3", hours=-48)
    other = _msg("Lunch on Friday?")
    res = reply_watch.scan([old, other], store, ours)
    assert res["held"] == 0 and res["unmatched"] == 1 and not store.customer_replied("in_w3")


def test_the_watch_script_refuses_to_read_an_incomplete_mailbox_as_silence(sends):
    import io
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import watch_replies

    from app.mailbox_imap import ReadResult

    store = get_store()
    loop.handle_payment_failed(_failed("in_w4", 1), store=store)
    bad = lambda since: ReadResult([_msg("Reference: in_w4")], (), ("INBOX",), 0, False)  # noqa: E731
    out = io.StringIO()
    assert watch_replies.main(["--ours", "billing@co.test"], out=out, reader=bad, store=store) == 2
    assert not store.customer_replied("in_w4") and "nothing concluded" in out.getvalue()
    good = lambda since: ReadResult([_msg("Reference: in_w4")], ("INBOX",), (), 0, False)  # noqa: E731
    out = io.StringIO()
    assert watch_replies.main(["--ours", "billing@co.test"], out=out, reader=good, store=store) == 0
    assert store.customer_replied("in_w4") and "replies held: 1" in out.getvalue()


def test_the_in_app_watch_runs_from_the_sweep_is_off_by_default_and_is_throttled(sends, monkeypatch):
    from app import approval, reply_watch
    from app.mailbox_imap import ReadResult

    store = get_store()
    loop.handle_payment_failed(_failed("in_auto", 1), store=store)
    reply_watch._last_run.clear()
    monkeypatch.delenv(reply_watch.WATCH_ENV, raising=False)
    assert approval.sweep(store)["replies"] is None            # off unless a person turns it on

    calls = []

    def reader(since):
        calls.append(since)
        return ReadResult([_msg("Reference: in_auto")], ("INBOX",), (), 0, False)

    cfg_from = "billing@co.test"
    monkeypatch.setattr("app.tenant_config.TenantConfig.from_address", cfg_from, raising=False)
    out = reply_watch.run_once(store, reader=reader)
    assert out["complete"] is True and out["held"] == 1 and store.customer_replied("in_auto")
    assert reply_watch.run_once(store, reader=reader) is None   # inside the interval: no second read
    assert len(calls) == 1


def test_an_unreadable_mailbox_never_stops_the_sweep_or_sets_a_hold(sends):
    from app import reply_watch
    from app.mailbox_imap import ReadResult

    store = get_store()
    loop.handle_payment_failed(_failed("in_down", 1), store=store)
    reply_watch._last_run.clear()
    bad = lambda since: ReadResult([_msg("Reference: in_down")], (), ("INBOX",), 0, False)  # noqa: E731
    assert reply_watch.run_once(store, reader=bad) == {"complete": False}
    reply_watch._last_run.clear()

    def boom(since):
        raise OSError("down")

    assert reply_watch.run_once(store, reader=boom) == {"complete": False}
    assert not store.customer_replied("in_down")


def test_a_reply_that_lost_the_reference_is_matched_by_its_sender_and_our_subject(sends, monkeypatch):
    from app import reply_watch

    monkeypatch.setenv("PAYPILOT_CUSTOMER_SALT", "test-salt-1234")
    store = get_store()
    loop.handle_payment_failed(_failed("in_s1", 1), store=store)   # emails cust@example.test
    ours = {"billing@co.test"}
    # no quoted Reference, our subject: held, and no address is stored anywhere
    reply = _msg("Can you call me?", frm="Cust <cust@example.test>", subject="Re: Your card on file has expired")
    assert reply_watch.scan([reply], store, ours)["held"] == 1 and store.customer_replied("in_s1")
    stored = " ".join(str(m.get("error")) for m in store.messages_for("in_s1"))
    assert "example.test" not in stored


def test_sender_matching_ignores_unrelated_mail_other_senders_and_a_missing_salt(sends, monkeypatch):
    from app import reply_watch

    store = get_store()
    ours = {"billing@co.test"}
    monkeypatch.setenv("PAYPILOT_CUSTOMER_SALT", "test-salt-1234")
    loop.handle_payment_failed(_failed("in_s2", 1), store=store)
    unrelated = _msg("Lunch?", frm="cust@example.test", subject="Lunch on Friday")
    stranger = _msg("Hi", frm="someone@else.test", subject="Re: Your card on file has expired")
    assert reply_watch.scan([unrelated, stranger], store, ours)["held"] == 0
    monkeypatch.delenv("PAYPILOT_CUSTOMER_SALT")           # salt off: reference-only, no crash
    reply = _msg("call me", frm="cust@example.test", subject="Re: Your card on file has expired")
    assert reply_watch.scan([reply], store, ours)["held"] == 0


def test_multi_tenant_watch_uses_only_the_tenants_own_credentials(monkeypatch):
    from types import SimpleNamespace as NS

    from app import imap_drafts, reply_watch
    from app.store import get_store

    store = get_store()
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    blank = dict(gmail_client_id="", gmail_client_secret="", gmail_read_refresh_token="",
                 ms_tenant="", ms_client_id="", ms_client_secret="", ms_refresh_token="")
    cfg = {"v": NS(**blank)}
    monkeypatch.setattr("app.tenant_config.config_of", lambda st: cfg["v"])
    monkeypatch.setattr(imap_drafts, "configured", lambda c=None: False)

    monkeypatch.setenv("PAYPILOT_REPLY_WATCH", "tenant")
    assert reply_watch.tenant_provider(store) is None                 # no credentials of its own: not watched
    cfg["v"] = NS(**{**blank, "gmail_client_id": "i", "gmail_client_secret": "s", "gmail_read_refresh_token": "r"})
    assert reply_watch.tenant_provider(store) == "gmail"
    cfg["v"] = NS(**{**blank, "ms_tenant": "t", "ms_client_id": "i", "ms_client_secret": "s", "ms_refresh_token": "r"})
    assert reply_watch.tenant_provider(store) == "graph"
    monkeypatch.setenv("PAYPILOT_REPLY_WATCH", "gmail")                 # pinned provider: a graph-only tenant is skipped
    assert reply_watch.tenant_provider(store) is None
    monkeypatch.setattr(imap_drafts, "configured", lambda c=None: True)
    monkeypatch.setenv("PAYPILOT_REPLY_WATCH", "imap")
    assert reply_watch.tenant_provider(store) == "imap"
    monkeypatch.setenv("PAYPILOT_TENANCY", "single")                    # single mode: the environment's provider
    assert reply_watch.tenant_provider(store) == "imap"
    monkeypatch.setenv("PAYPILOT_REPLY_WATCH", "tenant")                # "tenant" means nothing in single mode
    assert reply_watch.tenant_provider(store) is None


def test_the_reader_prefers_the_tenants_own_token_over_the_environment(monkeypatch):
    from types import SimpleNamespace as NS

    from app import mailbox_gmail, mailbox_reader

    seen = {}
    monkeypatch.setattr(mailbox_gmail, "access_token", lambda post, cid, sec, ref: seen.update(cid=cid, ref=ref) or "tok")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "env-id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "env-sec")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "env-ref")
    mailbox_reader.reader_for("gmail", config=NS(gmail_client_id="t-id", gmail_client_secret="t-sec",
                                                 gmail_read_refresh_token="t-ref"))
    assert seen == {"cid": "t-id", "ref": "t-ref"}
    mailbox_reader.reader_for("gmail")
    assert seen == {"cid": "env-id", "ref": "env-ref"}


def test_dry_run_reports_without_writing_and_the_review_sheet_lists_every_stage(sends):
    import io
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import render_followup_review
    import watch_replies

    from app.mailbox_imap import ReadResult

    store = get_store()
    loop.handle_payment_failed(_failed("in_dry", 1), store=store)
    good = lambda since: ReadResult([_msg("Reference: in_dry")], ("INBOX",), (), 0, False)  # noqa: E731
    out = io.StringIO()
    assert watch_replies.main(["--ours", "billing@co.test", "--dry-run"], out=out, reader=good, store=store) == 0
    assert "DRY RUN" in out.getvalue() and "would hold: 1" in out.getvalue()
    assert store.customer_replied("in_dry") is False

    sheet = io.StringIO()
    render_followup_review.main(sheet)
    text = sheet.getvalue()
    assert text.count("--- FIRST") == text.count("--- REMINDER") == text.count("--- FINAL") == 8
    assert "{name}" not in text and "{plan}" not in text and "{business}" not in text


def test_the_imap_probe_names_the_failing_stage_without_a_secret():
    from app import mailbox_reader

    def refused():
        raise OSError("[AUTHENTICATIONFAILED] Invalid credentials secret-pw-123")

    msg = mailbox_reader.imap_probe(connect=lambda: refused())
    assert "APP PASSWORD" in msg and "secret-pw-123" not in msg

    def down():
        raise OSError("Connection refused")

    assert "could not connect" in mailbox_reader.imap_probe(connect=down)
