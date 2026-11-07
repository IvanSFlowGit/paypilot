# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""End to end check of POST /webhooks/stripe, with signatures computed the way Stripe computes them.

The same ``run_cases`` runs two ways: in process against the app (tests/test_stripe_e2e.py) and
over HTTP against a deployed URL (this script). It sends real-shaped signed events, and for the
rejection cases it also sends one valid event, because a gate that rejects everything looks
identical to a gate that works until something legitimate is sent through it.

    STRIPE_WEBHOOK_SECRET=whsec_... python scripts/stripe_webhook_e2e.py --base https://paypilot.fly.dev

A live run writes ONE recovery for an invoice named ``in_e2e_<timestamp>`` on whatever tenant the
secret belongs to (the demo tenant on the public app, in mock mode, with live sending off) and
then closes it with ``invoice.paid``. The secret is read from the environment and never printed.

Exit: 0 every case behaved, 1 a case failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request


def sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    v1 = hmac.new(secret.encode(), str(ts).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


def event(etype: str, event_id: str, invoice_id: str, decline_code: str = "expired_card") -> dict:
    return {
        "id": event_id,
        "type": etype,
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": "cus_E2E", "subscription": "sub_E2E",
            "amount_due": 4900, "currency": "eur", "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": decline_code}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


def run_cases(send, secret: str) -> list[tuple[str, bool, str]]:
    """``send(body: bytes, signature: str | None) -> (status, json_or_none)``."""
    tag = str(int(time.time() * 1000))
    inv = f"in_e2e_{tag}"
    failed = json.dumps(event("invoice.payment_failed", f"evt_e2e_{tag}_a", inv)).encode()
    out: list[tuple[str, bool, str]] = []

    def case(name: str, ok: bool, detail: str) -> None:
        out.append((name, ok, detail))

    s, _ = send(failed, None)
    case("unsigned delivery is rejected", s == 400, f"status {s}")

    s, _ = send(failed, sign(failed, "whsec_not_the_secret"))
    case("signature from the wrong secret is rejected", s == 400, f"status {s}")

    other = json.dumps(event("invoice.payment_failed", f"evt_e2e_{tag}_x", inv, "insufficient_funds")).encode()
    s, _ = send(other, sign(failed, secret))
    case("signature for a different body (tampered) is rejected", s == 400, f"status {s}")

    s, _ = send(failed, sign(failed, secret, ts=int(time.time()) - 3600))
    case("a correctly signed but hour-old delivery is rejected", s == 400, f"status {s}")

    s, body = send(failed, sign(failed, secret))
    opened = s == 200 and isinstance(body, dict) and body.get("handled") is True and "recovery" in body
    strat = (body or {}).get("recovery", {}).get("strategy", {}) if opened else {}
    case("CONTROL: a validly signed payment_failed is accepted and opens a recovery", opened,
         f"status {s}, strategy action {strat.get('action')!r}")

    s, body = send(failed, sign(failed, secret))
    case("the same event id again is replayed, not processed twice",
         s == 200 and isinstance(body, dict) and body.get("idempotent") is True, f"status {s}")

    ignored = json.dumps({"id": f"evt_e2e_{tag}_c", "type": "customer.created", "data": {"object": {}}}).encode()
    s, body = send(ignored, sign(ignored, secret))
    case("a signed event type PayPilot does not handle is acknowledged, not run",
         s == 200 and isinstance(body, dict) and body.get("handled") is False, f"status {s}")

    garbage = b"not json at all"
    s, _ = send(garbage, sign(garbage, secret))
    case("a correctly signed body that is not JSON is a 400", s == 400, f"status {s}")

    paid = json.dumps(event("invoice.paid", f"evt_e2e_{tag}_b", inv)).encode()
    s, body = send(paid, sign(paid, secret))
    case("invoice.paid for the same invoice is accepted and closes it",
         s == 200 and isinstance(body, dict) and body.get("handled") is True, f"status {s}")
    return out


def _http_sender(base: str):
    url = base.rstrip("/") + "/webhooks/stripe"

    def send(body: bytes, signature: str | None):
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        if signature is not None:
            req.add_header("Stripe-Signature", signature)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None

    return send


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True, help="e.g. https://paypilot.fly.dev")
    args = ap.parse_args(argv)
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        print("could not run: STRIPE_WEBHOOK_SECRET is not set (it is read from the environment, never printed)",
              file=sys.stderr)
        return 2
    try:
        results = run_cases(_http_sender(args.base), secret)
    except Exception as exc:  # noqa: BLE001
        print(f"could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    bad = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  [{detail}]")
    print(f"\n{len(results) - len(bad)} of {len(results)} cases behaved" + ("" if not bad else f", {len(bad)} did NOT"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
