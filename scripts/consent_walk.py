#!/usr/bin/env python3
"""Walk the bank consent flow once against a live host, using the FAKE provider.

start (bearer) -> authorize_url on bank.invalid -> callback with the state and a
FAKE code -> 200 with a consent_id. Writes ONE synthetic row, every value of
which carries the FAKE marker. No real bank, no real data.

The bearer token comes from DECISION_API_TOKEN, else from the file
~/.config/paypilot/decision_token, which must be mode 0600. It is never printed.
After the callback the row is read back through GET /bank/consents/{id}.
Exit 0 walked, 1 a step refused, 2 could not run (no token, wide file, no network).

Run:    python3 scripts/consent_walk.py [--host https://paypilot.fly.dev]
Later:  python3 scripts/consent_walk.py --check [CONSENT_ID]   (read only; no id = the last walk's)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_ENV = "DECISION_API_TOKEN"
TOKEN_FILE = pathlib.Path.home() / ".config" / "paypilot" / "decision_token"
LAST_ID_FILE = pathlib.Path.home() / ".config" / "paypilot" / "last_consent_id"
CODE = "FAKE-walk"


def load_token(path: pathlib.Path = TOKEN_FILE) -> tuple[str | None, str]:
    """(token, reason). Env wins. A file readable by anyone but its owner is
    refused, because a secret on disk with a wide mode is already shared."""
    env = os.environ.get(TOKEN_ENV)
    if env:
        return env.strip(), "env"
    if not path.is_file():
        return None, f"{TOKEN_ENV} is not set and {path} does not exist"
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        return None, f"{path} is readable by others; run chmod 600 on it"
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        return None, f"{path} is empty"
    if any(c.isspace() for c in value):
        return None, (
            f"{path} holds text with whitespace inside, so it is not a bearer token; "
            "run make save-token FORCE=1"
        )
    return value, "file"


def unwrap(raw: str, client: str | None = None) -> tuple[str | None, str]:
    """The server accepts DECISION_API_TOKEN as a JSON map {token: client_id}.
    If what was saved is that map, the bearer is a KEY inside it. One entry is
    unwrapped; several need --client. Only client ids (the map's values, which
    are not secrets) are ever named, never a key."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw, "plain"
    if not (isinstance(parsed, dict) and parsed and all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items())):
        return raw, "plain"
    if client:
        keys = [k for k, v in parsed.items() if v == client]
        if len(keys) == 1:
            return keys[0], f"unwrapped from a JSON map for client {client}"
        return None, f"no single entry for client {client}; clients in the map: {sorted(set(parsed.values()))}"
    if len(parsed) == 1:
        return next(iter(parsed)), "unwrapped from a one-entry JSON map"
    return None, f"the saved value is a JSON map of several clients; pass --client one of {sorted(set(parsed.values()))}"


def diagnose(token: str, source: str) -> str:
    """Shape facts about a token that was refused. Length and form only: no
    character of the value is ever included."""
    inner_ws = any(c.isspace() for c in token)
    return (
        f"token from {source}: {len(token)} characters, "
        f"{'contains whitespace inside' if inner_ws else 'no inner whitespace'}. "
        "A 401 means the server is configured and this value matches nothing. Likely: "
        "(1) the clipboard held something other than the token, "
        "(2) the token was rotated since it was saved, "
        "(3) it was saved from a different app. Re-copy it from where you stored it."
    )


def _require_https(url: str) -> None:
    """The bearer token goes to this URL: https, or plain http to this machine only."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")):
        return
    raise SystemExit(f"refusing to send a token to {parts.scheme or 'no-scheme'}:// {parts.hostname}")


def _get(url: str, token: str | None = None) -> tuple[int, dict]:
    _require_https(url)
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
    ap.add_argument(
        "--check", nargs="?", const="LAST", metavar="CONSENT_ID",
        help="read one consent back, write nothing; with no id, the last one a walk saved",
    )
    ap.add_argument("--client", help="choose the entry when the saved token is a JSON map")
    args = ap.parse_args()
    host = args.host.rstrip("/")
    token, why = load_token()
    if not token:
        print(f"CANNOT RUN: {why}. Nothing was called.")
        return 2
    source = why
    token, note = unwrap(token, args.client)
    if not token:
        print(f"CANNOT RUN: {note}. Nothing was called.")
        return 2
    if note != "plain":
        print(note)
    if args.check:
        consent_id = args.check
        if consent_id == "LAST":
            if not LAST_ID_FILE.is_file():
                print(f"CANNOT RUN: no id given and {LAST_ID_FILE} does not exist; run a walk first. Nothing was called.")
                return 2
            consent_id = LAST_ID_FILE.read_text(encoding="utf-8").strip()
        return _read_back(host, token, consent_id)
    q = urllib.parse.urlencode({"provider": "fake", "redirect_uri": "https://example.invalid/cb"})
    try:
        status, body = _get(f"{host}/bank/consent/start?{q}", token)
    except OSError as exc:
        print(f"CANNOT RUN: {exc}")
        return 2
    if status != 200:
        print(f"REFUSED at start: {status} {body.get('error')} {body.get('detail')}")
        if status == 401:
            print(diagnose(token, source))
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
    return _read_back(host, token, body["consent_id"], expect_state=body.get("state"))


def _read_back(host: str, token: str, consent_id: str, expect_state: str | None = None) -> int:
    try:
        status, got = _get(f"{host}/bank/consents/{urllib.parse.quote(consent_id)}", token)
    except OSError as exc:
        print(f"CANNOT RUN: {exc}")
        return 2
    if status != 200:
        print(f"NOT FOUND on read-back: {status} {got.get('error')} {got.get('detail')}")
        return 1
    if got.get("consent_id") != consent_id or (expect_state and got.get("state") != expect_state):
        print(f"READ-BACK DISAGREES: {got}")
        return 1
    print(f"read-back ok: {consent_id} provider {got.get('provider')} state {got.get('state')}")
    if expect_state:  # a walk, not a later --check: remember the id so the check needs no paste
        LAST_ID_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        LAST_ID_FILE.write_text(consent_id + "\n", encoding="utf-8")
        LAST_ID_FILE.chmod(0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
