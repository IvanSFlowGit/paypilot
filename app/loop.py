"""The closed recovery loop: Stripe events in, recorded state out.

``app/api.py`` owns HTTP concerns (signature, rate limit, response shape) and
this module owns what an event *means* to the recovery state machine, so the
meaning is unit-testable without a web server.

Four Stripe event types close the loop:

* ``invoice.payment_failed`` opens it - persist the failure, run the recovery
  graph, and hand the draft back.
* ``invoice.paid`` / ``invoice.payment_succeeded`` close it as a recovery.
* ``customer.subscription.deleted`` closes it as churn.

Two rules run through all of it:

**An event for an invoice we never saw fail is not ours.** Most invoices in a
Stripe account get paid without ever failing. Counting those as recoveries
would inflate the number that the whole product is judged on, so an unmatched
invoice is acknowledged and ignored, never invented.

**Nothing here raises at the caller.** Stripe reads a non-2xx as "retry me",
so a permanent problem (an event we cannot match) must be acknowledged rather
than retried forever. Genuine faults still propagate; refusals do not.
"""

from __future__ import annotations

from app.graph import run_recovery
from app.store import UnknownInvoice, get_store
from app.stripe_map import (
    stripe_closing_event,
    stripe_event_to_failure,
    stripe_event_to_internal,
)
from app.store import STATE_CHURNED, STATE_RECOVERED

EVENT_PAYMENT_FAILED = "invoice.payment_failed"
EVENT_INVOICE_PAID = "invoice.paid"
EVENT_PAYMENT_SUCCEEDED = "invoice.payment_succeeded"
EVENT_SUBSCRIPTION_DELETED = "customer.subscription.deleted"

#: The exact set to subscribe the Stripe webhook destination to. Anything else
#: is acknowledged with a 200 and dropped.
HANDLED_EVENT_TYPES = frozenset(
    {
        EVENT_PAYMENT_FAILED,
        EVENT_INVOICE_PAID,
        EVENT_PAYMENT_SUCCEEDED,
        EVENT_SUBSCRIPTION_DELETED,
    }
)

_RECOVERY_EVENTS = frozenset({EVENT_INVOICE_PAID, EVENT_PAYMENT_SUCCEEDED})


def handle_payment_failed(event: dict, store=None) -> dict:
    """Persist a failed invoice and run the recovery graph over it."""
    store = store or get_store()
    row = stripe_event_to_failure(event)

    invoice_id = row["invoice_id"]
    if not invoice_id:
        # Without an invoice id there is nothing to close the loop against
        # later, so recording it would create a failure that can never be
        # resolved and would sit in the denominator of every rate forever.
        return {
            "handled": False,
            "reason": "missing_invoice_id",
            "invoice_id": None,
            "recovery": None,
        }

    store.record_failure(
        invoice_id=invoice_id,
        customer_id=row["customer_id"],
        amount_minor=row["amount_minor"],
        currency=row["currency"],
        failure_code=row["failure_code"],
        attempt_count=row["attempt_count"],
        stripe_customer_id=row["stripe_customer_id"],
        subscription_id=row["subscription_id"],
    )

    # The graph still receives the decimal-amount shape it was built around.
    # Delivery, and the failed -> messaged transition that goes with it, is not
    # wired yet: the draft is produced and stored state stays "failed".
    recovery = run_recovery(stripe_event_to_internal(event))
    return {
        "handled": True,
        "invoice_id": invoice_id,
        "state": store.get_failure(invoice_id)["state"],
        "recovery": recovery,
    }


def handle_recovery(event: dict, store=None) -> dict:
    """Close an invoice as recovered when Stripe reports it paid."""
    store = store or get_store()
    closing = stripe_closing_event(event)
    invoice_id = closing["invoice_id"]

    if not invoice_id or store.get_failure(invoice_id) is None:
        # A normal paid invoice that never failed. Not a recovery, and counting
        # it as one would inflate the headline metric.
        return {
            "handled": False,
            "reason": "no_matching_failure",
            "invoice_id": invoice_id or None,
        }

    changed = store.transition(
        invoice_id,
        STATE_RECOVERED,
        reason=event.get("type"),
        recovered_amount_minor=closing["amount_minor"],
    )
    return {
        "handled": True,
        "invoice_id": invoice_id,
        "state": STATE_RECOVERED,
        "changed": changed,
        "recovered_amount_minor": closing["amount_minor"],
        "currency": closing["currency"],
    }


def handle_churn(event: dict, store=None) -> dict:
    """Close every still-open invoice for a cancelled subscription as churn."""
    store = store or get_store()
    closing = stripe_closing_event(event)

    # Prefer the subscription: a customer may hold several subscriptions, and
    # cancelling one should not churn invoices belonging to another. Fall back
    # to the customer only when no invoice carries the subscription id (Stripe
    # does not always expand it on the invoice).
    open_rows = []
    if closing["subscription_id"]:
        open_rows = store.open_failures(subscription_id=closing["subscription_id"])
    if not open_rows and closing["stripe_customer_id"]:
        open_rows = store.open_failures(stripe_customer_id=closing["stripe_customer_id"])

    if not open_rows:
        return {
            "handled": False,
            "reason": "no_open_failures",
            "subscription_id": closing["subscription_id"],
        }

    churned = []
    for row in open_rows:
        try:
            store.transition(row["invoice_id"], STATE_CHURNED, reason=event.get("type"))
        except UnknownInvoice:  # pragma: no cover - row came from this store
            continue
        churned.append(row["invoice_id"])

    return {
        "handled": True,
        "state": STATE_CHURNED,
        "subscription_id": closing["subscription_id"],
        "churned_invoice_ids": churned,
    }


def handle_event(event: dict, store=None) -> dict:
    """Dispatch one Stripe event to the right handler.

    Returns ``{"handled": False, "reason": "unhandled_event_type"}`` for
    anything outside :data:`HANDLED_EVENT_TYPES` - acknowledged, not an error,
    so Stripe stops redelivering it.
    """
    event_type = event.get("type") or ""
    if event_type == EVENT_PAYMENT_FAILED:
        return handle_payment_failed(event, store)
    if event_type in _RECOVERY_EVENTS:
        return handle_recovery(event, store)
    if event_type == EVENT_SUBSCRIPTION_DELETED:
        return handle_churn(event, store)
    return {"handled": False, "reason": "unhandled_event_type", "type": event_type}
