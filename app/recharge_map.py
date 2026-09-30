# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Speak Recharge: verify and translate failed subscription charges.

Recharge bills subscriptions for Shopify stores and runs its own retry schedule
("Failed Payment Recovery"). PayPilot does not compete with that schedule. It
reads the failed charge, decides whether the customer has to act, and writes the
message; when Recharge will retry, Recharge's ``retry_date`` is reported as the
schedule, never a PayPilot guess.

Three pure pieces so the webhook route stays thin and testable with no network:

* :func:`verify_recharge_signature` - Recharge signs a webhook as the hex
  SHA-256 of ``client_secret + request_body`` in ``X-Recharge-Hmac-Sha256``
  (docs.getrecharge.com/docs/webhooks-overview, read 2026-09-30). That is a
  prefixed hash, not an HMAC, so it is computed exactly as documented and
  compared in constant time.
* :func:`classify_error_type` - one decision per published ``error_type``.
* :func:`recharge_charge_to_internal` - the flat event dict the graph consumes.

The error-type table comes from Recharge's own list, "Managing order errors"
(support.getrecharge.com/hc/en-us/articles/22181648082839, read 2026-09-30),
which marks each type as retried by Failed Payment Recovery and/or a hard
decline. Many types are not payment problems at all (inventory, shipping, tax,
a missing variant, test mode), and PayPilot must never email a customer about
those, so they classify as NOT_CUSTOMER and the route acknowledges them without
running recovery. An error type that is not in the table classifies as
UNMAPPED and is also not emailed about: an unknown code fails safe and loud.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

#: The route acknowledges these without emailing the customer.
NOT_CUSTOMER = "not_customer"
UNMAPPED = "unmapped"

#: Every ``error_type`` Recharge publishes, as (retried by Failed Payment
#: Recovery, hard decline), transcribed from the article named above. Kept as
#: data so a test can assert each one has a decision below.
PUBLISHED_ERROR_TYPES: dict[str, tuple[bool, bool]] = {
    "ACCOUNT_CLOSED": (True, False), "AMOUNT_TOO_LARGE": (True, False),
    "AMOUNT_TOO_SMALL": (False, True), "AUTHENTICATION_ERROR": (False, False),
    "AUTHENTICATION_FAILED": (False, True), "AUTHENTICATION_REQUIRED": (False, True),
    "BANK_ACCOUNT_RESTRICTED": (True, False), "BILLING_ADDRESS_ERROR": (True, False),
    "BLOCKED_FROM_AUTOMATIC_PROCESSING": (False, False),
    "BUYER_CANCELED_PAYMENT_METHOD": (True, False), "CALL_ISSUER": (True, False),
    "CANCELLED_PAYMENT": (True, False), "CAPTURE_AMOUNT_EXCEEDS_AUTHORIZED": (False, False),
    "CAPTURE_FAILED": (False, False), "CAPTURE_PAYMENT_ERROR": (False, False),
    "CARDNUMBER_INCORRECT": (True, False), "CARD_DECLINED": (True, False),
    "CARD_ERROR_GENERAL": (True, False), "CARD_EXPIRED": (True, False),
    "CARD_TESTING": (False, False), "CARD_UPDATED_NOW_PENDING_NEXT_ATTEMPT": (True, False),
    "CARD_ZIPCODE_FAILED_VALIDATION": (True, False),
    "CLOSED_MAX_RETRIES_REACHED": (True, False), "CONFIRMATION_REJECTED": (False, True),
    "CONTRACT_NOT_AVAILABLE": (False, False), "CONTRACT_TERMINATED": (False, False),
    "CONTRACT_UNDER_REVIEW": (False, False), "COULD_NOT_PROCESS": (True, False),
    "CUSTOMER_INVALID": (False, True), "CUSTOMER_NEEDS_TO_UPDATE_CARD": (True, False),
    "CUSTOMER_NOT_FOUND": (False, False), "CUSTOMER_NO_TOKEN": (False, False),
    "DEBIT_AUTHORIZATION_REVOKED": (True, False), "DEBIT_NOT_AUTHORIZED": (True, False),
    "DO_NOT_HONOR": (False, True), "EMPTY_CHARGE_ENTRY": (False, False),
    "EXPIRED_BUYER_ACTION": (False, True), "EXPIRED_PAYMENT_METHOD": (True, False),
    "FRAUD_SUSPECTED": (False, True), "FREE_GIFT_CARD_NOT_ALLOWED": (False, False),
    "GENERIC_ERROR": (True, False), "GENERAL_FAILURE": (False, True),
    "HIGH_RISK_FRAUD_SUSPECTED": (False, True), "INCORRECT_ADDRESS": (False, True),
    "INCORRECT_NUMBER": (True, False), "INCORRECT_ZIP": (False, True),
    "INSTRUMENT_DECLINED": (False, True), "INSUFFICIENT_CREDIT_BALANCE": (False, True),
    "INSUFFICIENT_FUNDS": (True, False), "INVALID": (False, False),
    "INVALID_ACCOUNT_NUMBER": (True, False), "INVALID_BILLING_ADDRESS": (False, True),
    "INVALID_COUNTRY": (False, True), "INVALID_CURRENCY": (False, True),
    "INVALID_CUSTOMER_BILLING_AGREEMENT": (True, False), "INVALID_DISCOUNT": (False, True),
    "INVALID_DISCOUNT_AMOUNT": (False, True), "INVALID_EXPIRY_DATE": (False, True),
    "INVALID_LINE_ITEM_PROPERTY": (False, True), "INVALID_NUMBER": (True, False),
    "INVALID_PAYMENT_METHOD": (True, False), "INVALID_PHONE_NUMBER": (False, True),
    "INVALID_PICK_UP_LOCATION": (False, True), "INVALID_PURCHASE_TYPE": (True, False),
    "INVALID_ROUTING_NUMBER": (True, False), "INVALID_SHIPPING_ADDRESS": (False, True),
    "INVALID_SHIPPING_PROVINCE": (False, True), "INVALID_TOKEN": (True, False),
    "INVALID_ZIP_CODE": (False, True), "INVENTORY_ALLOCATIONS_NOT_FOUND": (True, False),
    "MERCHANT_ACCOUNT_ERROR": (False, False), "MERCHANT_RULE": (False, False),
    "MIN_CHARGE_ERROR": (False, False), "NO_JSON_FOUND": (False, False),
    "NON_TEST_ORDER_LIMIT_REACHED": (False, False), "OFF_SESSION_REJECTED": (False, True),
    "PAYMENT_CAPTURE_ERROR": (False, False),
    "PAYMENT_INTENT_PAYMENT_ATTEMPT_FAILED": (True, False),
    "PAYMENT_METHOD_DECLINED": (True, False),
    "PAYMENT_METHOD_INCOMPATIBLE_WITH_GATEWAY_CONFIG": (False, False),
    "PAYMENT_METHOD_NOT_FOUND": (False, True), "PAYMENT_METHOD_NOT_SPECIFIED": (False, True),
    "PAYMENT_METHOD_REVOKED": (True, False), "PAYMENT_METHOD_UNSUPPORTED": (False, True),
    "PAYMENT_PROVIDER_ERROR": (True, False), "PAYMENT_PROVIDER_IS_NOT_ENABLED": (False, False),
    "PAYPAL_ERROR_GENERAL": (True, False), "PENDING_SCA_AUTHENTICATION": (False, False),
    "PICK_UP_CARD": (False, True), "PROCESSING_ERROR": (True, False),
    "PURCHASE_TYPE_NOT_SUPPORTED_BY_CARD": (True, False), "REFER_TO_CUSTOMER": (True, False),
    "RETRY_DECLINED": (False, True), "SCA_CONFIRM_FAILED": (True, False),
    "SEPA_DEBIT_FAILURE": (True, False), "SHIPPING_RATE_ERROR": (False, False),
    "SHOPIFY_REJECTED": (False, False), "TAX_RATE_ERROR": (False, False),
    "TEST_MODE": (False, False), "THROTTLE": (True, False),
    "TRANSACTION_LIMIT_EXCEEDED": (True, False), "TRANSACTION_SIZE_DECLINE": (True, False),
    "TRANSIENT_ERROR": (True, False), "UNEXPECTED_ERROR": (True, False),
    "UNEXPECTED_INVENTORY_LEVEL": (True, False), "UNEXPECTED_REGEN_ERROR": (False, False),
    "UNEXPECTED_VARIANT_ERROR_TYPE": (False, False), "UNKNOWN_ERROR": (False, False),
    "VARIANT_DOES_NOT_EXIST": (False, False),
}

#: Specific reasons PayPilot already has copy for.
_SPECIFIC: dict[str, str] = {
    "CARD_EXPIRED": "card_expired",
    "EXPIRED_PAYMENT_METHOD": "card_expired",
    "INSUFFICIENT_FUNDS": "insufficient_funds",
    # The details on file are wrong: retrying the same details fails the same way.
    "CARDNUMBER_INCORRECT": "card_details_invalid",
    "INCORRECT_NUMBER": "card_details_invalid",
    "INVALID_NUMBER": "card_details_invalid",
    "INVALID_EXPIRY_DATE": "card_details_invalid",
    "CARD_ZIPCODE_FAILED_VALIDATION": "card_details_invalid",
    "INCORRECT_ZIP": "card_details_invalid",
    "INCORRECT_ADDRESS": "card_details_invalid",
    "BILLING_ADDRESS_ERROR": "card_details_invalid",
    "INVALID_BILLING_ADDRESS": "card_details_invalid",
    "INVALID_ACCOUNT_NUMBER": "card_details_invalid",
    "INVALID_ROUTING_NUMBER": "card_details_invalid",
    # Recharge has stopped retrying: the customer has to supply a new method.
    "CLOSED_MAX_RETRIES_REACHED": "retries_exhausted",
    # The card on file will not work; the customer must replace it.
    "CUSTOMER_NEEDS_TO_UPDATE_CARD": "issuer_do_not_retry",
}

#: Not a payment problem the customer can fix, or a state where an email would
#: be wrong: store configuration, inventory, shipping, tax, test traffic, the
#: processor being unavailable, or the customer having ALREADY updated the card.
_NOT_CUSTOMER: frozenset[str] = frozenset({
    "AMOUNT_TOO_SMALL", "BLOCKED_FROM_AUTOMATIC_PROCESSING",
    "CAPTURE_AMOUNT_EXCEEDS_AUTHORIZED", "CAPTURE_FAILED", "CAPTURE_PAYMENT_ERROR",
    "CARD_TESTING", "CARD_UPDATED_NOW_PENDING_NEXT_ATTEMPT", "CONTRACT_NOT_AVAILABLE",
    "CONTRACT_TERMINATED", "CONTRACT_UNDER_REVIEW", "CUSTOMER_NOT_FOUND",
    "EMPTY_CHARGE_ENTRY", "FREE_GIFT_CARD_NOT_ALLOWED", "INVALID", "INVALID_COUNTRY",
    "INVALID_CURRENCY", "INVALID_DISCOUNT", "INVALID_DISCOUNT_AMOUNT",
    "INVALID_LINE_ITEM_PROPERTY", "INVALID_PHONE_NUMBER", "INVALID_PICK_UP_LOCATION",
    "INVALID_SHIPPING_ADDRESS", "INVALID_SHIPPING_PROVINCE",
    "INVENTORY_ALLOCATIONS_NOT_FOUND", "MERCHANT_ACCOUNT_ERROR", "MERCHANT_RULE",
    "MIN_CHARGE_ERROR", "NO_JSON_FOUND", "NON_TEST_ORDER_LIMIT_REACHED",
    "PAYMENT_METHOD_INCOMPATIBLE_WITH_GATEWAY_CONFIG", "PAYMENT_PROVIDER_ERROR",
    "PAYMENT_PROVIDER_IS_NOT_ENABLED", "SHIPPING_RATE_ERROR", "SHOPIFY_REJECTED",
    "TAX_RATE_ERROR", "TEST_MODE", "THROTTLE", "TRANSIENT_ERROR",
    "UNEXPECTED_INVENTORY_LEVEL", "UNEXPECTED_REGEN_ERROR",
    "UNEXPECTED_VARIANT_ERROR_TYPE", "UNKNOWN_ERROR", "VARIANT_DOES_NOT_EXIST",
})


def classify_error_type(error_type: str | None) -> str:
    """PayPilot failure code for a Recharge ``error_type``, or NOT_CUSTOMER / UNMAPPED.

    Order: a specific reason PayPilot has copy for; then the non-customer set;
    then Recharge's own flags for everything else that is a payment failure. A
    hard decline, or a failure Recharge does not retry, needs a new payment
    method. A failure Recharge retries is a generic decline whose timing
    Recharge owns.
    """
    code = (error_type or "").strip().upper()
    if not code or code not in PUBLISHED_ERROR_TYPES:
        return UNMAPPED
    if code in _SPECIFIC:
        return _SPECIFIC[code]
    if code in _NOT_CUSTOMER:
        return NOT_CUSTOMER
    retried, hard = PUBLISHED_ERROR_TYPES[code]
    if hard or not retried:
        return "issuer_do_not_retry"
    return "generic_decline"


def recharge_retries(error_type: str | None) -> bool:
    """True when Recharge's Failed Payment Recovery retries this error type."""
    return PUBLISHED_ERROR_TYPES.get((error_type or "").strip().upper(), (False, False))[0]


def verify_recharge_signature(payload: bytes, signature: str, client_secret: str) -> bool:
    """Constant-time check of ``X-Recharge-Hmac-Sha256`` as Recharge documents it."""
    if not signature or not client_secret:
        return False
    expected = hashlib.sha256(client_secret.encode("utf-8") + payload).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _retry_date(charge: dict) -> str | None:
    """Recharge's ``retry_date`` as ISO 8601 UTC, or None when absent or unparseable."""
    raw = charge.get("retry_date")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat(timespec="seconds")


def recharge_charge_to_internal(body: dict) -> dict:
    """Translate a Recharge charge webhook body (``{"charge": {...}}``) for the graph.

    Returns the event dict plus ``classification``. Handles the customer shape of
    both API versions: 2021-01 puts ``customer_id``/``email`` on the charge,
    2021-11 nests them under ``customer``. Amounts are already in major units.
    """
    charge = body.get("charge") if isinstance(body.get("charge"), dict) else {}
    error_type = charge.get("error_type")
    classification = classify_error_type(error_type)

    customer = charge.get("customer") if isinstance(charge.get("customer"), dict) else {}
    customer_id = customer.get("id") or charge.get("customer_id") or ""
    email = customer.get("email") or charge.get("email") or None
    first = (charge.get("first_name") or customer.get("first_name") or "").strip()
    last = (charge.get("last_name") or customer.get("last_name") or "").strip()
    name = " ".join(p for p in (first, last) if p) or None

    lines = charge.get("line_items") or []
    plan = None
    if lines and isinstance(lines[0], dict):
        plan = (lines[0].get("title") or "").strip() or None

    try:
        amount = float(charge.get("total_price") or 0)
    except (TypeError, ValueError):
        amount = 0.0

    will_retry = recharge_retries(error_type) and classification == "generic_decline"
    return {
        "classification": classification,
        "error_type": (error_type or "").strip().upper() or None,
        "event": {
            "customer_id": str(customer_id),
            "amount": amount,
            "currency": (charge.get("currency") or "usd").lower(),
            "failure_code": classification,
            "attempt": int(charge.get("number_times_tried") or 1),
            "customer_name": name,
            "customer_email": email,
            "plan": plan,
            "processor": "recharge",
            "recharge_retry_date": _retry_date(charge) if will_retry else None,
        },
    }
