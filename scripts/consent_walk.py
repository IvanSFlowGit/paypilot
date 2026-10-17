#!/usr/bin/env python3
"""Walk the bank consent flow once against a live host, using the FAKE provider.

start (bearer) -> authorize_url on bank.invalid -> callback with the state and a
FAKE code -> 200 with a consent_id. Writes ONE synthetic row, every value of
which carries the FAKE marker. No real bank, no real data.

The bearer token is read from DECISION_API_TOKEN in the environment and is never
printed. Exit 0 walked, 1 a step refused, 2 could not run (no token, no network).

Run:  python3 scripts/consent_walk.py [--host https://paypilot.fly.dev]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_ENV = "DECISION_API_TOKEN"
CODE = "FAKE-walk"


def _get(url: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="https://paypilot.fly.dev")
    host = ap.parse_args().host.rstrip("/")
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"CANNOT RUN: {TOKEN_ENV} is not set. Nothing was called.")
        return 2
    q = urllib.parse.urlencode({"provider": "fake", "redirect_uri": "https://example.invalid/cb"})
    try:
        status, body = _get(f"{host}/bank/consent/start?{q}", token)
    except OSError as exc:
        print(f"CANNOT RUN: {exc}")
        return 2
    if status != 200:
        print(f"REFUSED at start: {status} {body.get('error')} {body.get('detail')}")
        return 1
    url = body.get("authorize_url", "")
    if not url.startswith("https://bank.invalid/"):
        print(f"REFUSED: authorize_url is not on bank.invalid: {url[:60]}")
        return 1
    print(f"start ok: authorize_url on bank.invalid, state length {len(body.get('state', ''))}")
    cb = urllib.parse.urlencode({"state": body["state"], "code": CODE})
    status, body = _get(f"{host}/bank/consent/callback?{cb}")
    if status != 200 or not body.get("consent_id"):
        print(f"REFUSED at callback: {status} {body.get('error')} {body.get('detail')}")
        return 1
    print(f"callback ok: consent_id {body['consent_id']} state {body.get('state')}")
    print("The route read the row back before answering 200 (routes.py), so this is a stored consent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
