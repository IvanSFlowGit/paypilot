"""House-style gate: no em dashes, no en dashes, anywhere in the source.

Run by ``make lint``. Exits non-zero with the offending file:line so it can
fail a build rather than being a thing someone remembers to check.

``tests/test_zero_token.py`` is exempt because it asserts on those characters
directly, and a checker that flags its own checker is noise.
"""

from __future__ import annotations

import pathlib

BANNED = {"—": "em dash", "–": "en dash"}
ROOTS = ("app", "tests", "scripts")

#: Per-line opt-out for the handful of places that legitimately need the
#: characters: the tests that assert on their absence. A per-line marker rather
#: than a file exemption, so those files are still checked everywhere else -
#: exempting a whole file would blind the gate to a real dash inside it.
ALLOW_MARKER = "lint-style: allow-dash"


def main() -> int:
    offences: list[str] = []
    for root in ROOTS:
        for path in sorted(pathlib.Path(root).rglob("*.py")):
            if path.as_posix() == "scripts/lint_style.py":
                continue  # this file names the characters it bans, by escape
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if ALLOW_MARKER in line:
                    continue
                for char, name in BANNED.items():
                    if char in line:
                        offences.append(f"{path}:{number}: {name}")

    if offences:
        print("\n".join(offences))
        print(f"\n{len(offences)} style violation(s). Use a plain hyphen.")
        return 1
    print("style: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
