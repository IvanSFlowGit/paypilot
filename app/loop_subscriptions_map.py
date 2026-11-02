# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Speak Loop Subscriptions: translate a failed renewal order for the graph.

Loop Subscriptions (loopwork.co) is a Shopify subscription billing app. It owns
the subscription contracts, places each renewal order on Shopify, runs its own
retry schedule ("Payment Recovery") and sends its own customer emails. PayPilot
does not compete with that. It reads the failed renewal, decides whether the
customer has to act, and reports Loop's remaining retries as the schedule.

THIS NAME IS NOT ``app/loop.py``. That module is the Stripe recovery state
machine ("the closed recovery loop") and has nothing to do with Loop
Subscriptions, which is why this one is called ``loop_subscriptions_map``.

THIS MODULE HAS NEVER SEEN A LIVE LOOP PAYLOAD. The fixture under ``tests/`` is
copied from the example in Loop's own webhook guide, so it shows the field names
Loop documents and is not evidence that a delivery arrives as spelled. No Loop
merchant account, Admin API key or webhook subscription exists for this product.

WHAT IS ESTABLISHED, read first hand 2026-11-02 from Loop's own pages:

* A merchant subscribes with the Loop Admin API, ``POST
  api.loopsubscriptions.com/admin/2026-04/webhook``, which takes ``topic`` and
  ``address`` and needs the ``write_webhooks`` scope
  (developer.loopwork.co/reference/subscribe-webhook). The topic that matters is
  ``order/paymentFailed``, "Triggered when an payment is failed for an order"
  (help.loopwork.co/en/articles/12672678-loop-webhooks-explained-a-guide-for-developers).
* The failed-order payload carries ``paymentAttempt`` with ``errorCode``,
  ``errorMessage``, ``retryAttemptCount``, ``retryLeftCount``, ``retryDelay`` and
  ``idempotencyKey``, plus the order and subscription, and
  ``metaData.myshopifyDomain``.
* The error vocabulary is Loop's published table, "Payment errors"
  (help.loopwork.co/en/articles/12712734-payment-errors): Shopify's
  ``SubscriptionBillingAttemptErrorCode`` values for API version 2026-04 onwards,
  grouped by Loop into named failure reasons, and a second, older lowercase
  category list for before April 2026. Both are transcribed below.

WHAT IS NOT STATED ANYWHERE THAT WAS READ, and so is not assumed:

* That a delivery is signed or carries any header. The subscribe call has no
  field for a secret, so the route authenticates the way the Chargebee route
  does, with a secret in the webhook URL, and must never be described as
  verifying a signature.
* Where the topic name travels in a delivery. The example body has no topic
  field, so this module classifies by payload shape and does not read one.
* Which vocabulary ``errorCode`` uses after April 2026. The documentation's
  example shows lowercase ``insufficient_funds`` (the older category style),
  while the new table lists upper case Shopify codes. Both are accepted.
* The unit of ``retryDelay``. It is carried through unconverted, and no retry
  date is computed from it.
* Whether the order ``id`` is stable across retries of one renewal. The event
  carries the attempt idempotency key separately for that reason.
* Any retry or timeout behaviour of the delivery itself, and any plan or tier a
  merchant needs for the Admin API.

Three pure pieces so the webhook route stays thin and testable with no network:

* :func:`classify_loop_error` - one decision per published error code.
* :func:`loop_retries_remaining` - how many retries Loop says it still owns.
* :func:`loop_order_failed_to_internal` - the flat event dict the graph consumes.
"""

from __future__ import annotations

#: The route acknowledges these without emailing the customer.
NOT_CUSTOMER = "not_customer"
UNMAPPED = "unmapped"

#: Every Shopify error code in Loop's "Error codes on Loop after April 2026"
#: table, mapped to the Loop failure reason Loop groups it under. Transcribed
#: from the page named above. Kept as data so a test can assert every code has a
#: decision and the table cannot silently lose a row.
PUBLISHED_SHOPIFY_CODES: dict[str, str] = {
    "AMOUNT_TOO_LARGE": "Amount too large",
    "AMOUNT_TOO_SMALL": "Amount too small",
    "AUTHENTICATION_ERROR": "3DS authentication failed",
    "AUTHENTICATION_FAILED": "3DS authentication failed",
    "AUTHENTICATION_REQUIRED": "3DS authentication failed",
    "CONFIRMATION_REJECTED": "3DS authentication failed",
    "3DS_CHALLENGE_EXPIRED_LOOP": "3DS authentication failed",
    "BUYER_CANCELED_PAYMENT_METHOD": "PayPal agreement cancelled",
    "CALL_ISSUER": "Card declined",
    "CARD_DECLINED": "Card declined",
    "PAYMENT_METHOD_DECLINED": "Card declined",
    "CANCELLED_PAYMENT": "Billing agreement cancelled",
    "CARD_NUMBER_INCORRECT": "Incorrect card number",
    "INCORRECT_NUMBER": "Incorrect card number",
    "INVALID_NUMBER": "Incorrect card number",
    "CUSTOMER_INVALID": "Invalid customer",
    "CUSTOMER_NOT_FOUND": "Invalid customer",
    "DO_NOT_HONOR": "Do not honor",
    "EXPIRED_BUYER_ACTION": "Some error occurred",
    "GENERIC_ERROR": "Some error occurred",
    "OFF_SESSION_REJECTED": "Some error occurred",
    "UNEXPECTED_ERROR": "Some error occurred",
    "EXPIRED_CARD": "Card expired",
    "EXPIRED_PAYMENT_METHOD": "Card expired",
    "INVALID_EXPIRY_DATE": "Card expired",
    "FRAUD_SUSPECTED": "Fraud suspected",
    "MERCHANT_RULE": "Fraud suspected",
    "CONTRACT_UNDER_REVIEW": "Fraud suspected",
    "FREE_GIFT_CARD_NOT_ALLOWED": "Invalid gift card amount",
    "INCORRECT_ADDRESS": "Invalid address",
    "INCORRECT_ZIP": "Invalid address",
    "INVALID_BILLING_ADDRESS": "Invalid address",
    "INVALID_SHIPPING_ADDRESS": "Invalid address",
    "INSUFFICIENT_FUNDS": "Insufficient funds",
    "INSUFFICIENT_INVENTORY": "Insufficient inventory",
    "INVALID_CURRENCY": "Invalid currency",
    "INVALID_CUSTOMER_BILLING_AGREEMENT": "PayPal agreement invalid",
    "INVALID_PAYMENT_METHOD": "Invalid payment method",
    "INVALID_PURCHASE_TYPE": "Card type not supported",
    "PAYMENT_METHOD_UNSUPPORTED": "Card type not supported",
    "PURCHASE_TYPE_NOT_SUPPORTED": "Card type not supported",
    "INVENTORY_ALLOCATIONS_NOT_FOUND": "No inventory location found",
    "INVOICE_ALREADY_PAID": "Invoice already paid",
    "MERCHANT_ACCOUNT_ERROR": "Merchant account error",
    "NON_TEST_ORDER_LIMIT_REACHED": "Gateway test mode error",
    "PAYMENT_METHOD_INCOMPATIBLE_WITH_GATEWAY_CONFIG": "Gateway test mode error",
    "TEST_MODE": "Gateway test mode error",
    "PAYMENT_METHOD_NOT_FOUND": "Payment method revoked",
    "PAYMENT_METHOD_NOT_SPECIFIED": "Payment method revoked",
    "PAYMENT_PROVIDER_ERROR": "Payment provider error",
    "PROCESSING_ERROR": "Payment provider error",
    "PAYMENT_PROVIDER_IS_NOT_ENABLED": "Payment provider not enabled",
    "PAYPAL_ERROR_GENERAL": "PayPal general error",
    "RETRY_DECLINED": "Card retry declined",
    "TRANSACTION_LIMIT_EXCEEDED": "Transaction limit exceeded",
    "TRANSIENT_ERROR": "Transient error",
    "SHOPIFY_DECLINED": "Shopify declined",
    "SHOPIFY_NO_CONFIRMATION": "Shopify declined",
}

#: Every category in Loop's "Error codes on Loop before April 2026" table. These
#: are lowercase and are not Shopify's enum, which is why they are a separate
#: table rather than extra rows above.
PUBLISHED_LEGACY_CATEGORIES: frozenset[str] = frozenset({
    "card_declined",
    "insufficient_funds",
    "card_expired",
    "agreement_cancelled",
    "card_not_supported",
    "card_invalid",
    "paypal_error",
    "gateway_issue",
    "payment_revoked",
    "shoppay_not_enabled",
    "not_confirmed_payments",
    "inventory_allocations_not_found",
})

#: PayPilot's decision per Loop failure reason. The decisions are PayPilot's, in
#: the same sense as ``app.recharge_map``: Loop publishes the groups, not which
#: ones a customer can fix. Where the fix is the merchant's (inventory, store or
#: gateway configuration, test mode) the customer is never emailed.
_BY_REASON: dict[str, str] = {
    "Insufficient funds": "insufficient_funds",
    "Card expired": "card_expired",
    "Incorrect card number": "card_details_invalid",
    "Invalid address": "card_details_invalid",
    # Issuer declines: the customer's bank said no, and Loop owns the retry timing.
    "Card declined": "generic_decline",
    "Do not honor": "generic_decline",
    "Amount too large": "generic_decline",
    "Transaction limit exceeded": "generic_decline",
    "PayPal general error": "generic_decline",
    # The customer has to supply or re-authorise a payment method: retrying the
    # same one fails the same way. Loop's own page says of RETRY_DECLINED that
    # "continued retries on this card are unlikely to succeed".
    "3DS authentication failed": "issuer_do_not_retry",
    "PayPal agreement cancelled": "issuer_do_not_retry",
    "Billing agreement cancelled": "issuer_do_not_retry",
    "PayPal agreement invalid": "issuer_do_not_retry",
    "Payment method revoked": "issuer_do_not_retry",
    "Invalid payment method": "issuer_do_not_retry",
    "Card type not supported": "issuer_do_not_retry",
    "Card retry declined": "issuer_do_not_retry",
    "Fraud suspected": "issuer_do_not_retry",
    # Not a payment problem the customer can fix, or too unspecified to email
    # about. An unspecified error may hide a real decline, but emailing a customer
    # about something unnamed is the worse mistake, so it fails safe and loud.
    "Amount too small": NOT_CUSTOMER,
    "Invalid customer": NOT_CUSTOMER,
    "Some error occurred": NOT_CUSTOMER,
    "Invalid gift card amount": NOT_CUSTOMER,
    "Insufficient inventory": NOT_CUSTOMER,
    "Invalid currency": NOT_CUSTOMER,
    "No inventory location found": NOT_CUSTOMER,
    "Invoice already paid": NOT_CUSTOMER,
    "Merchant account error": NOT_CUSTOMER,
    "Gateway test mode error": NOT_CUSTOMER,
    "Payment provider error": NOT_CUSTOMER,
    "Payment provider not enabled": NOT_CUSTOMER,
    "Transient error": NOT_CUSTOMER,
    "Shopify declined": NOT_CUSTOMER,
}

#: Codes whose Loop group would send the wrong email, so the group decision is
#: overridden. A shipping address is not card detail, and ``MERCHANT_RULE`` and
#: ``CONTRACT_UNDER_REVIEW`` are the merchant's or Shopify's review, not the
#: customer's behaviour. The same three are NOT_CUSTOMER in ``app.recharge_map``.
_CODE_OVERRIDES: dict[str, str] = {
    "INVALID_SHIPPING_ADDRESS": NOT_CUSTOMER,
    "MERCHANT_RULE": NOT_CUSTOMER,
    "CONTRACT_UNDER_REVIEW": NOT_CUSTOMER,
}

#: The older lowercase categories, decided the same way.
_LEGACY_DECISIONS: dict[str, str] = {
    "card_declined": "generic_decline",
    "insufficient_funds": "insufficient_funds",
    "card_expired": "card_expired",
    "card_invalid": "card_details_invalid",
    "paypal_error": "generic_decline",
    "agreement_cancelled": "issuer_do_not_retry",
    "card_not_supported": "issuer_do_not_retry",
    "payment_revoked": "issuer_do_not_retry",
    "not_confirmed_payments": "issuer_do_not_retry",
    "gateway_issue": NOT_CUSTOMER,
    "shoppay_not_enabled": NOT_CUSTOMER,
    "inventory_allocations_not_found": NOT_CUSTOMER,
}


def classify_loop_error(error_code: str | None) -> str:
    """PayPilot failure code for a Loop ``paymentAttempt.errorCode``.

    Returns a PayPilot code, or NOT_CUSTOMER, or UNMAPPED. A code outside both
    published tables is UNMAPPED and is not emailed about: an unknown code fails
    safe and loud. Shopify codes are matched case-insensitively and so are the
    older categories, because the documentation's example uses lowercase for a
    field whose new table is upper case and nothing says which arrives.
    """
    raw = (error_code or "").strip()
    if not raw:
        return UNMAPPED
    upper = raw.upper()
    if upper in PUBLISHED_SHOPIFY_CODES:
        if upper in _CODE_OVERRIDES:
            return _CODE_OVERRIDES[upper]
        return _BY_REASON[PUBLISHED_SHOPIFY_CODES[upper]]
    lower = raw.lower()
    if lower in PUBLISHED_LEGACY_CATEGORIES:
        return _LEGACY_DECISIONS[lower]
    return UNMAPPED


def loop_failure_reason(error_code: str | None) -> str | None:
    """Loop's own named failure reason for a Shopify code, or None."""
    return PUBLISHED_SHOPIFY_CODES.get((error_code or "").strip().upper())


def _int_or_none(value: object) -> int | None:
    """A non-negative integer, or None. Booleans are not counts."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def loop_retries_remaining(attempt: dict | None) -> int | None:
    """How many retries Loop says it still owns, or None when it does not say.

    ``retryLeftCount`` is the documented field. None means unknown, which is not
    the same as zero: zero is Loop saying it has stopped, and only then does a
    customer message not duplicate one Loop is about to send.
    """
    if not isinstance(attempt, dict):
        return None
    return _int_or_none(attempt.get("retryLeftCount"))


def _money(value: object) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def loop_order_failed_to_internal(body: dict) -> dict:
    """Translate a Loop ``order/paymentFailed`` body for the graph.

    ``body`` is the delivery as Loop documents it: ``{"payload": {...},
    "metaData": {"myshopifyDomain": ...}}``. Returns the event dict plus
    ``classification``. Every key is read defensively and none is required:
    a body without a failed ``paymentAttempt`` is UNMAPPED rather than guessed
    at, because the topic is not carried in the example body and the shape is the
    only signal there is.

    Customer name, email and phone arrive in this payload. The name and email are
    passed on only because the graph needs them to draft and address the message.
    The phone number, shipping address and the rest of the order are not read.
    """
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    meta = body.get("metaData") if isinstance(body.get("metaData"), dict) else {}
    attempt = payload.get("paymentAttempt") if isinstance(payload.get("paymentAttempt"), dict) else {}
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    subscription = (
        payload.get("subscription") if isinstance(payload.get("subscription"), dict) else {}
    )

    failed = (str(payload.get("status") or "")).strip().upper() == "FAILED"
    error_code = (attempt.get("errorCode") or "").strip() or None
    classification = classify_loop_error(error_code) if (failed and attempt) else UNMAPPED

    first = (customer.get("firstName") or "").strip()
    last = (customer.get("lastName") or "").strip()
    name = " ".join(p for p in (first, last) if p) or None

    lines = payload.get("lineItems") or []
    plan = None
    if lines and isinstance(lines[0], dict):
        plan = (lines[0].get("sellingPlanName") or lines[0].get("productTitle") or "").strip() or None
    if plan is None:
        sub_lines = subscription.get("lineItems") or []
        if sub_lines and isinstance(sub_lines[0], dict):
            plan = (
                sub_lines[0].get("sellingPlanName") or sub_lines[0].get("productTitle") or ""
            ).strip() or None

    attempt_count = _int_or_none(attempt.get("retryAttemptCount"))
    retries_left = loop_retries_remaining(attempt)

    return {
        "classification": classification,
        "error_code": error_code,
        "loop_failure_reason": loop_failure_reason(error_code),
        "event_id": (attempt.get("idempotencyKey") or None),
        "loop_order_id": payload.get("id"),
        "loop_subscription_id": subscription.get("id"),
        "shop": (meta.get("myshopifyDomain") or "").strip().lower() or None,
        "loop_retries_left": retries_left,
        # Carried through unconverted: the unit of retryDelay is not stated.
        "loop_retry_delay": attempt.get("retryDelay"),
        "event": {
            "customer_id": str(customer.get("shopifyId") or ""),
            "amount": _money(payload.get("totalPrice")),
            "currency": (payload.get("currencyCode") or "usd").lower(),
            "failure_code": classification,
            # retryAttemptCount is documented but its base (0 or 1 on the first
            # failure) is not stated, so the floor is 1 rather than a guess.
            "attempt": max(1, attempt_count or 1),
            "customer_name": name,
            "customer_email": customer.get("email") or None,
            "plan": plan,
            "processor": "loop",
        },
    }
