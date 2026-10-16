# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Speak Chargebee: authenticate and translate failed payment events.

Chargebee bills subscriptions and, like Recharge, runs its own dunning. PayPilot
does not compete with that schedule: when Chargebee will retry, Chargebee's
``next_retry_at`` is reported as the schedule and the customer email is held.

THIS MODULE HAS NEVER SEEN A LIVE CHARGEBEE PAYLOAD. Every fixture under
``tests/`` is synthetic and written from the documentation cited below. No
Chargebee site, API key or webhook endpoint exists for this product, so nothing
here is evidence that the field names arrive as spelled. It is written from the
published attribute names and closed enums, which is the best available
evidence and is not the same thing as a delivery.

TWO FINDINGS DECIDE THE WHOLE DESIGN, AND BOTH DIFFER FROM STRIPE AND RECHARGE.

FIRST, THERE IS NO SIGNATURE. Stripe signs with an HMAC and Recharge with a
prefixed SHA-256, so both can be verified from the bytes. Chargebee publishes
neither. Its own webhook settings page (chargebee.com/docs/2.0/webhook_settings
.html, read 2026-10-16) says only, verbatim, "It is recommended that your
webhook URLs be protected using basic authentication and/or HTTPS", and calls a
key in the URL "Another (less secure) way of authenticating a webhook call".
So :func:`verify_chargebee_shared_secret` compares a secret the receiver chose,
in constant time, and that is ALL it can prove: that the caller knew the
secret. It does not prove the body is intact, and it cannot prove the delivery
is fresh, because there is nothing signed over the payload to tamper with. Any
sentence claiming PayPilot verifies Chargebee webhooks cryptographically is
false, and that is a property of Chargebee rather than of this code.

The same page says duplicates and reordering are expected rather than
exceptional: "Due to webhook retries, it's possible that your application
receives the same webhook more than once", with up to 7 retries, and "Webhooks
can also arrive at your application out-of-order". So the event ``id`` is the
idempotency key (Chargebee's own advice) and ``resource_version`` is the
ordering key, and :func:`chargebee_event_to_internal` carries both out so the
route and the audit trail can use them rather than re-deriving them.

SECOND, CHARGEBEE PUBLISHES NO ERROR VOCABULARY OF ITS OWN. A transaction's
``error_code`` is, verbatim, the "Error code received from the payment gateway
on failure" (apidocs.chargebee.com/docs/api/transactions, read 2026-10-16), so
the namespace belongs to whichever gateway the merchant connected: Stripe's
codes on Stripe, Adyen's on Adyen. There is therefore NO Chargebee error table
to transcribe, and writing one would be inventing a vocabulary the vendor does
not publish. Classification keys instead on the two closed enums Chargebee DOES
publish, ``transaction.status`` and ``invoice.dunning_status``, and the gateway
code is mapped only where PayPilot already owns that gateway's table, which
today means Stripe alone via :func:`app.stripe_map.failure_code_for_decline_code`.
Every other gateway's code is carried through unmapped rather than guessed at.

Three pure pieces so the webhook route stays thin and testable with no network:

* :func:`verify_chargebee_shared_secret` - constant-time compare, fails closed.
* :func:`classify_chargebee_failure` - one decision per published status.
* :func:`chargebee_event_to_internal` - the flat event dict the graph consumes.

Sources, all read 2026-10-16:
apidocs.chargebee.com/docs/api/events (the envelope: id, occurred_at,
event_type, content, source, api_version, and content "Has resources
(subscription, invoice etc) based on the event type");
apidocs.chargebee.com/docs/api/events/event-types (the event_type strings);
apidocs.chargebee.com/docs/api/transactions (status enum, error_code,
error_text, gateway);
apidocs.chargebee.com/docs/api/invoices (status and dunning_status enums,
next_retry_at, amount_due);
chargebee.com/docs/2.0/dunning.html (Chargebee retries and emails the customer
itself: "When Auto collection is turned ON for a subscription and the initial
attempt to collect the payment fails, Chargebee will retry to collect the
payment and send emails to your customer", up to 4 attempts).

KNOWN GAP, stated rather than papered over: the exact set of resource keys
inside ``content`` for ``payment_failed`` is NOT established first hand. The
events page says only that content carries resources based on the event type
and shows no sample. So every reader here is defensive, no key is required, and
an event whose content lacks a transaction classifies as UNMAPPED rather than
being guessed at. Settled by one real delivery, or by the sample in
Chargebee's own API Playground.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime

from app.stripe_map import failure_code_for_decline_code

#: The route acknowledges these without emailing the customer.
NOT_CUSTOMER = "not_customer"
UNMAPPED = "unmapped"

#: Chargebee ``event_type`` values this adapter acts on, with the published
#: description. Kept as data so a test can assert the route and this module
#: agree on the set rather than two lists drifting.
PUBLISHED_FAILURE_EVENTS: dict[str, str] = {
    "payment_failed": "Triggered when the payment collection fails.",
}

#: Event types that are recovery-relevant but are NOT a failure: a closing
#: event, or a state change caused by non-payment. The route reads these to
#: close a case rather than to open one.
PUBLISHED_CLOSING_EVENTS: dict[str, str] = {
    "payment_succeeded": "Triggered when the payment is successfully collected.",
    "subscription_cancelled": (
        "Triggered when the subscription is cancelled. If it is cancelled due to "
        "non payment or because the card details are not present, the subscription "
        "will have the possible reason as 'cancel_reason'."
    ),
}

#: Every ``transaction.status`` Chargebee publishes, as
#: (the customer has to act, PayPilot has copy for it), transcribed from the
#: transactions page named above. A gateway that cannot be reached is not the
#: customer's problem and they must never be emailed about it.
PUBLISHED_TRANSACTION_STATUSES: dict[str, tuple[bool, str]] = {
    # "Transaction failed. Refer the 'error_code' and 'error_text' fields to
    # know the reason" - the only status whose reason is the payment itself.
    "failure": (True, "generic_decline"),
    # "Transaction failed because of Gateway not accepting the connection".
    # Nothing about the customer's card is implicated.
    "timeout": (False, NOT_CUSTOMER),
    # "When connection with the Gateway gets terminated abruptly". Same.
    "needs_attention": (False, NOT_CUSTOMER),
    # "Indicates that a successful payment transaction has failed now due to a
    # late failure notification". The money went and then came back, which is
    # the case a retry schedule is least likely to cover.
    "late_failure": (True, "generic_decline"),
    "success": (False, NOT_CUSTOMER),
    "voided": (False, NOT_CUSTOMER),
    "in_progress": (False, NOT_CUSTOMER),
}

#: Every ``invoice.dunning_status`` Chargebee publishes, with its description.
PUBLISHED_DUNNING_STATUSES: dict[str, str] = {
    "in_progress": "Dunning is still in progress.",
    "exhausted": "Maximum number of attempts have been made.",
    "stopped": "Dunning has stopped for this invoice.",
    "success": "Payment successfully collected during dunning process.",
}


def verify_chargebee_shared_secret(provided: str | None, expected: str | None) -> bool:
    """Constant-time compare of the shared secret a Chargebee webhook carries.

    This is NOT signature verification and must never be described as such.
    Chargebee signs nothing, so the strongest check available to a receiver is
    that the caller knew a secret the receiver itself generated, carried either
    in basic auth or in the URL. It says nothing about the body.

    Fails closed on an unset or empty expected value, because an empty secret
    compared against an empty header would otherwise authenticate the whole
    internet. Both sides are compared with :func:`hmac.compare_digest` so the
    answer leaks no timing, and a non-ASCII secret is encoded rather than
    raising.
    """
    want = (expected or "").strip()
    got = (provided or "").strip()
    if not want or not got:
        return False
    return hmac.compare_digest(want.encode("utf-8"), got.encode("utf-8"))


def _gateway_code_failure(transaction: dict) -> str | None:
    """PayPilot failure code from the GATEWAY's own error code, or None.

    Only a gateway whose vocabulary PayPilot already owns is read. Chargebee
    names the gateway in ``transaction.gateway``, so this refuses to guess for
    anything else rather than applying Stripe's table to Adyen's codes, which
    collide: both publish short lowercase strings and neither reserves the
    other's namespace.
    """
    if (transaction.get("gateway") or "").strip().lower() != "stripe":
        return None
    return failure_code_for_decline_code(transaction.get("error_code"))


def classify_chargebee_failure(transaction: dict, invoice: dict | None = None) -> str:
    """PayPilot failure code for a Chargebee transaction, or NOT_CUSTOMER / UNMAPPED.

    Order, and each step is a published fact rather than a preference:

    1. A status outside the published enum is UNMAPPED. An unknown status fails
       safe and loud, and the route does not email anybody about it.
    2. A status that is not a payment problem is NOT_CUSTOMER. The gateway
       dropping the connection is not the customer's card.
    3. Dunning ``exhausted`` outranks the gateway's code, because Chargebee has
       stopped trying: "Maximum number of attempts have been made." Nobody else
       is going to collect this, so the customer has to supply a new method
       whatever the last decline said.
    4. The gateway's own code, where PayPilot owns that gateway's table.
    5. ``generic_decline`` last, which is what Chargebee's own enum supports
       without reading somebody else's error namespace.
    """
    status = (transaction.get("status") or "").strip().lower()
    if not status or status not in PUBLISHED_TRANSACTION_STATUSES:
        return UNMAPPED

    customer_must_act, fallback = PUBLISHED_TRANSACTION_STATUSES[status]
    if not customer_must_act:
        return NOT_CUSTOMER

    inv = invoice if isinstance(invoice, dict) else {}
    if (inv.get("dunning_status") or "").strip().lower() == "exhausted":
        return "retries_exhausted"

    return _gateway_code_failure(transaction) or fallback


def chargebee_next_retry(invoice: dict | None) -> str | None:
    """Chargebee's own next retry instant for this invoice, as ISO 8601 UTC.

    ``next_retry_at`` is, verbatim, the "Timestamp indicating when will the next
    attempt to collect payment for this invoice occur", and Chargebee sends its
    own customer emails while dunning is running. So when it is present AND
    dunning is in progress, Chargebee owns both the schedule and the
    correspondence, and a PayPilot message now would duplicate one the customer
    is already getting.

    Returns None where dunning is not in progress, because a stale
    ``next_retry_at`` left on an exhausted invoice would otherwise read as a
    promise nobody is going to keep.
    """
    inv = invoice if isinstance(invoice, dict) else {}
    if (inv.get("dunning_status") or "").strip().lower() != "in_progress":
        return None
    raw = inv.get("next_retry_at")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _minor_to_major(value: object) -> float:
    """Chargebee amounts are in the currency's minor unit ("in cents")."""
    try:
        return round(int(value) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


def chargebee_event_to_internal(event: dict) -> dict:
    """Translate a Chargebee event envelope for the graph.

    Returns the event dict plus ``classification``, the event ``id`` for
    idempotency and ``resource_version`` for ordering, both of which Chargebee's
    own docs name as the receiver's defence against the duplicate and
    out-of-order deliveries it says to expect.

    Every content key is optional, because the exact resource set for
    ``payment_failed`` is not established first hand. No transaction means
    UNMAPPED rather than a guess.
    """
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    transaction = content.get("transaction") if isinstance(content.get("transaction"), dict) else {}
    invoice = content.get("invoice") if isinstance(content.get("invoice"), dict) else {}
    customer = content.get("customer") if isinstance(content.get("customer"), dict) else {}
    subscription = (
        content.get("subscription") if isinstance(content.get("subscription"), dict) else {}
    )

    classification = classify_chargebee_failure(transaction, invoice) if transaction else UNMAPPED

    first = (customer.get("first_name") or "").strip()
    last = (customer.get("last_name") or "").strip()
    name = " ".join(p for p in (first, last) if p) or None

    # The invoice's unpaid amount is what the customer owes; the transaction
    # amount is what one attempt tried to take. Prefer the debt, because that
    # is the figure a customer-facing message is about.
    amount = _minor_to_major(invoice.get("amount_due")) or _minor_to_major(
        transaction.get("amount")
    )
    currency = (
        invoice.get("currency_code") or transaction.get("currency_code") or "usd"
    ).lower()

    customer_id = customer.get("id") or transaction.get("customer_id") or ""
    retry = chargebee_next_retry(invoice)

    return {
        "classification": classification,
        "event_id": event.get("id") or None,
        "event_type": (event.get("event_type") or "").strip().lower() or None,
        "resource_version": invoice.get("resource_version")
        or subscription.get("resource_version"),
        "gateway": (transaction.get("gateway") or "").strip().lower() or None,
        "gateway_error_code": (transaction.get("error_code") or "").strip() or None,
        "transaction_status": (transaction.get("status") or "").strip().lower() or None,
        "dunning_status": (invoice.get("dunning_status") or "").strip().lower() or None,
        "event": {
            "customer_id": str(customer_id),
            "amount": amount,
            "currency": currency,
            "failure_code": classification,
            # Chargebee counts attempts on the INVOICE, not the transaction.
            "attempt": int(invoice.get("attempt_count") or 1),
            "customer_name": name,
            "customer_email": customer.get("email") or None,
            "plan": (subscription.get("plan_id") or "").strip() or None,
            "processor": "chargebee",
            "chargebee_retry_date": retry,
        },
    }
