# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Turn a mailbox's messages into the facts the invoice rules table reads. No network, no clock.

DESIGN DRAFT: docs/invoice-sources.md section 3. This is the provider-agnostic core: an adapter
(Gmail, IMAP, Proton Bridge) fetches messages and hands them here as :class:`Message`. Nothing in
this module reads a mailbox, sends, labels or modifies anything.

* MATCH FIRST, STORE SECOND. A message is kept only if it names a known invoice number. Everything
  else is counted in ``dropped`` and discarded here; its text is never returned or logged.
* FACTS BEFORE LABELS. Chases sent, replies received, bounces, auto-replies and a promised-by date are
  extracted by rules. ``disputed`` is NEVER set here: only a model may propose it, and it maps to
  "hand to a person" (see invoice_rules). A reply that sounds angry is still just a reply.
* A SILENT MAILBOX IS NOT A QUIET DEBTOR. When the feed shows no recent mail at all, replies are
  ``None`` (untracked), never ``0``: zero replies from an unreachable mailbox would read as "nobody
  answered" and push an invoice toward escalation on the strength of our own outage.
* A PROMISED-BY DATE needs an explicit date (ISO or ``14 Nov 2026``) in a sentence about paying, in a
  reply from the other side, in the future of that message. Anything vaguer yields no date, because
  ``next week`` guessed into a date is a confident wrong one.

Synthetic data only in the tests; no real mailbox has been read by this code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.invoice_rules import InvoiceFacts
from app.invoice_source import InvoiceRecord, make_customer_ref
from app.pii import scrub_freeform

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DMY = re.compile(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(20\d{2})\b", re.I)
_PAYWORD = re.compile(r"\b(pay|paid|payment|transfer|remit|settle|wire)\w*", re.I)
_OOO_SUBJECT = re.compile(r"^\s*(automatic reply|auto[- ]?reply|out of office|abwesenheit|absence)", re.I)
_BOUNCE_FROM = re.compile(r"^(mailer-daemon|postmaster)@", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
# Phone-shaped only: a leading +, (nnn) groups, nnn-nnn-nnnn groups, or a bare 10-15 digit run not glued to a
# letter or hyphen. A looser pattern masked "INV-1042 2500.00" as a phone number and destroyed the reference.
_PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d|\(\d{2,4}\)[\s.-]?\d{3}[\s.-]?\d{3,4}|\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b|(?<![A-Za-z0-9-])\d{10,15}\b")
FEED_QUIET_DAYS = 14


@dataclass(frozen=True)
class Message:
    message_id: str
    from_addr: str
    to_addrs: tuple[str, ...]
    sent: datetime
    subject: str
    body: str
    headers: dict = field(default_factory=dict)  # lower-case keys


@dataclass(frozen=True)
class InvoiceMailFacts:
    invoice_no: str
    facts: InvoiceFacts
    bounced: bool
    auto_replies: int
    evidence: tuple[str, ...]  # message ids only, never content


def _addr(value: str) -> str:
    m = _EMAIL.search(value or "")
    return (m.group(0) if m else (value or "")).strip().lower()


def classify(msg: Message, our_addresses: frozenset[str]) -> str:
    """bounce | auto_reply | ours | inbound. First match wins; order is asserted by tests."""
    frm = _addr(msg.from_addr)
    h = msg.headers
    ctype = (h.get("content-type") or "").lower()
    if _BOUNCE_FROM.match(frm) or "multipart/report" in ctype or (h.get("x-failed-recipients") or "").strip():
        return "bounce"
    auto = (h.get("auto-submitted") or "").strip().lower()
    if (auto and auto != "no") or _OOO_SUBJECT.search(msg.subject or "") or (h.get("precedence") or "").lower() in ("bulk", "auto_reply", "junk"):
        return "auto_reply"
    if frm in our_addresses:
        return "ours"
    return "inbound"


def _mentions(text: str, invoice_no: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(invoice_no) + r"(?![A-Za-z0-9])", text, re.I) is not None


def promised_by(body: str, sent: date) -> date | None:
    """An explicit future date in a sentence about paying; the latest such date, or None."""
    best: date | None = None
    for sentence in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", body or ""):
        if not _PAYWORD.search(sentence):
            continue
        found: list[date] = []
        for m in _ISO.finditer(sentence):
            found.append(_safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        for m in _DMY.finditer(sentence):
            found.append(_safe_date(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]], int(m.group(1))))
        for d in found:
            if d is not None and sent <= d <= sent + timedelta(days=120) and (best is None or d > best):
                best = d
    return best


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def feed_alive(messages: list[Message], today: date) -> bool:
    """False when there is no mail at all in the last FEED_QUIET_DAYS: the feed, not the debtor, is quiet."""
    return any((today - m.sent.date()).days <= FEED_QUIET_DAYS for m in messages)


def build_facts(
    messages: list[Message],
    records: list[InvoiceRecord],
    our_addresses: set[str],
    today: date,
) -> tuple[dict[str, InvoiceMailFacts], int]:
    """Returns ``({invoice_no: InvoiceMailFacts}, dropped)``. ``dropped`` counts messages naming no known invoice."""
    ours = frozenset(_addr(a) for a in our_addresses)
    alive = feed_alive(messages, today)
    per: dict[str, dict] = {r.invoice_no: {"chases": 0, "replies": 0, "bounced": False, "auto": 0, "promise": None,
                                           "promise_msg_date": None, "ids": []} for r in records}
    dropped = 0
    for msg in sorted(messages, key=lambda m: m.sent):
        text = f"{msg.subject}\n{msg.body}"
        hit = [r.invoice_no for r in records if _mentions(text, r.invoice_no)]
        if not hit:
            dropped += 1
            continue
        kind = classify(msg, ours)
        for no in hit:
            p = per[no]
            p["ids"].append(msg.message_id)
            if kind == "bounce":
                p["bounced"] = True
            elif kind == "auto_reply":
                p["auto"] += 1
            elif kind == "ours":
                p["chases"] += 1
            else:
                p["replies"] += 1
                d = promised_by(msg.body, msg.sent.date())
                if d is not None and (p["promise_msg_date"] is None or msg.sent.date() >= p["promise_msg_date"]):
                    p["promise"], p["promise_msg_date"] = d, msg.sent.date()
    out = {
        no: InvoiceMailFacts(
            invoice_no=no,
            facts=InvoiceFacts(chases_sent=p["chases"], replies_received=p["replies"] if alive else None,
                               promised_by=p["promise"]),
            bounced=p["bounced"], auto_replies=p["auto"], evidence=tuple(p["ids"]))
        for no, p in per.items()
    }
    return out, dropped


def mask_for_model(text: str, known_names: tuple[str, ...] = ()) -> str:
    """Placeholder-mask what a model must not see: cards, addresses, IBANs, phones and any known name."""
    t = scrub_freeform(text or "")
    t = _EMAIL.sub("{{EMAIL}}", t)
    t = _IBAN.sub("{{IBAN}}", t)
    t = _PHONE.sub("{{PHONE}}", t)
    for n in sorted((n for n in known_names if n and len(n) > 1), key=len, reverse=True):
        t = re.sub(re.escape(n), "{{NAME}}", t, flags=re.I)
    return t


def customer_ref_for(addr: str, salt: str) -> str:
    """The same salted reference the CSV path uses, so a mailbox sender can be matched to a customer without a name."""
    return make_customer_ref(_addr(addr), salt)
