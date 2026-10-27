# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""IMAP drafts: the approval step for Proton Mail (via Bridge) and any IMAP mailbox.

Proton Mail has no public mail API. Its documented route for other software is
Proton Mail Bridge, "a desktop application that runs in the background" that
"set[s] up a local mail server" on the user's own computer, available "only with
a paid plan", with "a separate password from your login password" (proton.me/mail/bridge,
read 2026-10-27). So this adapter speaks plain IMAP to whatever host and port it
is given, and the Bridge case is just a host that happens to be the user's own
machine.

WHAT THAT MEANS FOR A HOSTED PAYPILOT. A cloud deployment cannot reach a Bridge
running on a merchant's laptop. This mode is therefore for PayPilot running on a
machine that can: the merchant's own computer, or a server that also runs
Bridge. A hosted merchant on Proton uses ``email`` mode instead, which needs no
mailbox access at all. The Proton Bridge port numbers and connection security
are NOT given in the pages read, so there are no defaults for them here: you set
the host and port Bridge shows in its own window.

The same adapter serves a normal IMAP mailbox, including Gmail over IMAP with an
app password, which avoids Google's restricted-scope verification. App passwords
"can only be used with accounts that have 2-Step Verification turned on" and may
not be offered on "a work, school, or another organization account"
(support.google.com/accounts/answer/185833), and Google says they "aren't
recommended". Gmail folder names differ (``[Gmail]/Drafts``, ``[Gmail]/Sent
Mail``); set them in the folder variables.

NOT VERIFIED against a live server, and said so rather than hoped:
whether the mail client the person sends from keeps the ``X-PayPilot-Approval``
header (if not, matching falls back to recipient, subject and time and the
approval records ``imap:subject``); whether the server's HEADER search is a
substring match (assumed, and every hit is re-checked for an exact marker).
"""

from __future__ import annotations

import base64
import imaplib
import ssl
import time
from email import policy
from email.parser import BytesParser

from app.mailbox_approval import HEADER, MailboxError

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FETCH_FIELDS = "SUBJECT TO MESSAGE-ID X-PAYPILOT-APPROVAL"
_MAX_SENT_SCAN = 50
TIMEOUT = 15


class ImapError(MailboxError):
    pass


# Literal reads, one per variable, so scripts/env_surface.py sees them.
def configured(config=None) -> bool:
    """Host, port, user and password are all required. Nothing is guessed."""
    from app.tenant_config import current_config

    cfg = config or current_config()
    return bool(cfg.imap_host and cfg.imap_port and cfg.imap_user and cfg.imap_password)


def _quote(value: str) -> str:
    """An IMAP quoted string, refusing anything that could end it early."""
    if not value or not value.isascii() or any(c in value for c in '"\\\r\n\x00'):
        raise ImapError("unsafe_value")
    return f'"{value}"'


def _imap_date(epoch: float) -> str:
    t = time.gmtime(epoch)
    return f"{t.tm_mday:02d}-{_MONTHS[t.tm_mon - 1]}-{t.tm_year}"


class ImapClient:
    """The four calls the approval core needs, over short-lived IMAP connections."""

    def __init__(self, connect=None, config=None) -> None:
        self._config = config
        if connect is None and not configured(config):
            raise ImapError("imap_not_configured")
        self._connect = connect or self._real_connect

    def _cfg(self):
        from app.tenant_config import current_config

        return self._config or current_config()

    def _real_connect(self):
        cfg = self._cfg()
        host, port, security = cfg.imap_host, cfg.imap_port, cfg.imap_security
        if security not in ("ssl", "starttls"):
            # Never fall back to plaintext on a typo: credentials go over this.
            raise ImapError("security_must_be_ssl_or_starttls")
        ctx = ssl.create_default_context()
        if host in LOOPBACK:
            # Bridge presents a self-signed certificate on the user's own
            # machine. A loopback connection cannot be intercepted from outside
            # the machine, so verification is skipped there and ONLY there.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if security == "ssl":
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=TIMEOUT)
        else:
            conn = imaplib.IMAP4(host, port, timeout=TIMEOUT)
            conn.starttls(ssl_context=ctx)
        typ, _ = conn.login(cfg.imap_user, cfg.imap_password)
        if typ != "OK":
            raise ImapError("login_refused")
        return conn

    def _session(self, folder: str, *, readonly: bool):
        conn = self._connect()
        try:
            typ, _ = conn.select(_quote(folder), readonly=readonly)
        except Exception:
            self._close(conn)
            raise
        if typ != "OK":
            self._close(conn)
            raise ImapError("select_failed")
        return conn

    @staticmethod
    def _close(conn) -> None:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - closing must never mask the real error
            pass

    @staticmethod
    def _headers(conn, uid: bytes) -> dict | None:
        typ, data = conn.uid("FETCH", uid, f"(INTERNALDATE BODY.PEEK[HEADER.FIELDS ({_FETCH_FIELDS})])")
        if typ != "OK":
            raise ImapError("fetch_failed")
        for item in data:
            if isinstance(item, tuple):
                meta, raw = item
                parsed = BytesParser(policy=policy.default).parsebytes(raw)
                tup = imaplib.Internaldate2tuple(meta)
                return {
                    "epoch": time.mktime(tup) if tup else 0.0,
                    "subject": str(parsed["Subject"] or ""),
                    "to": str(parsed["To"] or ""),
                    "message_id": str(parsed["Message-ID"] or "").strip(),
                    "marker": str(parsed[HEADER] or "").strip(),
                    "uid": uid.decode("ascii", "replace"),
                }
        return None

    def _find_marker(self, conn, marker: str) -> list[bytes]:
        """UIDs of messages whose marker header is EXACTLY ``marker``."""
        typ, data = conn.uid("SEARCH", None, "HEADER", HEADER, _quote(marker))
        if typ != "OK":
            raise ImapError("search_failed")
        out = []
        for uid in (data[0] or b"").split():
            h = self._headers(conn, uid)
            if h and h["marker"] == marker:
                out.append(uid)
        return out

    # -- the four calls ---------------------------------------------------

    def create_draft(self, raw_b64url: str) -> str:
        raw = base64.urlsafe_b64decode(raw_b64url.encode("ascii"))
        marker = str(BytesParser(policy=policy.default).parsebytes(raw)[HEADER] or "").strip()
        if not marker:
            raise ImapError("draft_has_no_marker")
        raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        conn = self._connect()
        try:
            typ, _ = conn.append(_quote(self._cfg().imap_drafts_folder), "\\Draft",
                                 imaplib.Time2Internaldate(time.time()), raw)
        finally:
            self._close(conn)
        if typ != "OK":
            raise ImapError("append_failed")
        return marker

    def draft_exists(self, marker: str) -> bool:
        conn = self._session(self._cfg().imap_drafts_folder, readonly=True)
        try:
            return bool(self._find_marker(conn, marker))
        finally:
            self._close(conn)

    def delete_draft(self, marker: str) -> None:
        conn = self._session(self._cfg().imap_drafts_folder, readonly=False)
        try:
            for uid in self._find_marker(conn, marker):
                conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            conn.expunge()
        finally:
            self._close(conn)

    def sent_since(self, *, to: str, after_epoch: int) -> list[dict]:
        conn = self._session(self._cfg().imap_sent_folder, readonly=True)
        try:
            # SINCE has day granularity, so ask for a day early and filter on
            # the exact INTERNALDATE below.
            typ, data = conn.uid("SEARCH", None, "SINCE", _imap_date(after_epoch - 86400),
                                 "TO", _quote(to))
            if typ != "OK":
                raise ImapError("search_failed")
            out = []
            for uid in (data[0] or b"").split()[-_MAX_SENT_SCAN:]:
                h = self._headers(conn, uid)
                if not h or h["epoch"] < after_epoch:
                    continue
                out.append({"id": h["message_id"] or h["uid"], "subject": h["subject"],
                            "to": h["to"], "marker": h["marker"]})
            return out
        finally:
            self._close(conn)


_client: ImapClient | None = None


def get_client(config=None) -> ImapClient:
    """A client for this tenant. A test may inject one with :func:`set_client`.

    Not cached per process, for the same reason as the Gmail client.
    """
    if _client is not None:
        return _client
    return ImapClient(config=config)


def set_client(client: ImapClient | None) -> None:
    global _client
    _client = client


def request_draft(*, invoice_id: str, recipient: str, subject: str, body: str, store,
                  config=None) -> dict:
    from app import approval as ap
    from app.mailbox_approval import open_draft
    from app.tenant_config import config_of

    config = config or config_of(store)
    if not configured(config):
        return ap._record_refusal(store, invoice_id, "imap_not_configured")
    try:
        client = get_client(config)
    except ImapError:
        return ap._record_refusal(store, invoice_id, "imap_not_configured")
    return open_draft(channel="imap", client=client, invoice_id=invoice_id,
                      recipient=recipient, subject=subject, body=body, store=store,
                      where="mailbox")


def reconcile(store, client: ImapClient | None = None) -> dict:
    from app.mailbox_approval import reconcile as core
    from app.tenant_config import config_of

    config = config_of(store)
    if client is None and not configured(config):
        return {"checked": 0, "error": "imap_not_configured"}
    return core(store, client or get_client(config), "imap")
