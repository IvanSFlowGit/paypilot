# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Loop Subscriptions mapper: the table is Loop's own, the fixture is Loop's example.

The fixture below is copied in shape from the ``order/paymentFailed`` example in
Loop's webhook guide (help.loopwork.co, read 2026-11-02). It is documentation, not
a delivery: no Loop merchant, Admin API key or webhook exists for this product,
so nothing here shows a live payload arrives as spelled.
"""

from __future__ import annotations

import json

import pytest

from app import loop_subscriptions_map as lm
from app.nodes import STRATEGY_RULES

#: Shape of Loop's documented failed-order example. Customer details are the
#: documentation's own placeholders.
DOC_FAILED_ORDER = {
    "payload": {
        "id": 9876543,
        "status": "FAILED",
        "customer": {
            "email": "customer@example.com",
            "phone": "+44 7890 123456",
            "lastName": "Johnson",
            "firstName": "Emma",
            "shopifyId": 9876543210987,
        },
        "lineItems": [
            {
                "sku": "MEMB001",
                "productTitle": "Exclusive Club Membership",
                "sellingPlanName": "15 per month membership",
            }
        ],
        "totalPrice": "15.00",
        "currencyCode": "GBP",
        "subscription": {"id": 7654321, "lineItems": []},
        "shippingAddress": {"address1": "10 Baker Street", "zip": "EC1A 1BB"},
        "paymentAttempt": {
            "createdAt": "2025-01-09T09:30:00.000Z",
            "errorCode": "insufficient_funds",
            "shopifyId": 765432109876,
            "retryDelay": "2",
            "errorMessage": "Insufficient funds in your account.",
            "idempotencyKey": "7654321-abc123",
            "retryLeftCount": 2,
            "retryAttemptCount": 1,
        },
    },
    "metaData": {"myshopifyDomain": "MembershipStore.myshopify.com"},
}

PAYPILOT_CODES = set(STRATEGY_RULES)


def _body(**attempt):
    body = json.loads(json.dumps(DOC_FAILED_ORDER))
    body["payload"]["paymentAttempt"].update(attempt)
    return body


def test_every_published_shopify_code_has_a_decision():
    for code in lm.PUBLISHED_SHOPIFY_CODES:
        result = lm.classify_loop_error(code)
        assert result != lm.UNMAPPED, code
        assert result == lm.NOT_CUSTOMER or result in PAYPILOT_CODES, (code, result)


#: A SECOND, independent transcription: the "Error messages" lines of the numbered
#: troubleshooting section on Loop's "Payment errors" page, read 2026-11-02, in page
#: order. The module's table was written from the summary table; this list was written
#: from the other half of the same page, so a dropped row in either shows up here.
LOOP_PAGE_ERROR_MESSAGES = (
    "AMOUNT_TOO_LARGE", "AMOUNT_TOO_SMALL",
    "AUTHENTICATION_ERROR", "AUTHENTICATION_FAILED", "AUTHENTICATION_REQUIRED",
    "CONFIRMATION_REJECTED", "3DS_CHALLENGE_EXPIRED_LOOP",
    "BUYER_CANCELED_PAYMENT_METHOD",
    "CALL_ISSUER", "CARD_DECLINED", "PAYMENT_METHOD_DECLINED",
    "CANCELLED_PAYMENT",
    "CARD_NUMBER_INCORRECT", "INCORRECT_NUMBER", "INVALID_NUMBER",
    "CUSTOMER_INVALID", "CUSTOMER_NOT_FOUND",
    "DO_NOT_HONOR",
    "EXPIRED_BUYER_ACTION", "GENERIC_ERROR", "OFF_SESSION_REJECTED", "UNEXPECTED_ERROR",
    "EXPIRED_CARD", "EXPIRED_PAYMENT_METHOD", "INVALID_EXPIRY_DATE",
    "FRAUD_SUSPECTED", "MERCHANT_RULE", "CONTRACT_UNDER_REVIEW",
    "FREE_GIFT_CARD_NOT_ALLOWED",
    "INCORRECT_ADDRESS", "INCORRECT_ZIP", "INVALID_BILLING_ADDRESS", "INVALID_SHIPPING_ADDRESS",
    "INSUFFICIENT_FUNDS", "INSUFFICIENT_INVENTORY", "INVALID_CURRENCY",
    "INVALID_CUSTOMER_BILLING_AGREEMENT", "INVALID_PAYMENT_METHOD",
    "INVALID_PURCHASE_TYPE", "PAYMENT_METHOD_UNSUPPORTED", "PURCHASE_TYPE_NOT_SUPPORTED",
    "INVENTORY_ALLOCATIONS_NOT_FOUND", "INVOICE_ALREADY_PAID", "MERCHANT_ACCOUNT_ERROR",
    "NON_TEST_ORDER_LIMIT_REACHED", "PAYMENT_METHOD_INCOMPATIBLE_WITH_GATEWAY_CONFIG", "TEST_MODE",
    "PAYMENT_METHOD_NOT_FOUND", "PAYMENT_METHOD_NOT_SPECIFIED",
    "PAYMENT_PROVIDER_ERROR", "PROCESSING_ERROR", "PAYMENT_PROVIDER_IS_NOT_ENABLED",
    "PAYPAL_ERROR_GENERAL", "RETRY_DECLINED", "TRANSACTION_LIMIT_EXCEEDED",
    "TRANSIENT_ERROR", "SHOPIFY_DECLINED", "SHOPIFY_NO_CONFIRMATION",
)


def test_table_matches_an_independent_reading_of_the_page():
    assert len(set(LOOP_PAGE_ERROR_MESSAGES)) == len(LOOP_PAGE_ERROR_MESSAGES)
    assert set(lm.PUBLISHED_SHOPIFY_CODES) == set(LOOP_PAGE_ERROR_MESSAGES)


def test_every_loop_failure_reason_is_decided():
    reasons = set(lm.PUBLISHED_SHOPIFY_CODES.values())
    assert reasons <= set(lm._BY_REASON), reasons - set(lm._BY_REASON)
    assert set(lm._BY_REASON) <= reasons, "a decision exists for a reason Loop never publishes"


def test_every_legacy_category_is_decided():
    assert set(lm._LEGACY_DECISIONS) == set(lm.PUBLISHED_LEGACY_CATEGORIES)
    for cat in lm.PUBLISHED_LEGACY_CATEGORIES:
        result = lm.classify_loop_error(cat)
        assert result == lm.NOT_CUSTOMER or result in PAYPILOT_CODES, (cat, result)


def test_decisions_target_codes_paypilot_actually_has():
    targets = set(lm._BY_REASON.values()) | set(lm._LEGACY_DECISIONS.values())
    targets |= set(lm._CODE_OVERRIDES.values())
    assert targets - {lm.NOT_CUSTOMER} <= PAYPILOT_CODES


def test_code_matching_is_case_insensitive_in_both_directions():
    assert lm.classify_loop_error("INSUFFICIENT_FUNDS") == "insufficient_funds"
    assert lm.classify_loop_error("insufficient_funds") == "insufficient_funds"
    assert lm.classify_loop_error("  Expired_Card ") == "card_expired"
    assert lm.classify_loop_error("card_expired") == "card_expired"


@pytest.mark.parametrize("value", [None, "", "   ", "NOT_A_LOOP_CODE", "card declined"])
def test_unknown_or_empty_code_is_unmapped(value):
    assert lm.classify_loop_error(value) == lm.UNMAPPED


def test_overrides_split_a_group_where_the_group_would_send_the_wrong_email():
    # Same Loop group ("Invalid address"), opposite decisions.
    assert lm.classify_loop_error("INVALID_BILLING_ADDRESS") == "card_details_invalid"
    assert lm.classify_loop_error("INVALID_SHIPPING_ADDRESS") == lm.NOT_CUSTOMER
    # Same Loop group ("Fraud suspected"), opposite decisions.
    assert lm.classify_loop_error("FRAUD_SUSPECTED") == "issuer_do_not_retry"
    assert lm.classify_loop_error("MERCHANT_RULE") == lm.NOT_CUSTOMER
    assert lm.classify_loop_error("CONTRACT_UNDER_REVIEW") == lm.NOT_CUSTOMER


def test_store_side_failures_never_email_the_customer():
    for code in (
        "INSUFFICIENT_INVENTORY",
        "INVENTORY_ALLOCATIONS_NOT_FOUND",
        "MERCHANT_ACCOUNT_ERROR",
        "TEST_MODE",
        "PAYMENT_PROVIDER_IS_NOT_ENABLED",
        "TRANSIENT_ERROR",
        "UNEXPECTED_ERROR",
        "gateway_issue",
    ):
        assert lm.classify_loop_error(code) == lm.NOT_CUSTOMER, code


def test_a_retry_declined_card_needs_a_new_method():
    assert lm.classify_loop_error("RETRY_DECLINED") == "issuer_do_not_retry"


def test_doc_example_translates():
    out = lm.loop_order_failed_to_internal(DOC_FAILED_ORDER)
    assert out["classification"] == "insufficient_funds"
    assert out["error_code"] == "insufficient_funds"
    assert out["event_id"] == "7654321-abc123"
    assert out["loop_order_id"] == 9876543
    assert out["loop_subscription_id"] == 7654321
    assert out["shop"] == "membershipstore.myshopify.com"
    assert out["loop_retries_left"] == 2
    assert out["loop_retry_delay"] == "2"
    ev = out["event"]
    assert ev["failure_code"] == "insufficient_funds"
    assert ev["processor"] == "loop"
    assert ev["amount"] == 15.0
    assert ev["currency"] == "gbp"
    assert ev["attempt"] == 1
    assert ev["customer_id"] == "9876543210987"
    assert ev["customer_name"] == "Emma Johnson"
    assert ev["plan"] == "15 per month membership"


def test_phone_and_address_never_leave_the_mapper():
    blob = json.dumps(lm.loop_order_failed_to_internal(DOC_FAILED_ORDER))
    assert "7890" not in blob and "123456" not in blob  # phone digits
    assert "Baker" not in blob and "EC1A" not in blob  # address


def test_a_body_that_is_not_a_failed_order_is_unmapped():
    ok = json.loads(json.dumps(DOC_FAILED_ORDER))
    ok["payload"]["status"] = "SUCCESS"
    assert lm.loop_order_failed_to_internal(ok)["classification"] == lm.UNMAPPED
    no_attempt = json.loads(json.dumps(DOC_FAILED_ORDER))
    del no_attempt["payload"]["paymentAttempt"]
    assert lm.loop_order_failed_to_internal(no_attempt)["classification"] == lm.UNMAPPED
    for junk in ({}, {"payload": None}, {"payload": "x"}, {"payload": {"status": "FAILED"}}):
        assert lm.loop_order_failed_to_internal(junk)["classification"] == lm.UNMAPPED


def test_unknown_code_in_a_failed_order_is_unmapped_not_guessed():
    out = lm.loop_order_failed_to_internal(_body(errorCode="BRAND_NEW_SHOPIFY_CODE"))
    assert out["classification"] == lm.UNMAPPED
    assert out["event"]["failure_code"] == lm.UNMAPPED


def test_retries_remaining_distinguishes_unknown_from_zero():
    assert lm.loop_retries_remaining({"retryLeftCount": 0}) == 0
    assert lm.loop_retries_remaining({"retryLeftCount": 3}) == 3
    assert lm.loop_retries_remaining({"retryLeftCount": "2"}) == 2
    assert lm.loop_retries_remaining({}) is None
    assert lm.loop_retries_remaining(None) is None
    assert lm.loop_retries_remaining({"retryLeftCount": True}) is None
    assert lm.loop_retries_remaining({"retryLeftCount": -1}) is None
    assert lm.loop_retries_remaining({"retryLeftCount": "soon"}) is None


def test_attempt_floor_is_one():
    assert lm.loop_order_failed_to_internal(_body(retryAttemptCount=0))["event"]["attempt"] == 1
    assert lm.loop_order_failed_to_internal(_body(retryAttemptCount=3))["event"]["attempt"] == 3
    missing = _body()
    del missing["payload"]["paymentAttempt"]["retryAttemptCount"]
    assert lm.loop_order_failed_to_internal(missing)["event"]["attempt"] == 1


def test_amount_and_currency_tolerate_garbage():
    body = json.loads(json.dumps(DOC_FAILED_ORDER))
    body["payload"]["totalPrice"] = "not a number"
    del body["payload"]["currencyCode"]
    ev = lm.loop_order_failed_to_internal(body)["event"]
    assert ev["amount"] == 0.0
    assert ev["currency"] == "usd"


def test_legacy_categories_match_case_insensitively():
    for cat in lm.PUBLISHED_LEGACY_CATEGORIES:
        assert lm.classify_loop_error(cat.upper()) == lm.classify_loop_error(cat)


def test_negative_retry_attempt_count_floors_to_one():
    import copy

    body = copy.deepcopy(DOC_FAILED_ORDER)
    body["payload"]["paymentAttempt"]["retryAttemptCount"] = -3
    assert lm.loop_order_failed_to_internal(body)["event"]["attempt"] == 1
