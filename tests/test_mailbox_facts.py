"""The mailbox core: matching, classification, promised-by, silent feeds, masking. SYNTHETIC emails only."""

from __future__ import annotations

from datetime import date, datetime

from app.invoice_source import InvoiceRecord
from app.mailbox_facts import (
    Message, build_facts, classify, customer_ref_for, feed_alive, mask_for_model, promised_by,
)

US = {"billing@us.example"}
TODAY = date(2026, 11, 10)


def rec(no):
    return InvoiceRecord(source="csv", source_id="s" + no, invoice_no=no, customer_ref="c" * 16, amount_minor=250000,
                         currency="usd", status="open", due=date(2026, 9, 1))


def msg(i, frm, subject, body, day=5, headers=None):
    return Message(message_id=f"m{i}", from_addr=frm, to_addrs=("x@y.example",), sent=datetime(2026, 11, day, 10, 0),
                   subject=subject, body=body, headers=headers or {})


def run(messages, nos=("INV-1042",)):
    return build_facts(messages, [rec(n) for n in nos], US, TODAY)


def test_a_chase_and_a_reply_are_counted_against_the_named_invoice():
    out, dropped = run([msg(1, "billing@us.example", "Invoice INV-1042", "Reminder"), msg(2, "ap@client.example", "Re: INV-1042", "Received, in hand.")])
    f = out["INV-1042"].facts
    assert (f.chases_sent, f.replies_received, dropped) == (1, 1, 0) and out["INV-1042"].evidence == ("m1", "m2")


def test_a_message_naming_no_known_invoice_is_dropped_and_not_returned():
    out, dropped = run([msg(1, "ap@client.example", "Lunch?", "Friday?"), msg(2, "ap@client.example", "re INV-1042", "ok")])
    assert dropped == 1 and out["INV-1042"].evidence == ("m2",)


def test_an_invoice_number_matches_as_a_whole_token_only():
    out, dropped = run([msg(1, "ap@client.example", "INV-10420 and INV-1042x", "ok")])
    assert dropped == 1 and out["INV-1042"].facts.replies_received == 0


def test_one_message_naming_two_invoices_counts_for_both():
    out, _ = run([msg(1, "ap@client.example", "INV-1 and INV-2", "paid both")], nos=("INV-1", "INV-2"))
    assert out["INV-1"].facts.replies_received == 1 and out["INV-2"].facts.replies_received == 1


def test_classification_order_bounce_then_auto_reply_then_ours_then_inbound():
    assert classify(msg(1, "MAILER-DAEMON@x.example", "INV-1042", ""), frozenset(US)) == "bounce"
    assert classify(msg(1, "a@b.example", "x", "", headers={"x-failed-recipients": "a@b.example"}), frozenset(US)) == "bounce"
    assert classify(msg(1, "a@b.example", "Out of office: INV-1042", ""), frozenset(US)) == "auto_reply"
    assert classify(msg(1, "a@b.example", "x", "", headers={"auto-submitted": "auto-replied"}), frozenset(US)) == "auto_reply"
    assert classify(msg(1, "billing@us.example", "Out of office", ""), frozenset(US)) == "auto_reply"
    assert classify(msg(1, "billing@us.example", "x", ""), frozenset(US)) == "ours"
    assert classify(msg(1, "a@b.example", "x", "", headers={"auto-submitted": "no"}), frozenset(US)) == "inbound"


def test_a_bounce_and_an_auto_reply_are_not_replies():
    out, _ = run([msg(1, "MAILER-DAEMON@x.example", "Undelivered INV-1042", ""), msg(2, "a@b.example", "Automatic reply: INV-1042", "away")])
    f = out["INV-1042"]
    assert f.bounced and f.auto_replies == 1 and f.facts.replies_received == 0


def test_a_display_name_form_of_our_address_still_counts_as_ours():
    out, _ = run([msg(1, "Billing Team <billing@us.example>", "INV-1042", "second reminder")])
    assert out["INV-1042"].facts.chases_sent == 1


def test_disputed_is_never_set_by_rules_however_angry_the_reply():
    out, _ = run([msg(1, "ap@client.example", "INV-1042", "I dispute this invoice entirely, it is wrong.")])
    assert out["INV-1042"].facts.disputed is False


def test_a_dead_feed_makes_replies_untracked_not_zero():
    old = Message("m1", "billing@us.example", ("x@y",), datetime(2026, 9, 1, 9, 0), "INV-1042", "chase", {})
    out, _ = build_facts([old], [rec("INV-1042")], US, TODAY)
    assert out["INV-1042"].facts.replies_received is None and out["INV-1042"].facts.chases_sent == 1
    assert feed_alive([old], TODAY) is False and feed_alive([msg(1, "a@b", "x", "")], TODAY) is True
    assert build_facts([], [rec("INV-1042")], US, TODAY)[0]["INV-1042"].facts.replies_received is None


def test_promised_by_takes_an_explicit_future_date_in_a_paying_sentence():
    s = date(2026, 11, 5)
    assert promised_by("We will pay on 2026-11-14.", s) == date(2026, 11, 14)
    assert promised_by("Payment will be made by 14 Nov 2026.", s) == date(2026, 11, 14)
    assert promised_by("Transfer scheduled 20 Nov. 2026", s) == date(2026, 11, 20)


def test_promised_by_refuses_vague_past_far_future_and_non_paying_sentences():
    s = date(2026, 11, 5)
    assert promised_by("We will pay next week.", s) is None
    assert promised_by("We will pay on 2026-10-01.", s) is None
    assert promised_by("We will pay on 2027-06-01.", s) is None
    assert promised_by("Meeting is on 2026-11-14.", s) is None
    assert promised_by("We will pay on 2026-02-30.", s) is None
    assert promised_by("", s) is None


def test_promised_by_takes_the_latest_date_and_only_from_the_other_side():
    out, _ = run([msg(1, "billing@us.example", "INV-1042", "Please pay by 2026-11-20."),
                  msg(2, "ap@client.example", "INV-1042", "We will pay on 2026-11-12. Correction: payment 2026-11-18.", day=6)])
    assert out["INV-1042"].facts.promised_by == date(2026, 11, 18)


def test_a_later_reply_without_a_date_does_not_erase_an_earlier_promise():
    out, _ = run([msg(1, "ap@client.example", "INV-1042", "We will pay on 2026-11-14.", day=5),
                  msg(2, "ap@client.example", "INV-1042", "Thanks, chasing internally.", day=7)])
    assert out["INV-1042"].facts.promised_by == date(2026, 11, 14)


def test_mask_removes_addresses_ibans_phones_cards_and_known_names():
    t = mask_for_model("Hi Dana Reyes, write to dana@client.example or +1 415 555 0134, IBAN DE89370400440532013000, card 4111 1111 1111 1111.", ("Dana Reyes",))
    for leak in ("dana@client.example", "415 555", "DE89370400", "4111", "Dana Reyes"):
        assert leak not in t
    assert "{{EMAIL}}" in t and "{{IBAN}}" in t and "{{NAME}}" in t


def test_mask_leaves_an_invoice_number_and_an_amount_alone():
    assert "INV-1042" in mask_for_model("Please settle INV-1042, 2500.00 USD") and "2500.00" in mask_for_model("INV-1042 2500.00")


def test_customer_ref_is_stable_salted_and_case_insensitive():
    a = customer_ref_for("AP@Client.example", "salt-12345")
    assert a == customer_ref_for("ap@client.example", "salt-12345") and a != customer_ref_for("ap@client.example", "other-salt-1")
