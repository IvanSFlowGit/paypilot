# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Read-only IMAP reader that yields :class:`app.mailbox_facts.Message` objects.

One adapter serves Proton Mail (through Proton Bridge's local IMAP), Gmail (IMAP with an app
password, which avoids Google's restricted-scope verification) and any other IMAP mailbox. The
connection and its TLS rules are ``app.imap_drafts``: this module only reads through it.

WHAT IS ENFORCED, AND TESTED BY RECORDING EVERY COMMAND THE FAKE SERVER SEES:
  * folders are SELECTed read-only;
  * messages are fetched with ``BODY.PEEK[]`` so nothing is marked read;
  * the only commands issued are SELECT, UID SEARCH, UID FETCH and LOGOUT. There is no STORE, COPY,
    APPEND, EXPUNGE, no flag changes and no sending, so a bug here cannot alter a mailbox.
  * a folder that cannot be read makes the result ``complete=False``. A partial read is never returned
    as if it were whole, because a missing folder would read as "nobody replied".

NOT VERIFIED against a live server (none was available): Gmail's IMAP folder names differ
(``[Gmail]/All Mail``), Bridge's port and security are whatever Bridge shows, and SINCE is date-only
on the server so the cap and the date filter here do the real bounding.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date
from email.utils import parsedate_to_datetime

from app.mailbox_facts import Message

BODY_CAP = 20_000
_KEEP_HEADERS = ("content-type", "auto-submitted", "precedence", "x-failed-recipients")
_TAGS = re.compile(r"<[^>]+>")
_MONTH = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class ImapReadError(Exception):
    """A read failed in a way the caller must see. Never carries message content or credentials."""


@dataclass(frozen=True)
class ReadResult:
    messages: list[Message]
    folders_ok: tuple[str, ...]
    folders_failed: tuple[str, ...]
    skipped_unparseable: int
    truncated: bool

    @property
    def complete(self) -> bool:
        return not self.folders_failed and not self.truncated


def _since(d: date) -> str:
    return f"{d.day:02d}-{_MONTH[d.month - 1]}-{d.year}"


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _text_body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # noqa: BLE001 - an undecodable part is an empty body, not a crash
        return ""
    if part.get_content_type() == "text/html":
        text = _TAGS.sub(" ", text)
    return text[:BODY_CAP]


def _parse(raw: bytes, folder: str, uid: str) -> Message | None:
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        sent = parsedate_to_datetime(str(msg["date"]))
    except Exception:  # noqa: BLE001
        return None
    if sent.tzinfo is not None:
        sent = sent.astimezone(UTC).replace(tzinfo=None)
    mid = (
        str(msg["message-id"] or "").strip()
        or "uid-" + hashlib.sha256(f"{folder}:{uid}".encode()).hexdigest()[:16]
    )
    headers = {k: str(msg[k]) for k in _KEEP_HEADERS if msg[k] is not None}
    tos = tuple(str(a) for a in (msg.get_all("to") or []))
    return Message(
        mid, str(msg["from"] or ""), tos, sent, str(msg["subject"] or ""), _text_body(msg), headers
    )


def read_messages(
    connect: Callable[[], object], folders: tuple[str, ...], since: date, limit: int = 500
) -> ReadResult:
    """Read messages newer than ``since`` from each folder, read-only, up to ``limit`` in total."""
    out: list[Message] = []
    ok: list[str] = []
    failed: list[str] = []
    skipped = 0
    truncated = False
    for folder in folders:
        conn = None
        try:
            conn = connect()
            typ, _ = conn.select(_quote(folder), readonly=True)
            if typ != "OK":
                raise ImapReadError("select_failed")
            typ, data = conn.uid("SEARCH", None, "SINCE", _since(since))
            if typ != "OK":
                raise ImapReadError("search_failed")
            uids = (data[0] or b"").split()
            for uid in uids:
                if len(out) >= limit:
                    truncated = True
                    break
                typ, fetched = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    skipped += 1
                    continue
                m = _parse(fetched[0][1], folder, uid.decode("ascii", "replace"))
                if m is None:
                    skipped += 1
                elif m.sent.date() >= since:
                    out.append(m)
            ok.append(folder)
        except Exception:  # noqa: BLE001 - any failure marks this folder failed, never half-trusted
            failed.append(folder)
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001
                    pass
    return ReadResult(out, tuple(ok), tuple(failed), skipped, truncated)


def connect_from_config(config=None) -> Callable[[], object]:
    """The real connection, from the same IMAP settings the draft approval uses."""
    from app.imap_drafts import ImapMailbox

    return ImapMailbox(config=config)._connect
