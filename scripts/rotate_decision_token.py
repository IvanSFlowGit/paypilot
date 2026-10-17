#!/usr/bin/env python3
"""Mint a new DECISION_API_TOKEN, set it on Fly, save it locally. Never print it.

WHY ROTATE. Fly secrets are write-only, and nothing holds the current value,
so the token is unrecoverable. Nothing uses it (no client traffic), and the AWS
Lambda reads its own copy from SSM, so rotating the Fly secret breaks nothing.

ORDER. Fly first, local file second. A failed Fly call must never leave a local
token that matches nothing. The value goes to flyctl on STDIN through
`flyctl secrets import`, so it is never in argv (visible to `ps`) or on screen.

PROOF. After the restart it calls /bank/consent/start once with the saved file's
token and requires 200 from the live host. That shows the Fly secret and the
local file are the same value without ever comparing or printing them.

Run:   python3 scripts/rotate_decision_token.py [--rotate] [--host URL]
Exit:  0 rotated and proven, 1 refused or proof failed, 2 could not run
"""
from __future__ import annotations

import argparse
import pathlib
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import consent_walk  # noqa: E402
import save_token  # noqa: E402

NAME = "DECISION_API_TOKEN"


def _fly(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["flyctl", *args], input=stdin, capture_output=True, text=True)


def verify(host: str) -> tuple[bool, str]:
    """Wait for the app, then one authenticated call with the SAVED token."""
    token, why = consent_walk.load_token(save_token.TOKEN_FILE)
    if not token:
        return False, why
    for _ in range(12):
        try:
            with urllib.request.urlopen(f"{host}/health?cb={time.time()}", timeout=10) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        time.sleep(5)
    q = urllib.parse.urlencode({"provider": "fake", "redirect_uri": "https://example.invalid/cb"})
    try:
        status, body = consent_walk._get(f"{host}/bank/consent/start?{q}", token)
    except OSError as exc:
        return False, f"could not reach {host}: {exc}"
    return judge(status, str(body.get("error", "")))


def judge(status: int, error: str = "") -> tuple[bool, str]:
    """Did the bearer get accepted? That is the only thing this proof is for.

    401 is the one answer that means the saved token and the Fly secret
    disagree. 503 means the server has no token configured. ANY OTHER status,
    including a 404 from an unregistered provider, is reached only AFTER the
    bearer was accepted, so it is a different fault and must not blame the
    token (it did on 2026-10-17, when 404 unknown_provider was read as a
    mismatch and a real registration defect was reported as a bad rotation).
    """
    detail = f"start returned {status} {error}".strip()
    if status == 401:
        return False, detail + "; the saved token and the Fly secret disagree"
    if status == 503:
        return False, detail + "; the server has no DECISION_API_TOKEN configured"
    return True, f"bearer accepted; {detail}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rotate", action="store_true", help="allow replacing an existing secret")
    ap.add_argument("--host", default="https://paypilot.fly.dev")
    args = ap.parse_args(argv)
    try:
        listed = _fly("secrets", "list")
    except OSError as exc:
        print(f"CANNOT RUN: flyctl is not runnable ({exc}). Nothing was changed.")
        return 2
    if listed.returncode != 0:
        print("CANNOT RUN: flyctl cannot list secrets, so I cannot tell whether one exists. Nothing was changed.")
        return 2
    exists = any(line.split()[:1] == [NAME] for line in listed.stdout.splitlines())
    if exists and not args.rotate:
        print(f"refusing: {NAME} already exists. Its value cannot be read back from Fly, so it is")
        print("  unrecoverable if you did not save it. Nothing uses it yet. Re-run with ROTATE=1 to replace it.")
        return 1
    token = secrets.token_urlsafe(32)
    out = _fly("secrets", "import", stdin=f"{NAME}={token}\n")
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).replace(token, "[token]").strip()
        print(f"FLY REFUSED (exit {out.returncode}); the local file was NOT written. {msg[:300]}")
        return 1
    save_token.save(token)
    print(f"set {NAME} on Fly and saved it to {save_token.TOKEN_FILE} (mode 0600); {len(token)} characters, not printed")
    ok, detail = verify(args.host.rstrip("/"))
    if not ok:
        print(f"PROOF FAILED: {detail}. Fly and the local file may disagree; re-run with ROTATE=1.")
        return 1
    print(f"proven on the live host: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
