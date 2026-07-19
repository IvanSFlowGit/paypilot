# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""House-style gate for anything shipped: dashes, and public-asset hygiene.

Run by ``make lint``. Exits non-zero with the offending file:line so it can
fail a build rather than being a thing someone remembers to check.

Two checks:

1. No em or en dashes in source.
2. **No comments in public static assets.** Anything served to a browser is
   readable by everyone, and build rationale is internal. Design notes
   explaining why a nav was restyled shipped to the live page and were
   visible in devtools to any visitor.

``tests/test_zero_token.py`` is exempt because it asserts on those characters
directly, and a checker that flags its own checker is noise.
"""

from __future__ import annotations

import pathlib

BANNED = {"—": "em dash", "–": "en dash"}
# Anchored to this file, not the cwd. Run from another directory, the old
# cwd-relative roots matched nothing and the gate PASSED having verified
# nothing, which is the worst failure mode a checker can have.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOTS = tuple(_REPO_ROOT / d for d in ("app", "tests", "scripts"))

#: Per-line opt-out for the handful of places that legitimately need the
#: characters: the tests that assert on their absence. A per-line marker rather
#: than a file exemption, so those files are still checked everywhere else -
#: exempting a whole file would blind the gate to a real dash inside it.
ALLOW_MARKER = "lint-style: allow-dash"


#: Files served verbatim to a browser. Comments in these are public.
PUBLIC_ASSET_ROOTS = (_REPO_ROOT / "app" / "static",)
PUBLIC_ASSET_SUFFIXES = (".html", ".htm", ".css", ".js", ".svg")


def check_public_assets() -> list[str]:
    """Comments in anything served to a browser are readable by everyone.

    Build rationale is internal. Explanatory comments about why a component was
    restyled shipped to the live landing page and were visible in devtools, which
    is both an infra-disclosure leak and internal notes in a deliverable.
    """
    offences: list[str] = []
    for root in PUBLIC_ASSET_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in PUBLIC_ASSET_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(_REPO_ROOT)
            for number, line in enumerate(text.splitlines(), 1):
                if "/*" in line or "<!--" in line:
                    offences.append(
                        f"{rel}:{number}: comment in a public asset "
                        "(anyone can read it in devtools)"
                    )
    return offences


def main() -> int:
    offences: list[str] = []
    scanned = 0
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.name == "lint_style.py":
                continue  # this file names the characters it bans, by escape
            scanned += 1
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if ALLOW_MARKER in line:
                    continue
                for char, name in BANNED.items():
                    if char in line:
                        offences.append(f"{path.relative_to(_REPO_ROOT)}:{number}: {name}")

    if not scanned:
        print("lint gate scanned 0 files; refusing to report success")
        return 1

    offences.extend(check_public_assets())

    if offences:
        print("\n".join(offences))
        print(f"\n{len(offences)} violation(s). Dashes: use a plain hyphen. "
              "Public assets: move the comment into git history.")
        return 1
    print("style: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
