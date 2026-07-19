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

import os
from datetime import UTC, datetime

from app import templates
from app.attribution import is_holdout
from app.audit import audit_security_event
from app.graph import run_recovery
from app.mailer import STATUS_SENT, send_dunning_email
from app.safety import PAYMENT_UPDATE_URL, message_violations
from app.store import (
    STATE_CHURNED,
    STATE_FAILED,
    STATE_MESSAGED,
    STATE_RECOVERED,
    UnknownInvoice,
    get_store,
)
from app.stripe_client import recovery_link
from app.stripe_map import (
    stripe_closing_event,
    stripe_event_to_failure,
    stripe_event_to_internal,
)

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
        holdout=is_holdout(invoice_id),
    )

    # Read the arm back from the ledger rather than recomputing it. A repeat
    # failure on the same invoice must keep its original assignment even if
    # PAYPILOT_HOLDOUT_PCT changed in between, or an invoice could be dunned on
    # one attempt and withheld on the next, belonging to neither arm.
    if store.get_failure(invoice_id)["holdout"]:
        return {
            "handled": True,
            "invoice_id": invoice_id,
            "state": STATE_FAILED,
            "recovery": None,
            "delivery": {"status": "holdout", "reason": "control_arm"},
        }

    # The graph still receives the decimal-amount shape it was built around.
    recovery = run_recovery(stripe_event_to_internal(event))

    delivery = deliver_recovery(
        invoice_id=invoice_id,
        recovery=recovery,
        row=row,
        store=store,
    )

    return {
        "handled": True,
        "invoice_id": invoice_id,
        "state": store.get_failure(invoice_id)["state"],
        "recovery": recovery,
        "delivery": delivery,
    }


def subject_for(failure_code: str) -> str:
    """Subject line from the committed copy library (no inference)."""
    return templates.get("subject", failure_code)


def compose_email_body(message: str, link: str) -> str:
    """Attach the recovery link to the drafted body.

    The drafted copy deliberately carries no URL - the templates and the model
    prompt both forbid one - so the single sanctioned link is appended here,
    where we know which link was actually minted for this invoice.
    """
    return f"{message}\n\nUpdate your payment details here:\n{link}"


def max_touches() -> int:
    """How many dunning emails one invoice may ever receive."""
    try:
        return max(1, int(os.getenv("PAYPILOT_MAX_TOUCHES", "3")))
    except ValueError:
        return 3


def send_cooldown_hours() -> float:
    """Minimum gap between two dunning emails to the same person.

    Set to 0 to disable, which the demo script does so a walkthrough is not
    blocked by a previous run.
    """
    try:
        return max(0.0, float(os.getenv("PAYPILOT_SEND_COOLDOWN_HOURS", "24")))
    except ValueError:
        return 24.0


def _cooldown_remaining(store, *, invoice_id: str, stripe_customer_id: str | None) -> float:
    """Hours left on the suppression window, or 0 when clear to send.

    Two layers of deduplication, answering two different questions:

    * **Transport idempotency** (``events`` table, ``Idempotency-Key``) asks
      "have I processed this exact delivery already?" It stops a redelivered
      webhook doing the work twice.
    * **This** asks "have I already emailed this human about this recently?"
      Nothing above catches that. Stripe emits a separate, legitimate,
      non-duplicate ``invoice.payment_failed`` for every retry attempt, and a
      customer with several failed invoices in one billing run generates
      several distinct events. Without a cooldown each one is a separate email
      to the same person within minutes.

    Keyed on the invoice AND the customer: the invoice window stops repeated
    nudges about one charge, and the customer window stops a billing run
    blasting somebody three times at once.
    """
    hours = send_cooldown_hours()
    if hours <= 0:
        return 0.0
    last = store.last_send_at(invoice_id=invoice_id, stripe_customer_id=stripe_customer_id)
    if not last:
        return 0.0
    try:
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() / 3600
    except (TypeError, ValueError):
        return 0.0  # unparseable timestamp must not block a legitimate send
    return max(0.0, round(hours - elapsed, 2))


def deliver_recovery(*, invoice_id: str, recovery: dict, row: dict, store) -> dict:
    """Mint a recovery link, guard the finished email, and hand it to the mailer.

    The state only advances to ``messaged`` on a real send. A dry run, a
    suppressed recipient, or a provider failure all leave the invoice at
    ``failed``, because claiming we messaged someone we did not is exactly the
    kind of flattery this ledger exists to prevent.
    """
    # Sequence cap, checked before anything is composed or sent. Stripe emits
    # invoice.payment_failed once per retry attempt, each with its own event id,
    # so event dedupe does not bound this: without a cap, an invoice Stripe
    # retries five times gets five emails to the same person.
    already_sent = store.sent_message_count(invoice_id)
    if already_sent >= max_touches():
        store.record_message(
            invoice_id=invoice_id, status="suppressed", error="max_touches_reached"
        )
        return {
            "status": "suppressed",
            "error": "max_touches_reached",
            "sent_count": already_sent,
        }

    # Business-level dedup, distinct from the transport idempotency above.
    remaining = _cooldown_remaining(
        store, invoice_id=invoice_id, stripe_customer_id=row.get("stripe_customer_id")
    )
    if remaining > 0:
        store.record_message(
            invoice_id=invoice_id, status="suppressed", error="cooldown_active"
        )
        return {
            "status": "suppressed",
            "error": "cooldown_active",
            "hours_remaining": remaining,
        }

    link = recovery_link(
        stripe_customer_id=row.get("stripe_customer_id"),
        hosted_invoice_url=row.get("hosted_invoice_url"),
    )
    body = compose_email_body(recovery.get("message", ""), link)

    # Final gate, on the exact text about to leave the building, allowing only
    # the link we just minted. Stricter than the host allowlist: it permits
    # this URL, not any URL that happens to sit on an allowed host.
    violations = message_violations(body, (link, PAYMENT_UPDATE_URL), allow_hosts=False)
    if violations:
        audit_security_event(
            event="outbound_email_blocked",
            detail=f"invoice {invoice_id}: composed email failed output guards",
            severity="error",
        )
        store.record_message(
            invoice_id=invoice_id, status="suppressed", error="failed_output_guard"
        )
        return {"status": "suppressed", "error": "failed_output_guard", "link": link}

    recipient = row.get("customer_email") or _local_customer_email(row.get("customer_id"))
    if not recipient:
        store.record_message(
            invoice_id=invoice_id, status="suppressed", error="no_recipient_address"
        )
        return {"status": "suppressed", "error": "no_recipient_address", "link": link}

    subject = subject_for(row.get("failure_code", ""))
    result = send_dunning_email(
        invoice_id=invoice_id, to=recipient, subject=subject, body=body, store=store
    )

    if result["status"] == STATUS_SENT:
        store.transition(invoice_id, STATE_MESSAGED, reason="dunning_email_sent")

    return {**result, "link": link}


def _local_customer_email(customer_id: str | None) -> str | None:
    """Fall back to the demo customer file when Stripe carries no email."""
    if not customer_id:
        return None
    from app.nodes import _load_customer

    return (_load_customer(customer_id) or {}).get("email")


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
