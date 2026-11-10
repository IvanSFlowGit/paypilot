"""The read-only IMAP reader. A fake server records EVERY command so 'read-only' is asserted, not claimed.

Synthetic mail only; no live server was available (see the module docstring)."""

from __future__ import annotations

from datetime import date
from email.message import EmailMessage

from app.mailbox_facts import build_facts
from app.mailbox_imap import read_messages

SINCE = date(2026, 11, 1)
ALLOWED = {"SELECT", "UID SEARCH", "UID FETCH", "LOGOUT"}


def mail(
    subject,
    body,
    frm="ap@client.example",
    date_hdr="Wed, 04 Nov 2026 10:00:00 +0000",
    extra=None,
    mid="<a@x>",
):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = (
        frm,
        "billing@us.example",
        subject,
        date_hdr,
        mid,
    )
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes()


class Server:
    def __init__(self, folders, broken=()):
        self.folders, self.broken, self.log, self.selected = folders, set(broken), [], None

    def conn(self):
        return Conn(self)


class Conn:
    def __init__(self, s):
        self.s = s

    def select(self, folder, readonly=False):
        self.s.log.append(("SELECT", folder, readonly))
        name = folder.strip('"')
        if name in self.s.broken or name not in self.s.folders:
            return "NO", [b""]
        self.s.selected = name
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        self.s.log.append(("UID " + cmd, args))
        msgs = self.s.folders[self.s.selected]
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(msgs)))]
        i = int(args[0]) - 1
        return "OK", [(b"1 (BODY[] {n}", msgs[i]), b")"]

    def logout(self):
        self.s.log.append(("LOGOUT",))
        return "BYE", []


def run(folders, **kw):
    s = Server(folders, kw.pop("broken", ()))
    return read_messages(s.conn, tuple(folders), SINCE, **kw), s


def test_only_read_commands_are_ever_issued_and_every_select_is_read_only():
    r, s = run({"INBOX": [mail("INV-1042", "ok")]})
    cmds = {e[0] for e in s.log}
    assert cmds <= ALLOWED and all(e[2] is True for e in s.log if e[0] == "SELECT")
    assert not any(c in cmds for c in ("UID STORE", "UID COPY", "APPEND", "EXPUNGE", "UID EXPUNGE"))


def test_fetch_uses_body_peek_so_nothing_is_marked_read():
    _, s = run({"INBOX": [mail("x", "y")]})
    fetches = [e for e in s.log if e[0] == "UID FETCH"]
    assert fetches and all("BODY.PEEK" in e[1][1] for e in fetches)


def test_fetch_is_a_bounded_partial_read_never_the_whole_message():
    from app.mailbox_imap import FETCH_CAP

    _, s = run({"INBOX": [mail("x", "y")]})
    fetches = [e for e in s.log if e[0] == "UID FETCH"]
    assert (
        fetches
        and all(e[1][1] == f"(BODY.PEEK[]<0.{FETCH_CAP}>)" for e in fetches)
        and FETCH_CAP <= 1_000_000
    )


def test_a_truncated_message_still_classifies_from_its_headers():
    raw = mail("Re: INV-1042", "x" * 5000, extra={"Auto-Submitted": "auto-replied"})[:900]
    r, _ = run({"INBOX": [raw]})
    assert len(r.messages) == 1 and r.messages[0].headers["auto-submitted"] == "auto-replied"


def test_a_message_is_parsed_into_the_core_type_and_feeds_build_facts():
    r, _ = run({"INBOX": [mail("Re: INV-1042", "We will pay on 2026-11-14.")]})
    assert r.complete and len(r.messages) == 1 and r.messages[0].from_addr == "ap@client.example"
    from app.invoice_source import InvoiceRecord

    rec = InvoiceRecord(
        source="csv",
        source_id="s",
        invoice_no="INV-1042",
        customer_ref="c" * 16,
        amount_minor=1,
        currency="usd",
        status="open",
    )
    out, dropped = build_facts(r.messages, [rec], {"billing@us.example"}, date(2026, 11, 10))
    assert (
        out["INV-1042"].facts.promised_by == date(2026, 11, 14)
        and out["INV-1042"].facts.replies_received == 1
    )


def test_dsn_and_auto_reply_headers_survive_into_the_message():
    r, _ = run(
        {
            "INBOX": [
                mail(
                    "x",
                    "y",
                    extra={"Auto-Submitted": "auto-replied", "X-Failed-Recipients": "a@b.example"},
                )
            ]
        }
    )
    h = r.messages[0].headers
    assert h["auto-submitted"] == "auto-replied" and h["x-failed-recipients"] == "a@b.example"


def test_a_folder_that_cannot_be_read_makes_the_result_incomplete_not_empty():
    r, _ = run({"INBOX": [mail("INV-1042", "ok")], "Sent": []}, broken=("Sent",))
    assert (
        r.complete is False
        and r.folders_failed == ("Sent",)
        and r.folders_ok == ("INBOX",)
        and len(r.messages) == 1
    )


def test_an_unknown_folder_is_a_failure_not_a_silent_zero():
    s = Server({"INBOX": []})
    r = read_messages(s.conn, ("Nope",), SINCE)
    assert r.complete is False and r.folders_failed == ("Nope",)


def test_the_cap_marks_the_result_truncated():
    r, _ = run({"INBOX": [mail("a", "b", mid=f"<{i}@x>") for i in range(5)]}, limit=3)
    assert len(r.messages) == 3 and r.truncated is True and r.complete is False


def test_a_message_with_no_usable_date_is_skipped_and_counted():
    r, _ = run({"INBOX": [mail("a", "b", date_hdr="not a date"), mail("c", "d")]})
    assert r.skipped_unparseable == 1 and len(r.messages) == 1


def test_old_mail_the_server_returns_is_filtered_by_the_real_date():
    r, _ = run(
        {"INBOX": [mail("old", "b", date_hdr="Wed, 01 Jul 2026 10:00:00 +0000"), mail("new", "b")]}
    )
    assert [m.subject for m in r.messages] == ["new"]


def test_a_connect_failure_is_a_failed_folder_with_no_content_in_the_result():
    def boom():
        raise OSError("connection refused: password hunter2")

    r = read_messages(boom, ("INBOX",), SINCE)
    assert r.complete is False and r.folders_failed == ("INBOX",) and "hunter2" not in repr(r)
