#!/usr/bin/env python3
# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Replay a saved Unit webhook delivery through the ACH table. Reads a file, prints, sends and writes nothing.

    scripts/unit_returns_replay.py --events /abs/path/delivery.json
    scripts/unit_returns_replay.py --events /abs/path/delivery.json --signature <X-Unit-Signature value>

With --signature the delivery is checked first: base64 HMAC-SHA1 of the file's exact
bytes under ``UNIT_WEBHOOK_SECRET``. A missing secret or a mismatch is exit 2 and
nothing is decided, because a delivery that fails the check is not Unit's.

Exit codes are three states: 0 every event was decided from a named reason, 1 the
report printed but some events were held (no reason in the event, or a name not in
Unit's table) or were not returns, 2 the file could not be read or failed the
signature and NOTHING was decided. A report from exit 2 does not exist.

No receiver is mounted for this on the app. See app/unit_map.py for what has and
has not been verified against Unit's own pages.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.unit_map import (  # noqa: E402
    MAX_BYTES,
    UnitPayloadError,
    parse_unit_delivery,
    verify_unit_signature,
)

SECRET_ENV = "UNIT_WEBHOOK_SECRET"


def main(argv: list[str] | None = None, *, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--events", required=True)
    ap.add_argument("--signature")
    args = ap.parse_args(argv)
    try:
        p = Path(args.events)
        if not p.is_absolute():
            raise ValueError("path: give an absolute path")
        if not p.is_file():
            raise ValueError("path: not a file")
        if p.stat().st_size > MAX_BYTES:
            raise ValueError("path: larger than the cap")
        raw = p.read_bytes()
        if args.signature is not None:
            if not os.environ.get(SECRET_ENV):
                raise ValueError(f"signature: set {SECRET_ENV}, there is no default")
            if not verify_unit_signature(raw, args.signature, os.environ[SECRET_ENV]):
                raise ValueError("signature: does not match the file's bytes")
        results = parse_unit_delivery(raw)
    except (ValueError, UnitPayloadError, OSError) as exc:
        print(f"NOT RUN: {exc}", file=out)
        print("Nothing was decided.", file=out)
        return 2

    status = Counter(r.status for r in results)
    print(f"subject: {Path(args.events).resolve()}", file=out)
    print(f"signature: {'verified' if args.signature is not None else 'not checked'}", file=out)
    print(f"events: {len(results)} read, {status['decided']} decided, {status['held']} held, "
          f"{status['not_a_return']} not a return", file=out)
    for r in results:
        row = "-" if r.decision_row is None else str(r.decision_row)
        if r.status == "not_a_return":
            who = "-"
        elif r.person_required:
            who = "person" if r.matched or r.status == "held" else "person (conservative row)"
        else:
            who = "automatic"
        print(f"  {r.event_id or '-':<14}{r.payment_id or '-':<12}{r.reason or '-':<32}"
              f"{r.code or '-':<5}{r.status:<14}row {row:<3}{who}", file=out)
    held = [r for r in results if r.status == "held"]
    for r in held[:20]:
        print(f"  held {r.event_id or '-'}: {r.why.split(':')[0]}", file=out)
    print("dry run: nothing was sent or written. No Unit receiver is mounted on the app.", file=out)
    return 0 if status["decided"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
