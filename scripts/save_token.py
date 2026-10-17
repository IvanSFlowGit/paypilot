#!/usr/bin/env python3
"""Save the decision API bearer token to ~/.config/paypilot/decision_token.

WHY NOT pbpaste. A step that reads the clipboard depends on what was copied
last, and the instruction to run it invites copying. On 2026-10-17 the file
ended up holding the text of the command itself (43 characters, whitespace
inside) and the walk returned a 401 that looked like a server fault.

The token is typed into a hidden prompt, so it is not echoed and not in shell
history. A value with whitespace is refused because a real bearer has none.
The file is written 0600 through a temp file and a rename. Only the length is
ever printed.

Run:  python3 scripts/save_token.py [--force]
Exit: 0 saved, 1 refused, 2 could not run (existing file without --force)
"""
from __future__ import annotations

import argparse
import getpass
import os
import pathlib
import sys

TOKEN_FILE = pathlib.Path.home() / ".config" / "paypilot" / "decision_token"


def validate(value: str) -> str | None:
    """None when usable, else the reason it is refused."""
    if not value:
        return "empty"
    if any(c.isspace() for c in value):
        return "contains whitespace, so it is not a bearer token (did the clipboard hold a command?)"
    return None


def save(value: str, path: pathlib.Path | None = None) -> None:
    # Resolved at call time: a default bound at import would ignore a patched
    # TOKEN_FILE and write to the real one (it did, in the first version of the test).
    path = path or TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    os.replace(tmp, path)
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = ap.parse_args(argv)
    if TOKEN_FILE.exists() and not args.force:
        print(f"CANNOT RUN: {TOKEN_FILE} exists. Re-run with FORCE=1 to replace it.")
        return 2
    value = getpass.getpass("Paste the token (hidden), then press Enter: ").strip()
    why = validate(value)
    if why:
        print(f"REFUSED: the value is {why}. Nothing was written.")
        return 1
    save(value)
    print(f"saved {len(value)} characters to {TOKEN_FILE} (mode 0600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
