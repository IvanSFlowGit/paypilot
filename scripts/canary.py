# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
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
import os
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
            f'https://github.com/search?q=%22{CANARY}%22&type=code',
            f'https://www.google.com/search?q=%22{CANARY}%22',
            f'https://grep.app/search?q={CANARY}',
        ],
    }


# Where the full, attacker-authored hit list lands on detection. The alert email
# deliberately carries none of the repo names, paths, or links (they are chosen
# by whoever published the matching repo - see ``alert_body``), so the owner
# needs somewhere else to learn WHERE the copy is. That somewhere is a local
# file on the machine that ran the scan, read from a trusted terminal rather
# than pushed through an inbox. A filesystem path is not a link and carries no
# host, so naming this default in the alert is safe.
DEFAULT_REPORT_NAME = "canary-findings.json"


def findings_path() -> str:
    """Local path the full findings are written to. Env-overridable, never empty."""
    return (os.getenv("CANARY_REPORT_PATH") or "").strip() or DEFAULT_REPORT_NAME


def write_findings(report: dict, path: str | None = None) -> str | None:
    """Persist the whole scan (repos, paths, URLs) locally, or return None if clean.

    This is the fix for "the alert never tells me where the thief is": the email
    cannot carry the hits without letting an attacker choose what reaches the
    inbox, so the hits go to a file instead. Written with ``json.dump``, which
    escapes control characters, so a terminal escape smuggled into a repo name
    becomes an inert ``\\u001b`` in the file rather than an active sequence when
    the owner later reads it. Temp-then-replace, so a crash mid-write cannot
    truncate a prior report into a half-file that reads as "no copies".
    """
    if not report.get("github_findings"):
        return None
    target = path or findings_path()
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=True)
    os.replace(tmp, target)
    return target


#: Sent instead of the composed alert if composition ever produces something the
#: output guard rejects. A constant, so it cannot itself carry anything: the
#: point is that a detection is never silently swallowed, only ever downgraded.
MINIMAL_ALERT = (
    "Copy detection fired, and the composed alert failed the output guard, so it "
    "was not sent.\n\n"
    f"Fingerprint: {CANARY}\n\n"
    "Check the GitHub code search for that string by hand, and re-run "
    "scripts/canary.py locally to see the hits."
)

ALERT_SUBJECT = "PayPilot: possible unlicensed copy detected"


def _clean(value, limit: int = 120) -> str:
    """One untrusted string, made safe to print to a terminal.

    Repo names, paths and URLs in a code-search result are chosen by whoever
    published the matching repo. stdout is a terminal, so an escape sequence in
    a repo name can erase the lines above it and rewrite the report that is
    reporting on it. Control characters go, and the length is capped so one
    pathological path cannot bury the rest of the output.
    """
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text[:limit] if len(text) <= limit else text[:limit] + "..."


def alert_body(report: dict) -> str:
    """Compose the alert. Built from OUR constants only, never from the hits.

    This is the whole fix for the defect, and it is a composition rule rather
    than a filter: a code-search hit is attacker-authored data, so quoting one
    in an email means an attacker who publishes a repo containing the canary
    chooses a link that lands in the owner's inbox. Sanitising it is a losing
    game; not including it is not. Counts carry the signal - "the canary was
    found, twice" is what makes the owner go and look - and the owner reads the
    hits from a terminal they trust.

    Fingerprint names are echoed only when they are one of ours. Anything else
    in the findings dict came from somewhere unexpected and is counted, not
    printed, which is positive validation rather than a blocklist.
    """
    known = {CANARY, *PHRASES}
    findings = report.get("github_findings") or {}
    canary_hits = sum(len(v) for k, v in findings.items() if k == CANARY)
    total_hits = sum(len(v) for v in findings.values())
    unknown = [k for k in findings if k not in known]

    lines = [f"Copy detection found a fingerprint outside {OWN_REPO}.", ""]
    if canary_hits:
        lines.append(f"CANARY (an unlicensed copy): {CANARY}")
        lines.append(f"  {canary_hits} hit(s). A canary hit is a copy, not a coincidence.")
    phrases_matched = [k for k in findings if k in PHRASES]
    if phrases_matched:
        lines.append(f"Phrases matched (each a lead, not proof): {len(phrases_matched)}")
    if unknown:
        lines.append(f"Unrecognised fingerprints in the report: {len(unknown)}")
    lines += [
        f"Fingerprints matched: {len(findings)}. Code-search hits: {total_hits}.",
        "",
        "The repo names, paths and links are deliberately left out of this email: "
        "they are written by whoever published the matching repo, so quoting them "
        "here would let them choose what lands in your inbox.",
        "",
        f"The full hit list (repos, paths, links) is written to {DEFAULT_REPORT_NAME} "
        "on the machine that ran this scan. Open it from a trusted terminal: it is a "
        "plain file, not a link, and json-escaped so a hostile repo name cannot run "
        "in your shell. Then follow the copy-enforcement runbook in docs/legal/.",
        "",
        "To see them another way, run scripts/canary.py yourself, or search GitHub "
        "code for the fingerprint above.",
    ]
    return "\n".join(lines)


def email_alert(report: dict) -> bool:
    """Email the owner when a copy is found. Returns True if an email was sent.

    Goes through ``app.mailer``, which is the one place in this project that
    talks to the mail provider, and its transport (``_post_to_resend``) is where
    the output guard is enforced for every send. This function used to post to
    Resend itself, with none of those guards, carrying a body assembled from
    code-search results. Callers may add a stricter check of their own on top -
    ``app/loop.py`` pins the exact recovery link it minted, and the fallback
    below picks a safe body rather than accepting a refusal - but no caller can
    skip the transport check.

    Silent no-op when unconfigured, so a run without secrets still prints its
    report rather than crashing. A detection is never silently swallowed: if the
    composed body somehow fails the guard, the minimal notice goes instead.
    """
    from app.mailer import STATUS_SENT, send_operator_alert
    from app.safety import message_violations

    key = (os.getenv("CANARY_RESEND_API_KEY") or os.getenv("RESEND_API_KEY") or "").strip()
    to = (os.getenv("CANARY_ALERT_EMAIL") or "").strip()
    sender = (os.getenv("CANARY_FROM_EMAIL") or "PayPilot Canary <alerts@streamflow.solutions>").strip()
    if not (key and to and report.get("github_findings")):
        return False

    body = alert_body(report)
    if message_violations(body):
        # Same guard function the sender enforces with, called here only to pick
        # the fallback. The sender would refuse this body outright, and a refusal
        # the owner never hears about is a detection lost.
        body = MINIMAL_ALERT

    result = send_operator_alert(
        to=to, subject=ALERT_SUBJECT, body=body, api_key=key, sender_address=sender
    )
    return result["status"] == STATUS_SENT


def main(argv: list[str]) -> int:
    report = scan()
    # Persist the full hit list locally the moment there is one, before printing
    # or emailing: the email cannot carry it and an unwatched cron run has no
    # terminal to read. This is where the owner learns which repo, which path.
    written = write_findings(report)
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
                # _clean, not raw: everything in a hit is attacker-authored and
                # this line goes to a terminal that obeys escape sequences.
                print(f"    {_clean(h['repo'])}  {_clean(h['path'])}"
                      f"\n      {_clean(h['url'], 300)}")
        print("\nA CANARY hit is a copy. A phrase hit is a lead to check by hand.")
        if written:
            print(f"Full findings written to: {written}")
            print("Enforcement steps: docs/legal/copy-enforcement-runbook.md")
    else:
        print("No GitHub code-search hits outside the owner's repo.")
    print("\nRun these web searches by hand (no reliable unauthenticated API):")
    for q in report["web_queries"]:
        print(f"  {q}")
    if report["github_findings"]:
        sent = email_alert(report)
        print(f"\nemail alert: {'sent' if sent else 'not sent (CANARY_ALERT_EMAIL / key unset)'}")
    return 1 if report["github_findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
