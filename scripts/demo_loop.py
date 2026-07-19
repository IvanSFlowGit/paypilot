"""End-to-end proof: a real Stripe invoice fails, gets dunned, and recovers.

Run with ``make demo-loop``. It drives Stripe test mode through the whole cycle
and prints the dashboard before and after, so the claim "we recovered EUR 49"
is something you can watch happen rather than something a slide asserts.

The cycle:

1. create a test clock, a customer on it, and a subscription
2. attach a card that ATTACHES fine but always FAILS to charge
3. advance the clock a month, so Stripe genuinely tries and fails the renewal
4. feed the real ``invoice.payment_failed`` event through the recovery loop -
   it records the failure, drafts the dunning copy, and delivers it
5. swap in a card that succeeds (what the customer does at the portal link)
6. pay the invoice, and feed the real ``invoice.paid`` event back through
7. print the dashboard again

Two deliberate choices:

**Test PaymentMethod tokens, never raw card numbers.** PayPilot never handles
card data, and a demo that did would contradict the product's own claim.
``pm_card_chargeCustomerFail`` is Stripe's token for 4000000000000341.

**Events come from Stripe's API, not a tunnel.** The script fetches the real
event objects Stripe emitted and passes them to the same ``handle_event`` the
webhook calls. That keeps the proof runnable without ngrok or a deployed URL,
and the events are genuinely Stripe's. Signature verification is the one part
this path does not exercise; ``tests/test_closed_loop.py`` covers that.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.loop import handle_event  # noqa: E402
from app.report import build_report  # noqa: E402
from app.store import Store, reset_store  # noqa: E402

CARD_ALWAYS_FAILS = "pm_card_chargeCustomerFail"  # 4000000000000341
CARD_SUCCEEDS = "pm_card_visa"                    # 4242424242424242

MONTH_SECONDS = 31 * 24 * 60 * 60


def _fail(message: str) -> None:
    print(f"\nSTOPPED: {message}", file=sys.stderr)
    raise SystemExit(1)


def _require_test_mode():
    """Refuse to run against a live key.

    This script creates customers, subscriptions and charges. Pointed at a live
    account it would bill real people. The guard is a hard stop, not a warning,
    and it checks the key prefix rather than trusting an env name like "staging".
    """
    key = (os.getenv("STRIPE_API_KEY") or os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        _fail(
            "STRIPE_API_KEY is not set. Use a TEST mode key (sk_test_...).\n"
            "Find it at dashboard.stripe.com in a sandbox, then put it in .env."
        )
    if not (key.startswith("sk_test_") or key.startswith("rk_test_")):
        _fail(
            "STRIPE_API_KEY is not a test-mode key. This script creates "
            "subscriptions and charges customers; it will not run against live.\n"
            "Test keys start with sk_test_ or rk_test_."
        )
    import stripe

    stripe.api_key = key
    return stripe


def _step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}", flush=True)


def _print_report(store, label: str) -> dict:
    report = build_report(store)
    totals = report["totals"]
    print(f"\n--- dashboard: {label} ---")
    print(
        f"  failed {totals['failed']}  messaged {totals['messaged']}  "
        f"recovered {totals['recovered']}  churned {totals['churned']}"
    )
    for code, bucket in sorted(report["by_currency"].items()):
        print(
            f"  {code.upper()}: at risk {bucket['failed_value']:,.2f}  "
            f"recovered {bucket['recovered_value']:,.2f}"
        )
    if not report["by_currency"]:
        print("  (no invoices recorded)")
    return report


def _latest_event(stripe, event_type: str, invoice_id: str, tries: int = 12):
    """Poll Stripe for the real event it emitted for this invoice.

    Events are not always queryable the instant an API call returns, so this
    retries rather than racing.
    """
    for _ in range(tries):
        for event in stripe.Event.list(type=event_type, limit=25).auto_paging_iter():
            obj = (event.get("data") or {}).get("object") or {}
            if obj.get("id") == invoice_id:
                return json.loads(json.dumps(event, default=str))
        time.sleep(2)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="PayPilot end-to-end recovery demo")
    parser.add_argument("--email", default=os.getenv("PAYPILOT_DEMO_EMAIL", ""),
                        help="Customer email. Must be on PAYPILOT_ALLOWED_RECIPIENTS to receive mail.")
    parser.add_argument("--amount", type=int, default=4900, help="Invoice amount in minor units")
    parser.add_argument("--currency", default="eur")
    parser.add_argument("--db", default=os.getenv("PAYPILOT_DEMO_DB", "data/demo-loop.db"),
                        help="Ledger for this run. Separate from the production one.")
    args = parser.parse_args()

    if not args.email:
        _fail("--email is required (or set PAYPILOT_DEMO_EMAIL).")

    stripe = _require_test_mode()

    # A dedicated ledger: a demo run must never mix its invoices into the real
    # recovery numbers.
    store = Store(args.db)
    reset_store(store)
    print(f"ledger: {args.db}")

    _step(1, "Creating test clock, customer, and a card that always fails to charge")
    clock = stripe.test_helpers.TestClock.create(frozen_time=int(time.time()))
    customer = stripe.Customer.create(
        email=args.email, name="Demo Customer", test_clock=clock.id,
        payment_method=CARD_ALWAYS_FAILS,
        invoice_settings={"default_payment_method": CARD_ALWAYS_FAILS},
    )
    print(f"    customer {customer.id} on clock {clock.id}")

    _step(2, "Creating the subscription")
    price = stripe.Price.create(
        unit_amount=args.amount, currency=args.currency,
        recurring={"interval": "month"},
        product_data={"name": "PayPilot Demo Plan"},
    )
    subscription = stripe.Subscription.create(
        customer=customer.id, items=[{"price": price.id}],
        metadata={"paypilot_customer_id": "cust_001"},
    )
    print(f"    subscription {subscription.id} ({subscription.status})")

    _step(3, "Advancing the clock a month so the renewal genuinely fails")
    stripe.test_helpers.TestClock.advance(
        clock.id, frozen_time=int(time.time()) + MONTH_SECONDS
    )
    for _ in range(30):
        if stripe.test_helpers.TestClock.retrieve(clock.id).status == "ready":
            break
        time.sleep(2)
    else:
        _fail("test clock did not settle in time; re-run or check the Stripe dashboard")

    invoices = stripe.Invoice.list(customer=customer.id, limit=10)
    failed = next((i for i in invoices.auto_paging_iter() if i.status in ("open", "uncollectible")), None)
    if failed is None:
        _fail("no unpaid invoice after advancing the clock; the renewal may not have run yet")
    print(f"    invoice {failed.id} is {failed.status}, amount_due {failed.amount_due}")

    _step(4, "Feeding the real invoice.payment_failed event through the recovery loop")
    event = _latest_event(stripe, "invoice.payment_failed", failed.id)
    if event is None:
        _fail(f"Stripe emitted no invoice.payment_failed for {failed.id}")
    result = handle_event(event, store)
    delivery = result.get("delivery") or {}
    print(f"    recorded: state={result.get('state')}  delivery={delivery.get('status')}")
    if delivery.get("status") == "dry_run":
        print("    (dry run: set PAYPILOT_SEND_EMAIL=1 and allowlist the address to send)")
    if delivery.get("link"):
        print(f"    recovery link: {delivery['link']}")
    if result.get("recovery"):
        print("\n    --- drafted message ---")
        for line in result["recovery"]["message"].splitlines():
            print(f"    {line}")

    before = _print_report(store, "after failure")

    _step(5, "Customer updates their card at the portal link (succeeding card)")
    stripe.PaymentMethod.attach(CARD_SUCCEEDS, customer=customer.id)
    stripe.Customer.modify(
        customer.id, invoice_settings={"default_payment_method": CARD_SUCCEEDS}
    )

    _step(6, "Paying the invoice, which is what the retry does")
    paid = stripe.Invoice.pay(failed.id)
    print(f"    invoice {paid.id} is now {paid.status}, amount_paid {paid.amount_paid}")

    _step(7, "Feeding the real invoice.paid event back through the loop")
    paid_event = _latest_event(stripe, "invoice.paid", failed.id)
    if paid_event is None:
        _fail(f"Stripe emitted no invoice.paid for {failed.id}")
    closed = handle_event(paid_event, store)
    print(f"    closed: handled={closed.get('handled')} state={closed.get('state')}")

    after = _print_report(store, "after recovery")

    print("\n=== RESULT ===")
    recovered = after["totals"]["recovered"] - before["totals"]["recovered"]
    print(f"  invoices recovered this run: {recovered}")
    for code, bucket in sorted(after["by_currency"].items()):
        print(f"  {code.upper()} recovered: {bucket['recovered_value']:,.2f}")
    print(f"\n  Stripe test clock: {clock.id} (delete it in the dashboard when done)")
    store.close()
    reset_store(None)
    return 0 if recovered > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
