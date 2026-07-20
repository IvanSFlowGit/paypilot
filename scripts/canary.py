# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Detect unauthorised copies of this source.

The licence is legal protection: it tells you what to do AFTER you find an
infringing copy. This finds the copy. It does neither more nor less than search
public code and the web for fingerprints that are distinctive to PayPilot, so a
wholesale lift shows up in a search rather than never at all.

Two kinds of fingerprint:

* CANARY - a meaningless-looking token planted in the source, in the two places
  a copier keeps: the committed dunning copy (the artifact the whole product
  exists to produce) and an attribution constant. A search hit for the canary
  anywhere but this repo is a copy, full stop - the string has no other reason
  to exist.
* PHRASES - distinctive strings from the actual code and copy. Higher recall,
  lower certainty: a hit is a lead to look at, not proof.

Run ``python scripts/canary.py`` for a report, or ``--json`` for automation.
Needs ``gh`` authenticated for GitHub code search; web search is a printed
query you run by hand (no unauthenticated API is reliable for this).

This catches the lazy copier, which is the common case. It does not catch a
competent one who strips fingerprints - nothing does. That is the honest limit.
"""

from __future__ import annotations

import json
import subprocess
import sys

# The canary. Planted verbatim in data/templates/dunning.json (_meta.canary)
# and referenced below so a grep of the repo proves it is wired, not orphaned.
# If you rotate it, change it in BOTH places and re-plant, then re-baseline.
CANARY = "pp-824b8f4a0fd0"

# This repo, excluded from every search: a hit here is us, not a copy.
OWN_REPO = "IvanSFlowGit/paypilot"

# Distinctive phrases. Chosen to be things a copier keeps and unlikely to occur
# independently: specific comment wording, the audit event names, template copy.
# Distinctive enough that an independent origin is implausible. Generic names
# like "webhook_signature_rejected" were removed after a test run matched three
# unrelated repos - a phrase must be specific to be a lead, not noise.
PHRASES = [
    "AI dunning agent that recovers failed payments via RAG",  # our own summary
    "the card-update link the email points at",                # onboarding wording
    "an unsigned endpoint that writes to the recovery ledger",  # our comment
    "claiming we messaged someone we did not is exactly the",   # our comment
]


def _gh_code_search(query: str) -> list[dict]:
    """GitHub code search via gh, minus our own repo. Empty on any failure."""
    try:
        out = subprocess.run(
            ["gh", "api", "-X", "GET", "search/code",
             "-f", f"q={query} -repo:{OWN_REPO}", "--jq",
             ".items[] | {repo: .repository.full_name, path: .path, url: .html_url}"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    hits = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                hits.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return hits


def scan() -> dict:
    """Search every fingerprint; return findings grouped by fingerprint."""
    findings: dict[str, list[dict]] = {}
    for needle in [CANARY, *PHRASES]:
        hits = _gh_code_search(f'"{needle}"')
        if hits:
            findings[needle] = hits
    return {
        "canary": CANARY,
        "own_repo": OWN_REPO,
        "github_findings": findings,
        "web_queries": [
            f'https://www.google.com/search?q=%22{CANARY}%22',
            f'https://grep.app/search?q={CANARY}',
        ],
    }


def main(argv: list[str]) -> int:
    report = scan()
    if "--json" in argv:
        print(json.dumps(report, indent=2))
        return 1 if report["github_findings"] else 0

    print(f"Canary: {report['canary']}")
    print(f"Excluding own repo: {report['own_repo']}\n")
    if report["github_findings"]:
        print("POSSIBLE COPIES FOUND on GitHub:")
        for needle, hits in report["github_findings"].items():
            label = "CANARY" if needle == report["canary"] else "phrase"
            print(f"\n  [{label}] {needle!r}")
            for h in hits:
                print(f"    {h['repo']}  {h['path']}\n      {h['url']}")
        print("\nA CANARY hit is a copy. A phrase hit is a lead to check by hand.")
    else:
        print("No GitHub code-search hits outside the owner's repo.")
    print("\nRun these web searches by hand (no reliable unauthenticated API):")
    for q in report["web_queries"]:
        print(f"  {q}")
    return 1 if report["github_findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
