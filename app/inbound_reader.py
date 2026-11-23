# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Read what a customer wrote back about an invoice, and draft a reply a person can approve.

"I have already paid, but you sent me another reminder" is the commonest answer to a
chase and today it only stops the sequence. This reads it: an intent, the facts the
customer stated, and, where a bank feed is connected, whether a matching credit is
actually there. Then it drafts one reply in the business's name.

Pure and deterministic. No model, no network, no clock (``today`` is passed in), no write.
The draft is composed from fixed sentences filled with fields the CALLER supplies (a name
from their own records, the invoice number, the amount, the due date) and with the few
facts this module extracts (a date, an amount). The customer's own words are never copied
into the reply, so nothing in a message can steer it.

Rules that are the point of the module:

* A reply NEVER says a payment was received unless a bank feed was checked and a credit
  carrying the invoice number was found. "I have paid" is a claim. Nothing here marks an
  invoice paid; that stays with the ledger.
* A pending credit is reported as pending, never as received.
* The quoted original is stripped before anything is matched. Our own footer says
  "reply STOP and I will remove you", and it sits inside every reply that quotes us.
* An opt-out outranks everything and is never argued with.
* Anything mixed, unclear, non-English, a dispute, a cancellation, a shortfall, a failed
  payment or an unchecked claim is routed to a person, with a holding reply that admits
  nothing and promises only that a person will reply.
* Every draft is a draft. ``person_must_act`` says whether a person has to do more than
  approve it. Sending is a separate, approved step in ``app.approval``.

Limits, stated rather than implied: English only; the phrase lists are starting sets
written from common wording, not from a corpus of this client's mail, so a reply that
matches nothing returns ``unclear`` and goes to a person rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.in_flight import HOLD, RELEASE, UNKNOWN, InFlightVerdict
from app.money import exponent

INTENTS = (
    "opt_out", "hardship", "dispute", "repairs", "cancel", "already_paid", "benefits",
    "update_details", "extension", "unclear",
)
ITEM_LABELS = ("invoice", "rent", "service charge")
QUALIFIERS = ("still_reminded", "no_receipt", "payment_failed", "wrong_amount")
CHECKS = (
    "unchecked", "feed_unreachable", "found_full_pending", "found_full_booked", "found_part",
    "not_found", "expired", "currency_mismatch",
)

MAX_CHARS = 4000

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

_OPT_OUT = re.compile(
    r"\b(unsubscribe|stop\s+(?:emailing|contacting|sending|messaging|writing)|remove\s+me|"
    r"do\s+not\s+(?:contact|email|write)|don'?t\s+(?:contact|email|write))\b", re.I)
_HARDSHIP = re.compile(
    r"\b(can(?:'?t|not)\s+(?:afford|pay)|unable\s+to\s+pay|struggl\w+|lost\s+(?:my|our)\s+job|"
    r"redundan\w+|unemploy\w+|laid\s+off|financial\s+(?:difficult\w+|hardship|trouble)|"
    r"in\s+hospital|illness|sick|bereave\w+|passed\s+away|funeral|mental\s+health|homeless|"
    r"no\s+money|debt\s+advice)\b", re.I)
_REPAIRS = re.compile(
    r"\b(repair\w*|damp|mould|mold|leak\w*|boiler|no\s+(?:hot\s+water|heating)|infest\w+|"
    r"disrepair|not\s+been\s+fixed|withhold\w*\s+(?:the\s+)?rent)\b", re.I)
_BENEFITS = re.compile(
    r"\b(universal\s+credit|housing\s+(?:benefit|allowance|payment)|benefits?\s+(?:claim|payment)|"
    r"waiting\s+for\s+(?:my\s+)?(?:benefit|claim)|council\s+(?:will|is\s+going\s+to)\s+pay)\b", re.I)
_DISPUTE = re.compile(
    r"\b(disput\w+|incorrect|overcharg\w+|chargeback|fraud\w*|unauthori[sz]ed|refund|"
    r"wrong\s+(?:invoice|charge|customer)|not\s+(?:my|our)\s+invoice|"
    r"never\s+(?:ordered|received|agreed)|(?:did\s+not|didn'?t)\s+(?:order|receive|agree)|"
    r"(?:charged|billed|debited)\s+(?:me\s+|us\s+)?(?:twice|again|two\s+times)|double\s+charg\w+|"
    r"paid\s+(?:it\s+)?twice)\b", re.I)
_CANCEL = re.compile(
    r"\b(cancel\w*|close\s+(?:my|our)\s+account|terminat\w+|stop\s+(?:my|the|our)\s+subscription)\b", re.I)
_PAID = re.compile(
    r"\b(already\s+paid|(?:i|we)(?:'?ve|\s+have)?\s+paid|(?:has|have)\s+(?:already\s+)?been\s+paid|"
    r"was\s+paid|paid\s+(?:it|this|that|the\s+invoice|on|last|yesterday|today|via|by|in\s+full)|"
    r"payment\s+(?:was|has\s+been)\s+(?:made|sent)|"
    r"(?:sent|made|wired|settled)\s+(?:the\s+|a\s+|my\s+|this\s+)?(?:payment|transfer|funds|money)|"
    r"transferred\s+(?:it|the\s+money|the\s+funds|the\s+payment)|settled\s+(?:it|this|the\s+invoice))\b",
    re.I)
_NEGATED = re.compile(r"\b(not|never|haven'?t|hasn'?t|n'?t|yet\s+to)\s*(?:yet\s+)?$", re.I)
_UPDATE = re.compile(
    r"\b(new\s+card|(?:update|change)\s+(?:my|our|the)\s+(?:card|bank|details|payment\s+method)|"
    r"changed\s+(?:my\s+|our\s+)?(?:card|bank)|card\s+(?:expired|was\s+(?:replaced|lost|stolen)|has\s+changed)|"
    r"different\s+card)\b", re.I)
_EXTENSION = re.compile(
    r"\b(more\s+time|extension|extend|will\s+pay|can\s+pay|able\s+to\s+pay|payment\s+plan|"
    r"instal+ments?|pay\s+(?:you\s+)?(?:on|by|next|in|at\s+the\s+end)|delay|defer|payday)\b", re.I)

_Q_STILL = re.compile(
    r"\b(still|again|another|keeps?\s+(?:sending|emailing|receiving|getting)|"
    r"(?:receiv\w+|get\w*|got)\s+(?:a\s+|another\s+|these\s+|your\s+)?(?:reminders?|chasers?))\b", re.I)
_Q_RECEIPT = re.compile(
    r"\b(?:no|not\s+(?:got|received)|never\s+(?:got|received)|(?:did\s+not|didn'?t)\s+(?:get|receive))"
    r"\s+(?:a\s+)?(?:receipt|confirmation)\b", re.I)
_Q_FAILED = re.compile(r"\b(bounced|returned|rejected|failed|declined|reversed)\b", re.I)
_Q_AMOUNT = re.compile(
    r"\b(?:(?:wrong|incorrect|different|less|more)\s+amount|only\s+paid|short[- ]?paid|part(?:ial)?\s+payment)\b",
    re.I)

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(\d{4}))?\b", re.I)
_MDY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", re.I)
_NUM = r"(?:[0-9]{1,3}(?:,[0-9]{3}){1,4}|[0-9]{1,12})(?:\.[0-9]{1,2})?"
_AMOUNT = re.compile(
    r"(?<![0-9,.])(?:(?P<sym>[€$£])|\b(?P<iso1>EUR|USD|GBP)\b)\s?(?P<n1>" + _NUM + r")(?![0-9]|[.,][0-9])|"
    r"(?<![0-9,.])(?P<n2>" + _NUM + r")(?![0-9]|[.,][0-9])\s?"
    r"(?:(?P<iso2>EUR|USD|GBP)|(?P<word>euros?|dollars?|pounds?))\b", re.I)
_SYMBOL_CCY = {"€": "eur", "$": "usd", "£": "gbp"}
_WORD_CCY = {"euro": "eur", "dollar": "usd", "pound": "gbp"}

_QUOTE_START = re.compile(
    r"^(?:on\s.+wrote:|-{2,}\s*original message\s*-{2,}|-{2,}\s*forwarded message\s*-{2,}|"
    r"from:\s.+|_{5,}|sent from my .+)$", re.I)
_SIGNOFF = re.compile(r"^(?:--\s*|regards|kind regards|best regards|thanks|thank you|cheers),?$", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_NAME_OK = re.compile(r"^[A-Za-zÀ-ɏ'’\- ]{1,40}$")


@dataclass(frozen=True)
class Reading:
    intent: str
    found: tuple[str, ...]  # every intent matched, in precedence order
    qualifier: str | None
    claimed_paid_on: date | None
    claimed_amount_minor: int | None
    promise_date: date | None
    confidence: str  # clear, mixed or unclear
    signals: tuple[str, ...]  # rule ids that fired, never message text


@dataclass(frozen=True)
class Context:
    """What the CALLER knows. The customer's message supplies none of these fields."""

    business_name: str
    invoice_no: str
    amount_minor: int
    currency: str
    customer_first_name: str | None = None
    due: date | None = None
    signoff_name: str | None = None
    item_label: str = "invoice"

    def __post_init__(self) -> None:
        if self.item_label not in ITEM_LABELS:
            raise ValueError(f"item_label: expected one of {ITEM_LABELS}")
        for name in ("business_name", "invoice_no", "currency"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or _CONTROL.search(value)):
                raise ValueError(f"{name}: control characters are refused")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int) or self.amount_minor <= 0:
            raise ValueError("amount_minor: expected a positive integer")


@dataclass(frozen=True)
class Reply:
    subject: str
    body: str
    actions: tuple[str, ...]
    person_must_act: bool
    rule_id: str
    check: str


def strip_quoted(body: str) -> str:
    """Drop quoted lines and everything from the first quote header down. Keeps the customer's own words."""
    kept: list[str] = []
    for raw in (body or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith(">"):
            continue
        if _QUOTE_START.match(line):
            break
        kept.append(raw)
    text = "\n".join(kept).strip()
    return text[:MAX_CHARS]


def _english_enough(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 6:
        return False
    return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.9


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _dates(sentence: str, today: date, *, future: bool = False) -> list[date]:
    out: list[date] = []
    low = sentence.lower()
    if re.search(r"\byesterday\b", low):
        out.append(today - timedelta(days=1))
    if re.search(r"\btoday\b", low):
        out.append(today)
    for m in _ISO.finditer(sentence):
        d = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            out.append(d)
    for rx, order in ((_DMY, "dm"), (_MDY, "md")):
        for m in rx.finditer(sentence):
            if order == "dm":
                day, mon, yr = int(m.group(1)), _MONTHS[m.group(2).lower()[:3]], m.group(3)
            else:
                mon, day, yr = _MONTHS[m.group(1).lower()[:3]], int(m.group(2)), m.group(3)
            year = int(yr) if yr else today.year
            d = _safe(year, mon, day)
            if d and not yr and not future and d > today:
                d = _safe(year - 1, mon, day)
            elif d and not yr and future and d < today:
                d = _safe(year + 1, mon, day)
            if d:
                out.append(d)
    return out


def _amount_minor(sentence: str, currency: str) -> int | None:
    """The amount a customer states, or None when it cannot be read safely.

    None is the safe answer and routes to a person. Refused: a European decimal comma
    ("25,00", "1.234,50"), a thousands group that is not three digits, and a currency
    other than the invoice's. The match must not run on into more digits.
    """
    m = _AMOUNT.search(sentence)
    if not m:
        return None
    stated = (
        _SYMBOL_CCY.get(m.group("sym") or "")
        or (m.group("iso1") or m.group("iso2") or "").lower()
        or _WORD_CCY.get((m.group("word") or "").lower().rstrip("s"), "")
    )
    if not stated or stated != currency.lower():
        return None
    raw = (m.group("n1") or m.group("n2") or "").replace(",", "")
    try:
        value = Decimal(raw)
    except Exception:
        return None
    places = exponent(currency)
    scaled = value * (10 ** places)
    if scaled != scaled.to_integral_value() or scaled <= 0:
        return None
    return int(scaled)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _paid_claim(sentence: str) -> bool:
    for m in _PAID.finditer(sentence):
        if not _NEGATED.search(sentence[max(0, m.start() - 14): m.start()]):
            return True
    return False


def read(message: str, *, today: date, currency: str) -> Reading:
    """Classify one inbound message. Precedence: opt_out, dispute, cancel, already_paid,
    update_details, extension. More than one distinct intent is ``mixed``, which is for a person."""
    text = strip_quoted(message if isinstance(message, str) else "")
    if not text or not _english_enough(text):
        return Reading("unclear", (), None, None, None, None, "unclear", ("not_english_or_empty",))

    found: list[str] = []
    signals: list[str] = []
    checks = (
        ("opt_out", _OPT_OUT.search),
        ("hardship", _HARDSHIP.search),
        ("dispute", _DISPUTE.search),
        ("repairs", _REPAIRS.search),
        ("cancel", _CANCEL.search),
        ("already_paid", lambda t: any(_paid_claim(s) for s in _sentences(t)) or None),
        ("benefits", _BENEFITS.search),
        ("update_details", _UPDATE.search),
        ("extension", _EXTENSION.search),
    )
    for name, fn in checks:
        if fn(text):
            found.append(name)
            signals.append(name)

    paid_on: date | None = None
    amount: int | None = None
    qualifier: str | None = None
    if "already_paid" in found:
        for s in _sentences(text):
            if _paid_claim(s) or paid_on is None:
                ds = _dates(s, today)
                if ds and paid_on is None and _paid_claim(s):
                    paid_on = ds[0]
                a = _amount_minor(s, currency)
                if a is not None and amount is None and _paid_claim(s):
                    amount = a
        for rx, name in ((_Q_AMOUNT, "wrong_amount"), (_Q_FAILED, "payment_failed"),
                         (_Q_RECEIPT, "no_receipt"), (_Q_STILL, "still_reminded")):
            if rx.search(text):
                qualifier = name
                signals.append("q:" + name)
                break
    if paid_on is not None and paid_on > today:
        paid_on = None  # a payment claimed for a future date is not a payment claim we can check

    promise: date | None = None
    if "extension" in found:
        for s in _sentences(text):
            ds = [d for d in _dates(s, today, future=True) if today < d <= today + timedelta(days=120)]
            if ds:
                promise = max(ds)

    if not found:
        return Reading("unclear", (), None, None, None, None, "unclear", ("no_rule_matched",))
    confidence = "clear" if len(found) == 1 else "mixed"
    return Reading(found[0], tuple(found), qualifier, paid_on, amount, promise, confidence, tuple(signals))


def check_claim(verdict: InFlightVerdict | None, ctx: Context) -> str:
    """Turn the bank-feed verdict into one of CHECKS. ``None`` means no feed was consulted."""
    if verdict is None:
        return "unchecked"
    if verdict.label == UNKNOWN:
        if verdict.rule_id == "partial_credit":
            return "found_part"
        return "feed_unreachable" if verdict.rule_id == "feed_unreachable" else "currency_mismatch"
    if verdict.label == HOLD:
        if verdict.matched_minor >= ctx.amount_minor:
            return "found_full_pending" if verdict.pending_only else "found_full_booked"
        return "found_part"
    if verdict.label == RELEASE:
        return "expired" if verdict.rule_id == "hold_expired" else "not_found"
    return "unchecked"


def _fmt(amount_minor: int, currency: str) -> str:
    places = exponent(currency)
    value = Decimal(amount_minor) / (10 ** places)
    return f"{currency.upper()} {value:,.{places}f}"


def _long_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def _greeting(ctx: Context) -> str:
    n = (ctx.customer_first_name or "").strip()
    return f"Hello {n}," if n and _NAME_OK.match(n) else "Hello,"


def _sign(ctx: Context) -> str:
    who = (ctx.signoff_name or "").strip()
    who = who if who and _NAME_OK.match(who) else ""
    return f"{who}\n{ctx.business_name}" if who else ctx.business_name


def _frame(ctx: Context, lines: list[str]) -> str:
    inv = f"{ctx.item_label} {ctx.invoice_no} ({_fmt(ctx.amount_minor, ctx.currency)}"
    inv += f", due {_long_date(ctx.due)})" if ctx.due else ")"
    body = [_greeting(ctx), ""]
    body += [ln.replace("{inv}", inv) for ln in lines]
    body += ["", _sign(ctx), "", f"Reference: {ctx.invoice_no}"]
    return "\n".join(body)


def draft(reading: Reading, check: str, ctx: Context) -> Reply:
    """One reply for one reading. Every branch is deterministic and every branch is a draft."""
    subject = f"Re: {ctx.item_label.capitalize()} {ctx.invoice_no}"
    hold = ("hold_chase",)

    def reply(lines, actions, must, rule):
        return Reply(subject, _frame(ctx, lines), actions, must, rule, check)

    if reading.intent == "opt_out":
        return reply(
            ["We have stopped reminder emails to this address for {inv}.",
             "A person will contact you separately about the invoice itself."],
            ("suppress_contact", "route_to_person"), True, "opt_out")

    if reading.intent == "hardship":
        return reply(
            ["Thank you for telling us about your situation regarding {inv}.",
             "We have paused reminders. A person will contact you personally to talk about what can be "
             "done. You can reply to this email at any time."],
            hold + ("flag_vulnerable", "route_to_person"), True, "hardship")

    if reading.intent == "repairs":
        return reply(
            ["Thank you for your message about {inv}.",
             "We have paused reminders while a person looks into the points you raised, including any "
             "repair issue, and replies to you."],
            hold + ("route_repairs",), True, "repairs")

    if reading.intent == "benefits":
        return reply(
            ["Thank you for your message about {inv}.",
             "We have noted that a benefit payment is expected. We have paused reminders and a person "
             "will check what we have received and reply to you."],
            hold + ("route_to_person",), True, "benefits")

    if reading.intent == "dispute" or reading.qualifier == "wrong_amount":
        return reply(
            ["Thank you for letting us know about {inv}.",
             "We have paused reminders for it and a person will review this and reply to you."],
            hold + ("route_dispute",), True, "dispute")

    if reading.intent == "cancel":
        return reply(
            ["Thank you for your message about {inv}.",
             "We have paused reminders for it and a person will reply to you about your request."],
            hold + ("route_cancellation",), True, "cancel")

    if reading.confidence == "mixed" or reading.intent == "unclear":
        return reply(
            ["Thank you for your message about {inv}.",
             "We have paused reminders for it and a person will read it and reply to you."],
            hold + ("route_to_person",), True, "mixed_or_unclear")

    if reading.intent == "already_paid":
        stated = []
        if reading.claimed_paid_on:
            stated.append(f"on {_long_date(reading.claimed_paid_on)}")
        stated_amt = reading.claimed_amount_minor
        amt_mismatch = stated_amt is not None and stated_amt != ctx.amount_minor
        if amt_mismatch:
            return reply(
                ["Thank you for your message about {inv}.",
                 f"You mentioned a payment of {_fmt(stated_amt, ctx.currency)}, which is different from "
                 "the invoice amount. We have paused reminders and a person will check this and reply to you."],
                hold + ("route_to_person",), True, "paid_amount_differs")
        when = f" {stated[0]}" if stated else ""
        if reading.qualifier == "payment_failed":
            return reply(
                ["Thank you for your message about {inv}.",
                 "You mentioned that the payment did not go through. We have paused reminders and a person "
                 "will check what happened and reply to you."],
                hold + ("route_to_person",), True, "paid_but_failed")
        apology = ("We are sorry for the reminder. " if reading.qualifier == "still_reminded" else "")
        if check == "found_full_booked":
            return reply(
                [f"Thank you for your message about {{inv}}. {apology}".rstrip(),
                 f"We can see a payment referencing this invoice for {_fmt(ctx.amount_minor, ctx.currency)}. "
                 "We have paused reminders while our records are updated."],
                hold + ("money_in_flight",), False, "paid_found_booked")
        if check == "found_full_pending":
            return reply(
                [f"Thank you for your message about {{inv}}. {apology}".rstrip(),
                 "We can see a payment referencing this invoice that is still pending, so it has not "
                 "reached us yet. We have paused reminders until it clears."],
                hold + ("money_in_flight",), False, "paid_found_pending")
        if check == "found_part":
            return reply(
                ["Thank you for your message about {inv}.",
                 "We can see a payment referencing this invoice that is less than the invoice amount. "
                 "We have paused reminders and a person will check it and reply to you."],
                hold + ("money_in_flight", "route_to_person"), True, "paid_found_part")
        if check in ("not_found", "expired"):
            return reply(
                [f"Thank you for your message about {{inv}}. {apology}".rstrip(),
                 f"We cannot see a payment referencing this invoice yet{when}. Payments can take a few "
                 "business days to appear. If you have the bank reference or a confirmation, please reply "
                 "with it and we will look again. We have paused reminders in the meantime."],
                hold + ("route_to_person",), True, "paid_not_found")
        return reply(
            ["Thank you for your message about {inv}.",
             f"You mentioned that it has been paid{when}. We cannot check payments automatically right now, "
             "so we have paused reminders and a person will confirm it and reply to you."],
            hold + ("route_to_person",), True, "paid_unchecked")

    if reading.intent == "update_details":
        return reply(
            ["Thank you for your message about {inv}.",
             "We have paused reminders. Please reply to this email and a person will send you a secure "
             "way to update your payment details. We will never ask for card numbers by email."],
            hold + ("route_to_person",), True, "update_details")

    # extension
    when = f" until {_long_date(reading.promise_date)}" if reading.promise_date else ""
    return reply(
        ["Thank you for your message about {inv}.",
         f"We have noted that you would like more time{when}. We have paused reminders and a person "
         "will confirm what we can agree and reply to you."],
        hold + ("route_to_person",), True, "extension")
