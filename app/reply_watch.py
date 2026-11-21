# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Turn a mailbox into the reply hold: a customer who answers a dunning email stops the sequence.

No network and no clock here: an adapter (Gmail, Graph, IMAP) reads the mailbox and hands the
messages in. See scripts/watch_replies.py.

* NOTHING IS STORED ABOUT THE SENDER. Every dunning email carries ``Reference: <invoice id>``; a
  reply that quotes it is tied to the invoice, so PayPilot never needs to keep a customer's address.
* ONLY A REAL REPLY HOLDS. Bounces, out-of-office and other auto-replies are classified by the same
  rules as the invoice facts (:func:`app.mailbox_facts.classify`) and never set the hold: an away
  message is not somebody answering.
* ONLY MAIL AFTER OUR FIRST SEND, and only after the hold was last cleared, so a person releasing
  the hold is not undone by the next scan re-reading the reply they already dealt with.
* A MAILBOX THAT COULD NOT BE READ FULLY IS REPORTED, NOT TREATED AS "NO REPLIES". The caller gets
  ``complete`` and must not read an incomplete scan as silence.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, date, datetime, timedelta

from app.mailbox_facts import Message, _addr, _mentions, classify
from app.store import TERMINAL_STATES


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return _aware(datetime.fromisoformat(iso))
    except ValueError:
        return None


SALT_ENV = "PAYPILOT_CUSTOMER_SALT"


def sender_ref(address: str) -> str | None:
    """Salted hash of an address, or None when no salt is set (sender matching is then off).

    The same construction as the invoice facts' customer reference, so one salt covers both.
    """
    from app.invoice_source import InvoiceError, make_customer_ref

    try:
        return make_customer_ref(_addr(address), os.environ.get(SALT_ENV, ""))
    except InvoiceError:
        return None


def record_recipient(store, invoice_id: str, recipient: str) -> None:
    """Remember WHO we emailed, as a hash, so a reply without the Reference line still matches."""
    ref = sender_ref(recipient)
    if ref:
        store.record_recipient_ref(invoice_id, ref)


def _our_subjects() -> list[str]:
    from app import templates

    doc = templates.load()
    out = [v.lower() for k, sec in doc.items() if k.startswith("subject") and isinstance(sec, dict)
           for v in sec.values()]
    out += [v.lower() for k, v in doc.get("fallback", {}).items() if k.startswith("subject")]
    return out


def _strip_reply_prefix(subject: str) -> str:
    s = (subject or "").strip().lower()
    while True:
        for p in ("re:", "aw:", "sv:", "fw:", "fwd:"):
            if s.startswith(p):
                s = s[len(p):].strip()
                break
        else:
            return s


def scan(messages: list[Message], store, our_addresses: set[str]) -> dict:
    """Set the reply hold for every open, already-emailed invoice a real reply refers to.

    Returns counts only: ``held`` (new holds), ``already`` (hold already on), ``ignored`` (not a
    reply, or older than our first send or the last clearing), ``unmatched`` (names no open invoice).
    """
    ours = frozenset(_addr(a) for a in our_addresses)
    open_ids = [f["invoice_id"] for f in store.list_failures()
                if f["state"] not in TERMINAL_STATES and store.sent_message_count(f["invoice_id"]) > 0]
    subjects = set(_our_subjects())
    held = already = ignored = unmatched = 0
    for msg in sorted(messages, key=lambda m: m.sent):
        text = f"{msg.subject}\n{msg.body}"
        hit = [i for i in open_ids if _mentions(text, i)]
        if not hit and subjects and classify(msg, ours) == "inbound":
            # A reply that lost the quoted Reference line: tie it by sender, but only when the
            # subject is one of OUR dunning subjects, so unrelated mail from the same person is not
            # read as an answer.
            ref = sender_ref(msg.from_addr)
            if ref and _strip_reply_prefix(msg.subject) in subjects:
                owned = store.invoices_for_recipient_ref(ref)
                hit = [i for i in open_ids if i in owned]
        if not hit:
            unmatched += 1
            continue
        if classify(msg, ours) != "inbound":
            ignored += 1
            continue
        # The ledger stores whole seconds; compare at that grain or mail in the same second as a
        # clearing reads as newer than it.
        when = _aware(msg.sent).replace(microsecond=0)
        for invoice_id in hit:
            floor = max(
                (t for t in (_parse(store.first_sent_at(invoice_id)),
                             _parse(store.last_reply_event_at(invoice_id))) if t is not None),
                default=None,
            )
            if floor is not None and when <= floor:
                ignored += 1
                continue
            if store.customer_replied(invoice_id):
                already += 1
                continue
            store.mark_customer_replied(invoice_id, by="mailbox")
            held += 1
    return {"held": held, "already": already, "ignored": ignored, "unmatched": unmatched}


# ---------------------------------------------------------------------------------------------
# In-app, automatic. Off unless a person turns it on: reading a mailbox is a data-processing
# decision, so it is opt-in and says which provider.
# ---------------------------------------------------------------------------------------------

_log = logging.getLogger("paypilot.reply_watch")
WATCH_ENV = "PAYPILOT_REPLY_WATCH"                  # imap | gmail | graph
INTERVAL_ENV = "PAYPILOT_REPLY_WATCH_MINUTES"       # default 10; the sweep ticks every minute
WINDOW_DAYS = 14
_last_run: dict[str, float] = {}


def watch_provider() -> str | None:
    """The platform opt-in: imap | gmail | graph (single mode reads that provider from the
    environment), or ``tenant`` (multi mode: each tenant is read through whichever mailbox
    credentials IT holds). Anything else is off."""
    from app.mailbox_reader import PROVIDERS

    value = (os.environ.get(WATCH_ENV) or "").strip().lower()
    return value if value in PROVIDERS or value == "tenant" else None


def _interval_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get(INTERVAL_ENV) or 10)) * 60
    except ValueError:
        return 600.0


def tenant_provider(store) -> str | None:
    """The mailbox provider to read for this store, or None when it is not watched.

    Single mode: the environment's provider. Multi mode: only from the tenant's OWN stored
    credentials (IMAP settings, a read-only Gmail token, or Microsoft Graph secrets), never from the
    platform environment, so one tenant's mailbox is never read with another's credentials. ``imap``,
    ``gmail`` or ``graph`` pins the provider; ``tenant`` takes the first one the tenant has.
    """
    from app import tenant_config
    from app.imap_drafts import configured as imap_configured
    from app.store import tenancy_mode

    mode = watch_provider()
    if mode is None:
        return None
    if tenancy_mode() != "multi":
        return None if mode == "tenant" else mode
    try:
        cfg = tenant_config.config_of(store)
        has = {
            "imap": bool(imap_configured(cfg)),
            "gmail": bool(cfg.gmail_client_id and cfg.gmail_client_secret and cfg.gmail_read_refresh_token),
            "graph": bool(cfg.ms_tenant and cfg.ms_client_id and cfg.ms_client_secret and cfg.ms_refresh_token),
        }
    except Exception:  # noqa: BLE001 - an unknown or suspended tenant is simply not watched
        return None
    order = ("imap", "gmail", "graph") if mode == "tenant" else (mode,)
    return next((p for p in order if has.get(p)), None)


def tenant_can_watch(store) -> bool:
    return tenant_provider(store) is not None


def run_once(store, *, reader=None, monotonic=time.monotonic) -> dict | None:
    """One throttled, read-only scan of the configured mailbox. Never raises.

    ``None`` means it did not run (off, or inside the interval). A scan that could not read the whole
    mailbox concludes NOTHING and is logged, never read as "no replies".
    """
    from app import tenant_config

    if reader is None and tenant_provider(store) is None:
        return None
    key = getattr(store, "tenant_id", "") or ""
    now = monotonic()
    if key in _last_run and now - _last_run[key] < _interval_seconds():
        return None
    _last_run[key] = now
    try:
        cfg = tenant_config.config_of(store)
        if reader is None:
            from app.mailbox_reader import reader_for

            reader = reader_for(tenant_provider(store), ("INBOX",), config=cfg)
        result = reader(date.today() - timedelta(days=WINDOW_DAYS))
        if not result.complete:
            _log.error(json.dumps({"event": "reply_watch_incomplete", "failed": list(result.folders_failed),
                                   "truncated": result.truncated}))
            return {"complete": False}
        counts = scan(result.messages, store, {cfg.from_address})
        if counts["held"]:
            _log.info(json.dumps({"event": "reply_hold_set", **counts}))
        return {"complete": True, **counts}
    except Exception as exc:  # noqa: BLE001 - a mailbox outage must never stop the sweep
        _log.error(json.dumps({"event": "reply_watch_failed", "error": type(exc).__name__}))
        return {"complete": False}
