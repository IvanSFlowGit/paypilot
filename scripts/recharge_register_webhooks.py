# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Point a merchant's Recharge store at PayPilot's /webhooks/recharge.

Onboarding one merchant is two webhooks on their store:
``charge/failed`` and ``charge/max_retries_reached``. Dry run by default; pass
``--apply`` to create them. Topics already registered to the same address are
skipped, so running it twice is safe.

The merchant creates an API token in their Recharge admin and gives it to you.
It is read from ``RECHARGE_API_TOKEN`` and never printed. The API Client Secret
from the same portal goes on the PayPilot deployment as ``RECHARGE_CLIENT_SECRET``
so deliveries verify.

    RECHARGE_API_TOKEN=... python scripts/recharge_register_webhooks.py
    RECHARGE_API_TOKEN=... python scripts/recharge_register_webhooks.py --apply

Endpoint shape: POST https://api.rechargeapps.com/webhooks with ``address``,
``topic`` and ``included_objects``, headers ``X-Recharge-Access-Token`` and
``X-Recharge-Version: 2021-11``, one webhook per topic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.rechargeapps.com/webhooks"
VERSION = "2021-11"
TOPICS = ("charge/failed", "charge/max_retries_reached")
DEFAULT_BASE = "https://paypilot.fly.dev"


def _request(method: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API, data=data, method=method, headers={
        "X-Recharge-Access-Token": token,
        "X-Recharge-Version": VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read() or b"{}")


def existing_topics(listing: dict, address: str) -> set[str]:
    """Topics already registered to ``address``. Accepts a list or {"webhooks": [...]}."""
    hooks = listing.get("webhooks", listing) if isinstance(listing, dict) else listing
    if not isinstance(hooks, list):
        return set()
    return {h.get("topic") for h in hooks
            if isinstance(h, dict) and h.get("address") == address and h.get("topic")}


def plan(address: str, already: set[str]) -> list[dict]:
    """Webhooks still to create, in a stable order."""
    return [{"address": address, "topic": t, "included_objects": ["customer"]}
            for t in TOPICS if t not in already]


def main(argv: list[str] | None = None, request=_request) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=DEFAULT_BASE, help="PayPilot deployment URL")
    ap.add_argument("--apply", action="store_true", help="create the webhooks (default: dry run)")
    args = ap.parse_args(argv)

    token = (os.getenv("RECHARGE_API_TOKEN") or "").strip()
    if not token:
        print("REFUSED: set RECHARGE_API_TOKEN to the merchant's Recharge API token")
        return 1
    address = args.base_url.rstrip("/") + "/webhooks/recharge"
    if not address.startswith("https://"):
        print(f"REFUSED: {address} is not https; Recharge deliveries carry customer data")
        return 1

    try:
        already = existing_topics(request("GET", token), address)
    except urllib.error.HTTPError as exc:
        print(f"REFUSED: listing webhooks returned HTTP {exc.code} (check the token's scopes)")
        return 1

    todo = plan(address, already)
    for topic in sorted(already & set(TOPICS)):
        print(f"  already registered  {topic} -> {address}")
    for hook in todo:
        print(f"  {'create' if args.apply else 'would create'}  {hook['topic']} -> {address}")
    if not todo:
        print("Nothing to do.")
        return 0
    if not args.apply:
        print("Dry run. Re-run with --apply to create them.")
        return 0

    for hook in todo:
        try:
            created = request("POST", token, hook)
        except urllib.error.HTTPError as exc:
            print(f"FAILED: {hook['topic']} returned HTTP {exc.code}")
            return 1
        wid = (created.get("webhook") or created).get("id") if isinstance(created, dict) else None
        print(f"  created {hook['topic']} id={wid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
