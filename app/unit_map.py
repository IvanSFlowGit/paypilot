# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Speak Unit: translate a returned-ACH event into an ACH decision, and refuse to guess.

Unit (unit.co) is a banking-as-a-service platform. When an ACH payment a client
originated comes back, Unit reports a return reason by NAME, for example
``InsufficientFunds``, where the decision table here is keyed by NACHA code,
``R01``. This module is the translation, and it decides nothing: the decision
comes from the ACH table through the rails registry.

WHAT WAS READ, AND WHEN. Read from Unit's own pages on 2026-11-09, neither of
which carries a date or version:

* the ACH returns reference (unit.co/docs/api/ach-returns): 41 rows of reason
  name and ACH code, for originated and received payments. ``UNIT_REASONS``
  below is that table. The page does not say the list is complete.
* the webhooks page (unit.co/docs/api/webhooks): ``X-Unit-Signature``, an
  HMAC-SHA1 over the entire request body, base64 encoded, and only when a secret
  token is set on the webhook.

STATE AS OF 2026-11-11. A sandbox Unit webhook has delivered REAL SIGNED events
(customer.created, account.created) to this receiver through a tunnel and the
signature check accepted them: base64 HMAC-SHA1 over the raw body, header
X-Unit-Signature, confirmed against Unit's docs and by a 200. NO REAL
``payment.returned`` EVENT HAS EVER BEEN SEEN. The sandbox has no simulation for
returning an originated ACH debit (Unit's docs list only transmit and clear, and
Unit support confirmed it in writing). Unit's human support said in writing on
2026-11-10, quoting its docs: for ACH returns the R-code is on the
``payment.returned`` event's ``reason`` attribute (for example R03), and a
returnedAchTransaction also carries a readable ``reason`` (for example NoAccount).
That is support's statement, relayed; NO FULL EXAMPLE PAYLOAD WAS GIVEN. An earlier
answer from Unit's AI chat said the event has no reason, then said the reason
arrives on ``receivedPayment.returned`` as ``returnReason``; both are superseded
and neither is relied on. What is still NOT established, each failing safe:

1. THE FULL ENVELOPE. The pages show a JSON:API ``data`` array of events, each
   with ``type``, ``attributes`` and ``relationships``. Anything that is not
   that shape raises :class:`UnitPayloadError` and nothing is decided.
2. THE EXACT POSITION OF ``reason`` IN A REAL ``payment.returned`` BODY. ``reason``
   is read first and ``returnReason`` second. If neither is present the result is
   HELD (``reason_not_in_event``) and a person acts. This module never calls
   Unit's API to look the reason up.
3. THAT A SIMULATED RETURN (``simulate-return-ach-repayment``, which fires Payment
   Returned for the underlying payment) HAS THE SAME SHAPE AS A RETURNED
   ORIGINATED DEBIT. It is a different path, so it proves delivery, signature and
   parsing and does not prove the shape of a real returned debit.

Only ``payment.returned`` is decided. ``payment.rejected`` is a rejection before
the payment reached the network, with its own reason list (it can say
``InsufficientFunds`` about Unit's own balance check), so it is not a NACHA
return and is reported as not a network return rather than mapped.

The signature check fails CLOSED. Unit signs only when a secret is configured;
a receiver here refuses an unsigned delivery rather than accepting it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from app import ach_return_map, rails

#: Unit's reason name -> ACH code, read from unit.co/docs/api/ach-returns on 2026-11-09. 41 rows.
UNIT_REASONS: dict[str, str] = {
    "InsufficientFunds": "R01",
    "AccountClosed": "R02",
    "NoAccount": "R03",
    "InvalidAccountNumberStructure": "R04",
    "UnauthorizedDebitToConsumer": "R05",
    "ReturnedPerOdfiRequest": "R06",
    "AuthorizationRevokedByCustomer": "R07",
    "PaymentStopped": "R08",
    "UncollectedFunds": "R09",
    "ConsumerPaymentUnauthorized": "R10",
    "CheckTruncationEntryReturned": "R11",
    "BranchSoldToAnotherDfi": "R12",
    "RdfiNotQualifiedToParticipate": "R13",
    "RepresentativePayeeDeceasedOrUnableToContinue": "R14",
    "BeneficiaryOrBankAccountHolderDeceased": "R15",
    "AccountFrozen": "R16",
    "FileRecordEditCriteria": "R17",
    "ImproperEffectiveEntryDate": "R18",
    "AmountFieldError": "R19",
    "NonTransactionAccount": "R20",
    "InvalidCompanyIdentification": "R21",
    "InvalidIndividualIdNumber": "R22",
    "CreditEntryRefusedByReceiver": "R23",
    "DuplicateEntry": "R24",
    "AddendaError": "R25",
    "MandatoryFieldError": "R26",
    "TraceNumberError": "R27",
    "RoutingNumberCheckDigitError": "R28",
    "CorporatePaymentUnauthorized": "R29",
    "RdfiNotParticipantInCheckTruncationProgram": "R30",
    "PermissibleReturnEntry": "R31",
    "RdfiNonSettlement": "R32",
    "ReturnOfXckEntry": "R33",
    "LimitedParticipationDfi": "R34",
    "ReturnOfImproperDebitEntry": "R35",
    "ReturnOfImproperCreditEntry": "R36",
    "SourceDocumentPresentedForPayment": "R37",
    "StopPaymentOnSourceDocument": "R38",
    "ImproperSourceDocument": "R39",
    "InvalidIndividualOrCompanyName": "R45",
    "IncorrectlyCodedOutboundInternationalPayment": "R85",
}

_CODE_RE = re.compile(r"^R\d{2}$")
_ALNUM = re.compile(r"[^a-z0-9]")
_BY_FOLDED = {_ALNUM.sub("", name.lower()): code for name, code in UNIT_REASONS.items()}

RETURN_EVENT = "payment.returned"
MAX_EVENTS = 1000
MAX_BYTES = 5_000_000


class UnitPayloadError(ValueError):
    """The delivery is not the shape this module understands. Nothing is decided."""


def nacha_code_for(reason: object) -> str | None:
    """The ACH code for a Unit reason name, or a code passed through, or None.

    Case and punctuation are ignored. An unknown name returns None, never a guess,
    and the caller treats None as "a person acts".
    """
    if not isinstance(reason, str):
        return None
    text = reason.strip()
    if _CODE_RE.fullmatch(text.upper()):
        return text.upper()
    return _BY_FOLDED.get(_ALNUM.sub("", text.lower()))


def verify_unit_signature(raw_body: bytes, header: str | None, secret: str | None) -> bool:
    """True only if ``header`` is the base64 HMAC-SHA1 of ``raw_body`` under ``secret``.

    Fails closed: no secret, no header or a wrong header is False. Unit signs only
    when a token is configured on the webhook, so a receiver that accepted an
    unsigned delivery would be accepting anyone's POST.
    """
    if not secret or not header or not isinstance(raw_body, (bytes, bytearray)):
        return False
    digest = hmac.new(secret.encode("utf-8"), bytes(raw_body), hashlib.sha1).digest()
    expected = base64.b64encode(digest)
    return hmac.compare_digest(expected, header.strip().encode("utf-8"))


@dataclass(frozen=True)
class UnitReturn:
    """One returned-payment event and what the ACH table says about it."""

    event_id: str
    event_type: str
    payment_id: str
    reason: str | None
    code: str | None
    status: str  # decided | held | not_a_return
    matched: bool
    decision_row: int | None
    person_required: bool
    why: str
    #: What the customer is told for this code, straight from the ACH table. Empty when
    #: held or not a return: a held return gets no message until a person decides.
    customer_message: str = ""


def _text(value: object, limit: int = 128) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _reason_of(attributes: object) -> str | None:
    if not isinstance(attributes, dict):
        return None
    for key in ("reason", "returnReason"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:128]
    return None


def _payment_id(item: dict) -> str:
    rel = item.get("relationships")
    if isinstance(rel, dict):
        data = (rel.get("payment") or {}).get("data") if isinstance(rel.get("payment"), dict) else None
        if isinstance(data, dict):
            return _text(data.get("id"))
    return ""


def _decide_one(item: dict) -> UnitReturn:
    etype = _text(item.get("type"))
    event_id = _text(item.get("id"))
    payment_id = _payment_id(item)
    if etype != RETURN_EVENT:
        return UnitReturn(event_id, etype, payment_id, None, None, "not_a_return", False, None, False,
                          f"{etype or 'an event with no type'} is not {RETURN_EVENT}, so no ACH decision is made.")
    reason = _reason_of(item.get("attributes"))
    if reason is None:
        return UnitReturn(event_id, etype, payment_id, None, None, "held", False, None, True,
                          "reason_not_in_event: the event carries no reason, and this module never calls "
                          "Unit to look one up. A person acts.")
    code = nacha_code_for(reason)
    if code is None:
        return UnitReturn(event_id, etype, payment_id, reason, None, "held", False, None, True,
                          "unknown_reason_name: not in Unit's published return table. A person acts.")
    exp = rails.explain("ach", code)
    message = ach_return_map.lookup_return_code(code).customer_message
    return UnitReturn(event_id, etype, payment_id, reason, code, "decided", bool(exp.matched),
                      exp.decision_row, bool(exp.person_required), exp.why, message)


def parse_unit_delivery(raw: bytes | str) -> list[UnitReturn]:
    """Parse one webhook delivery (or a saved one) into decisions.

    Accepts ``{"data": [event, ...]}`` or a bare list of events. Any other shape
    raises :class:`UnitPayloadError`. Events repeated within the delivery by id
    are decided once, because Unit documents at-least-once delivery.
    """
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_BYTES:
            raise UnitPayloadError("delivery: larger than the cap")
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise UnitPayloadError("delivery: not UTF-8") from None
    elif isinstance(raw, str):
        text = raw
    else:
        raise UnitPayloadError("delivery: expected bytes or text")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise UnitPayloadError("delivery: not valid JSON") from None
    events = doc.get("data") if isinstance(doc, dict) else doc
    if isinstance(events, dict):
        events = [events]
    if not isinstance(events, list) or not events:
        raise UnitPayloadError("delivery: expected a non-empty data array of events")
    if len(events) > MAX_EVENTS:
        raise UnitPayloadError("delivery: more events than the cap")
    out: list[UnitReturn] = []
    seen: set[str] = set()
    for item in events:
        if not isinstance(item, dict):
            raise UnitPayloadError("delivery: an event is not an object")
        eid = _text(item.get("id"))
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        out.append(_decide_one(item))
    return out
