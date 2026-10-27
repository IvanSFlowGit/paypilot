"""IMAP drafts (Proton Mail via Bridge, or any IMAP mailbox). No network.

A small in-memory IMAP server stands in for the mailbox. It answers the same
command shapes ``imaplib`` sends (UID SEARCH, UID FETCH, UID STORE, APPEND,
EXPUNGE), so the client's search building, header parsing, timestamp filtering
and quoting are exercised for real. What is NOT proven here is how a real
server (Proton Bridge, Dovecot, Gmail) answers them; docs/approval.md lists
that as a human-run step.
"""

from __future__ import annotations

import email
import imaplib
import time
from datetime import UTC, datetime, timedelta

import pytest

from app import approval, imap_drafts, loop, stripe_client
from app.imap_drafts import ImapClient, ImapError
from app.mailbox_approval import marker_for
from app.store import STATE_FAILED, STATE_MESSAGED

PORTAL = "https://billing.stripe.com/p/session/test_abc123"
CUSTOMER = "me@mine.test"
APPROVER = "owner@merchant.test"


def _event(invoice_id="in_1"):
    return {
        "id": f"evt_{invoice_id}", "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": "cus_XYZ",
            "subscription": "sub_XYZ", "customer_email": CUSTOMER, "amount_due": 4900,
            "currency": "eur", "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


class FakeServer:
    """Folders of messages: {folder: [{uid, flags, raw, epoch}]}."""

    def __init__(self):
        self.folders = {"Drafts": [], "Sent": []}
        self.next_uid = 1
        self.commands: list[str] = []

    def add(self, folder, raw: bytes, epoch=None, flags=()):
        uid = self.next_uid
        self.next_uid += 1
        self.folders[folder].append(
            {"uid": uid, "flags": set(flags), "raw": raw, "epoch": epoch or time.time()})
        return uid

    def connect(self):
        return FakeConn(self)


class FakeConn:
    def __init__(self, server):
        self.s = server
        self.folder = None
        self.closed = False

    def select(self, name, readonly=False):
        self.s.commands.append(f"SELECT {name}")
        bare = name.strip('"')
        if bare not in self.s.folders:
            return "NO", [b"no such mailbox"]
        self.folder = bare
        return "OK", [b"1"]

    def append(self, folder, flags, date, raw):
        self.s.commands.append(f"APPEND {folder}")
        bare = folder.strip('"')
        if bare not in self.s.folders:
            return "NO", [b"no such mailbox"]
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "IMAP wants CRLF"
        self.s.add(bare, raw, flags=("\\Draft",))
        return "OK", [b"done"]

    def expunge(self):
        msgs = self.s.folders[self.folder]
        self.s.folders[self.folder] = [m for m in msgs if "\\Deleted" not in m["flags"]]
        return "OK", [b""]

    def logout(self):
        self.closed = True
        return "BYE", [b""]

    @staticmethod
    def _hdr(raw, name):
        m = email.message_from_bytes(raw)
        return str(m[name] or "")

    def uid(self, cmd, *args):
        self.s.commands.append(f"UID {cmd} " + " ".join(str(a) for a in args))
        msgs = self.s.folders[self.folder]
        if cmd == "SEARCH":
            crit = list(args[1:])
            hits = []
            for m in msgs:
                ok, i = True, 0
                while i < len(crit):
                    k = crit[i]
                    if k == "HEADER":
                        want = crit[i + 2].strip('"')
                        ok &= want in self._hdr(m["raw"], crit[i + 1])
                        i += 3
                    elif k == "TO":
                        ok &= crit[i + 1].strip('"').lower() in self._hdr(m["raw"], "To").lower()
                        i += 2
                    elif k == "SINCE":
                        d = datetime.strptime(crit[i + 1], "%d-%b-%Y").replace(tzinfo=UTC)
                        ok &= m["epoch"] >= d.timestamp()
                        i += 2
                    else:
                        raise AssertionError(f"unsupported criterion {k}")
                if ok:
                    hits.append(str(m["uid"]).encode())
            return "OK", [b" ".join(hits)]
        if cmd == "FETCH":
            uid = int(args[0])
            m = next(x for x in msgs if x["uid"] == uid)
            parsed = email.message_from_bytes(m["raw"])
            lines = []
            for f in ("Subject", "To", "Message-ID", "X-PayPilot-Approval"):
                if parsed[f] is not None:
                    lines.append(f"{f}: {parsed[f]}")
            blob = ("\r\n".join(lines) + "\r\n\r\n").encode()
            when = datetime.fromtimestamp(m["epoch"], UTC).strftime("%d-%b-%Y %H:%M:%S +0000")
            meta = f'{uid} (UID {uid} INTERNALDATE "{when}" BODY[HEADER.FIELDS (X)] {{{len(blob)}}}'.encode()
            return "OK", [(meta, blob), b")"]
        if cmd == "STORE":
            uid = int(args[0])
            for m in msgs:
                if m["uid"] == uid:
                    m["flags"].add("\\Deleted")
            return "OK", [b""]
        raise AssertionError(f"unsupported command {cmd}")


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def imap(server, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "imap")
    monkeypatch.setenv("PAYPILOT_SEND_EMAIL", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("PAYPILOT_ALLOWED_RECIPIENTS", CUSTOMER)
    monkeypatch.setenv("PAYPILOT_APPROVER_EMAILS", APPROVER)
    monkeypatch.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "0")
    monkeypatch.setenv("PAYPILOT_IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("PAYPILOT_IMAP_PORT", "1143")
    monkeypatch.setenv("PAYPILOT_IMAP_USER", "owner@pm.test")
    monkeypatch.setenv("PAYPILOT_IMAP_PASSWORD", "bridge-pass")
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid, *_a, **_k: PORTAL)
    resend = []

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_ok"}

    monkeypatch.setattr("httpx.post", lambda url, json=None, **k: (resend.append(json), _Resp())[1])
    imap_drafts.set_client(ImapClient(connect=server.connect))
    yield server, resend
    imap_drafts.set_client(None)


def _customer_mail(resend):
    return [p for p in resend if CUSTOMER in p["to"]]


def _raw_sent(row, *, keep_header=True, subject=None, to=None):
    m = email.message.EmailMessage()
    m["To"] = to or CUSTOMER
    m["Subject"] = subject or row["subject"]
    if keep_header:
        m["X-PayPilot-Approval"] = marker_for(row["id"], row["body_sha256"])
    m.set_content(row["body"])
    return m.as_bytes()


# -- configuration -----------------------------------------------------------


def test_nothing_is_guessed_when_unconfigured(monkeypatch):
    for k in ("PAYPILOT_IMAP_HOST", "PAYPILOT_IMAP_PORT", "PAYPILOT_IMAP_USER",
              "PAYPILOT_IMAP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert not imap_drafts.configured()
    with pytest.raises(ImapError):
        ImapClient()


def test_every_required_setting_is_required(monkeypatch):
    base = {"PAYPILOT_IMAP_HOST": "h", "PAYPILOT_IMAP_PORT": "993",
            "PAYPILOT_IMAP_USER": "u", "PAYPILOT_IMAP_PASSWORD": "p"}
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    assert imap_drafts.configured()
    for drop in base:
        monkeypatch.delenv(drop)
        assert not imap_drafts.configured(), drop
        monkeypatch.setenv(drop, base[drop])
    monkeypatch.setenv("PAYPILOT_IMAP_PORT", "abc")
    assert not imap_drafts.configured()


def test_an_unknown_security_value_never_falls_back_to_plaintext(monkeypatch):
    monkeypatch.setenv("PAYPILOT_IMAP_HOST", "h")
    monkeypatch.setenv("PAYPILOT_IMAP_PORT", "143")
    monkeypatch.setenv("PAYPILOT_IMAP_USER", "u")
    monkeypatch.setenv("PAYPILOT_IMAP_PASSWORD", "p")
    for bad in ("none", "plain", "tls", "off", "starttl"):
        monkeypatch.setenv("PAYPILOT_IMAP_SECURITY", bad)
        with pytest.raises(ImapError) as exc:
            ImapClient()._real_connect()
        assert str(exc.value) == "security_must_be_ssl_or_starttls"


# -- creating the draft ------------------------------------------------------


def test_a_failure_puts_a_draft_in_the_mailbox_and_sends_nothing(imap, isolated_store):
    server, resend = imap
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["status"] == "pending_approval"
    assert len(server.folders["Drafts"]) == 1
    assert _customer_mail(resend) == []
    assert isolated_store.get_failure("in_1")["state"] == STATE_FAILED
    row = isolated_store.approvals_for("in_1")[0]
    assert row["channel"] == "imap"
    assert row["external_id"] == marker_for(row["id"], row["body_sha256"])


def test_the_draft_is_the_exact_text_marker_and_flagged_as_a_draft(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    stored = server.folders["Drafts"][0]
    msg = email.message_from_bytes(stored["raw"])

    assert "\\Draft" in stored["flags"]
    assert msg["To"] == CUSTOMER and msg["Subject"] == row["subject"]
    assert msg["X-PayPilot-Approval"] == marker_for(row["id"], row["body_sha256"])
    assert msg.get_payload(decode=True).decode().replace("\r\n", "\n").strip() == row["body"].strip()


def test_not_configured_fails_closed(imap, isolated_store, monkeypatch):
    server, resend = imap
    monkeypatch.delenv("PAYPILOT_IMAP_PASSWORD")
    imap_drafts.set_client(None)
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["error"] == "imap_not_configured"
    assert server.folders["Drafts"] == [] and _customer_mail(resend) == []


def test_a_missing_drafts_folder_fails_closed(imap, isolated_store, monkeypatch):
    server, resend = imap
    monkeypatch.setenv("PAYPILOT_IMAP_DRAFTS_FOLDER", "Brouillons")
    result = loop.handle_payment_failed(_event(), isolated_store)

    assert result["delivery"]["error"] == "imap_draft_create_failed"
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_FAILED
    assert _customer_mail(resend) == []


def test_the_folder_names_are_configurable_for_gmail_imap(imap, isolated_store, monkeypatch):
    server, _ = imap
    server.folders["[Gmail]/Drafts"] = []
    monkeypatch.setenv("PAYPILOT_IMAP_DRAFTS_FOLDER", "[Gmail]/Drafts")
    loop.handle_payment_failed(_event(), isolated_store)

    assert len(server.folders["[Gmail]/Drafts"]) == 1 and server.folders["Drafts"] == []


# -- reconciling -------------------------------------------------------------


def test_a_draft_still_in_drafts_is_waiting(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    out = imap_drafts.reconcile(isolated_store)

    assert out["waiting"] == 1
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_a_sent_message_with_the_marker_settles_the_invoice(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    server.folders["Drafts"].clear()
    server.add("Sent", _raw_sent(row))

    out = imap_drafts.reconcile(isolated_store)

    assert out["sent"] == 1
    done = isolated_store.approvals_for("in_1")[0]
    assert done["status"] == approval.A_SENT and done["resolved_by"] == "imap:header"
    assert isolated_store.get_failure("in_1")["state"] == STATE_MESSAGED
    assert isolated_store.sent_message_count("in_1") == 1


def test_if_the_client_dropped_the_header_subject_and_recipient_still_match(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    server.folders["Drafts"].clear()
    server.add("Sent", _raw_sent(row, keep_header=False))

    imap_drafts.reconcile(isolated_store)

    assert isolated_store.approvals_for("in_1")[0]["resolved_by"] == "imap:subject"


def test_an_older_message_with_the_same_subject_is_not_a_match(imap, isolated_store):
    """The failure code fixes the subject, so last month's identical mail to
    the same customer must not settle today's request."""
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    server.folders["Drafts"].clear()
    server.add("Sent", _raw_sent(row, keep_header=False), epoch=time.time() - 86400 * 30)

    out = imap_drafts.reconcile(isolated_store)

    assert out["sent"] == 0 and out["waiting"] == 1
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_a_message_to_someone_else_is_not_a_match(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    server.folders["Drafts"].clear()
    server.add("Sent", _raw_sent(row, keep_header=False, to="other@else.test"))

    assert imap_drafts.reconcile(isolated_store)["sent"] == 0


def test_a_marker_that_only_contains_ours_is_not_a_match(imap, isolated_store):
    """IMAP HEADER search is a substring match. '17:...' contains '7:...'."""
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    server.folders["Drafts"].clear()
    other = _raw_sent(row).replace(
        marker_for(row["id"], row["body_sha256"]).encode(),
        b"1" + marker_for(row["id"], row["body_sha256"]).encode())
    server.add("Sent", other)
    # Subject and recipient also match, so remove them to isolate the marker.
    server.folders["Sent"][0]["raw"] = server.folders["Sent"][0]["raw"].replace(
        row["subject"].encode(), b"Something else entirely")

    assert imap_drafts.reconcile(isolated_store)["sent"] == 0


def test_a_vanished_draft_is_held_through_the_grace_period_then_skipped(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    server.folders["Drafts"].clear()

    assert imap_drafts.reconcile(isolated_store)["waiting"] == 1
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    isolated_store._conn.execute("UPDATE approvals SET gone_since = ?", (old,))
    isolated_store._conn.commit()
    out = imap_drafts.reconcile(isolated_store)

    assert out["skipped"] == 1
    assert isolated_store.approvals_for("in_1")[0]["resolved_by"] == "imap:draft_deleted"
    assert isolated_store.sent_message_count("in_1") == 0


def test_an_expired_request_removes_its_draft(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    isolated_store._conn.execute("UPDATE approvals SET expires_at = ?", (past,))
    isolated_store._conn.commit()

    imap_drafts.reconcile(isolated_store)

    assert server.folders["Drafts"] == [], "a stale draft must not be sendable"
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_EXPIRED


def test_the_gmail_channel_is_not_reconciled_by_the_imap_pass(imap, isolated_store):
    loop.handle_payment_failed(_event(), isolated_store)
    isolated_store._conn.execute("UPDATE approvals SET channel = 'gmail'")
    isolated_store._conn.commit()

    assert imap_drafts.reconcile(isolated_store)["checked"] == 0


def test_a_server_error_is_counted_and_never_marks_anything_sent(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)

    def broken():
        raise imaplib.IMAP4.error("server fell over")

    imap_drafts.set_client(ImapClient(connect=broken))
    out = imap_drafts.reconcile(isolated_store)

    assert out["errors"] == 1
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_the_sweep_runs_the_imap_pass_only_in_imap_mode(imap, isolated_store, monkeypatch):
    loop.handle_payment_failed(_event(), isolated_store)
    assert approval.sweep(isolated_store)["imap"]["checked"] == 1
    monkeypatch.setenv("PAYPILOT_APPROVAL_MODE", "email")
    assert approval.sweep(isolated_store)["imap"] == {}


# -- injection ---------------------------------------------------------------


@pytest.mark.parametrize("evil", ['a@b.test" OR ALL', 'a@b.test\r\nA1 DELETE Drafts',
                                  "a@b.test\\", "a@b.tést", ""])
def test_a_hostile_recipient_never_reaches_the_server_command(imap, evil):
    server, _ = imap
    before = len(server.commands)
    with pytest.raises(ImapError):
        ImapClient(connect=server.connect).sent_since(to=evil, after_epoch=0)
    assert not any("DELETE" in c for c in server.commands[before:])


def test_a_hostile_folder_name_is_refused(imap, monkeypatch):
    server, _ = imap
    monkeypatch.setenv("PAYPILOT_IMAP_SENT_FOLDER", 'Sent"\r\nA1 DELETE Drafts')
    with pytest.raises(ImapError):
        ImapClient(connect=server.connect).sent_since(to=CUSTOMER, after_epoch=0)


def test_the_connection_is_always_closed(imap, isolated_store):
    server, _ = imap
    conns = []
    real = server.connect

    def tracking():
        c = real()
        conns.append(c)
        return c

    imap_drafts.set_client(ImapClient(connect=tracking))
    loop.handle_payment_failed(_event(), isolated_store)
    imap_drafts.reconcile(isolated_store)

    assert conns and all(c.closed for c in conns)


# -- TLS: verification is skipped on loopback and nowhere else ----------------


class _Seen(Exception):
    pass


def _capture_ctx(monkeypatch, host, security="ssl"):
    seen = {}

    def fake_ssl(h, p, ssl_context=None, timeout=None):
        seen["ctx"] = ssl_context
        raise _Seen

    class FakePlain:
        def __init__(self, h, p, timeout=None):
            pass

        def starttls(self, ssl_context=None):
            seen["ctx"] = ssl_context
            raise _Seen

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    monkeypatch.setattr(imaplib, "IMAP4", FakePlain)
    monkeypatch.setenv("PAYPILOT_IMAP_HOST", host)
    monkeypatch.setenv("PAYPILOT_IMAP_PORT", "993")
    monkeypatch.setenv("PAYPILOT_IMAP_USER", "u")
    monkeypatch.setenv("PAYPILOT_IMAP_PASSWORD", "p")
    monkeypatch.setenv("PAYPILOT_IMAP_SECURITY", security)
    with pytest.raises(_Seen):
        ImapClient()._real_connect()
    return seen["ctx"]


@pytest.mark.parametrize("security", ["ssl", "starttls"])
def test_a_remote_server_is_always_verified(monkeypatch, security):
    import ssl

    ctx = _capture_ctx(monkeypatch, "imap.example.test", security)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
@pytest.mark.parametrize("security", ["ssl", "starttls"])
def test_loopback_accepts_the_bridge_self_signed_certificate(monkeypatch, host, security):
    import ssl

    ctx = _capture_ctx(monkeypatch, host, security)
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False


def test_a_lookalike_of_loopback_is_not_trusted(monkeypatch):
    import ssl

    for host in ("127.0.0.1.evil.test", "localhost.evil.test", "0.0.0.0"):
        ctx = _capture_ctx(monkeypatch, host)
        assert ctx.verify_mode == ssl.CERT_REQUIRED, host


# -- gaps the first mutation run exposed --------------------------------------


def test_draft_exists_matches_the_marker_exactly_not_as_a_substring(imap, isolated_store):
    """HEADER search is a substring match on a real server. If our draft was
    deleted but another draft's marker CONTAINS ours, we must not read that as
    'still waiting', or a refused request would sit pending forever."""
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    ours = marker_for(row["id"], row["body_sha256"])
    server.folders["Drafts"].clear()
    longer = _raw_sent(row).replace(ours.encode(), b"9" + ours.encode())
    server.add("Drafts", longer, flags=("\\Draft",))

    assert ImapClient(connect=server.connect).draft_exists(ours) is False
    assert len(server.folders["Drafts"]) == 1, "and the other draft is left alone"


def test_a_delete_removes_only_the_exact_marker(imap, isolated_store):
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    ours = marker_for(row["id"], row["body_sha256"])
    longer = _raw_sent(row).replace(ours.encode(), b"9" + ours.encode())
    server.add("Drafts", longer, flags=("\\Draft",))

    ImapClient(connect=server.connect).delete_draft(ours)

    left = [email.message_from_bytes(m["raw"])["X-PayPilot-Approval"] for m in server.folders["Drafts"]]
    assert left == ["9" + ours]


def test_a_message_sent_before_the_request_is_filtered_by_exact_time(imap, isolated_store):
    """Sent two hours ago is inside the server's day-granular SINCE window but
    before this request existed, so it cannot be the answer to it."""
    server, _ = imap
    loop.handle_payment_failed(_event(), isolated_store)
    row = isolated_store.approvals_for("in_1")[0]
    server.folders["Drafts"].clear()
    server.add("Sent", _raw_sent(row), epoch=time.time() - 7200)

    out = imap_drafts.reconcile(isolated_store)

    assert out["sent"] == 0
    assert isolated_store.approvals_for("in_1")[0]["status"] == approval.A_PENDING


def test_a_draft_without_the_marker_is_refused_before_it_is_stored(imap):
    server, _ = imap
    import base64

    m = email.message.EmailMessage()
    m["To"] = CUSTOMER
    m["Subject"] = "no marker"
    m.set_content("x")
    raw = base64.urlsafe_b64encode(m.as_bytes()).decode()

    with pytest.raises(ImapError):
        ImapClient(connect=server.connect).create_draft(raw)
    assert server.folders["Drafts"] == []
