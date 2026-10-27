#!/usr/bin/env python3
"""Is this deployment ready to send mail to a merchant's customers?

Run it before switching a merchant on, and again after any config change. It
reads the same environment the app reads and answers in three states, because a
check that cannot tell "not ready" from "could not look" is how a missing
setting reads as a pass:

    0  READY       every requirement for the configured mode is met
    1  NOT READY   at least one requirement is missing; each is listed
    2  COULD NOT RUN  a live check was asked for and failed to execute

Nothing here sends to a customer. ``--send-test`` sends one plain notice to the
approvers (no link, no customer data). ``--gmail-live`` and ``--imap-live`` make
one real round trip with the configured mailbox: connect, create a throwaway
draft, find it again, delete it. All are off by default because they touch real
accounts.

``--env-file PATH`` checks a merchant's ``secrets.env`` instead of this
process's environment. Values are loaded, never printed.

Run:   python3 scripts/approval_preflight.py [--env-file PATH] [--send-test]
                                             [--gmail-live] [--imap-live]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import approval, gmail_drafts, imap_drafts  # noqa: E402
from app.mailer import allowed_recipients, sending_enabled  # noqa: E402
from app.safety import allowed_link_hosts, host_of  # noqa: E402


def load_env_file(path: str) -> int:
    """Load KEY=VALUE lines into the environment. Returns how many were set.

    Blank values are skipped, matching what the install pushes to Fly: a blank
    is "not provided", and loading it would make a missing key look present.
    """
    n = 0
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip() and value:
            os.environ[key.strip()] = value
            n += 1
    return n


def check() -> tuple[list[str], list[str]]:
    """Return ``(problems, notes)``. No problems means ready."""
    problems: list[str] = []
    notes: list[str] = []
    mode = approval.approval_mode()
    notes.append(f"approval mode: {mode}")

    if not sending_enabled():
        notes.append("PAYPILOT_SEND_EMAIL is off: every message is a dry run (safe default)")
    if not allowed_recipients():
        problems.append("PAYPILOT_ALLOWED_RECIPIENTS is empty: no customer can be mailed")
    if not (os.getenv("RESEND_API_KEY") or "").strip():
        problems.append("RESEND_API_KEY is unset: no transport for approval requests or sends")
    if not os.getenv("STRIPE_WEBHOOK_SECRET"):
        problems.append("STRIPE_WEBHOOK_SECRET is unset: the Stripe route cannot verify events")
    if not os.getenv("ADMIN_TOKEN"):
        problems.append("ADMIN_TOKEN is unset: /approvals is open to anyone")

    update_url = (os.getenv("PAYPILOT_UPDATE_URL") or "").strip()
    if not update_url:
        problems.append(
            "PAYPILOT_UPDATE_URL is unset: the fallback card-update link would point at "
            "the PayPilot demo page, not this merchant"
        )
    elif host_of(update_url) not in allowed_link_hosts():
        problems.append(
            "PAYPILOT_UPDATE_URL's host is not in PAYPILOT_ALLOWED_LINK_HOSTS: the output "
            "guard would refuse the first real message that uses the fallback link"
        )
    if not (os.getenv("PAYPILOT_BUSINESS_NAME") or "").strip():
        problems.append("PAYPILOT_BUSINESS_NAME is unset: emails would sign with the neutral default")

    if mode == approval.MODE_OFF:
        problems.append(
            "PAYPILOT_APPROVAL_MODE=off: messages send with no person approving them. "
            "Intended only for a merchant who has said so in writing"
        )
        return problems, notes

    if mode == approval.MODE_EMAIL:
        if not approval.approver_emails():
            problems.append("PAYPILOT_APPROVER_EMAILS is empty: nobody can approve, so nothing sends")
        if not approval.public_base_url():
            problems.append(
                "PAYPILOT_PUBLIC_BASE_URL is unset or not https: approval links cannot be built"
            )
    if mode == approval.MODE_GMAIL:
        if not gmail_drafts.configured():
            problems.append(
                "Google client id, client secret and refresh token are not all set: "
                "no drafts can be created"
            )
        if not approval.approver_emails():
            problems.append("PAYPILOT_APPROVER_EMAILS is empty: nobody is told a draft is waiting")
        notes.append(
            "gmail mode needs Google app verification before a merchant outside your "
            "test-user list can authorise it (docs/approval.md)"
        )
    if mode == approval.MODE_IMAP:
        if not imap_drafts.configured():
            problems.append(
                "PAYPILOT_IMAP_HOST, _PORT, _USER and _PASSWORD must all be set; nothing is "
                "guessed, including the Proton Bridge port (read it from the Bridge window)"
            )
        if os.getenv("PAYPILOT_IMAP_SECURITY", "ssl").strip().lower() not in ("ssl", "starttls"):
            problems.append("PAYPILOT_IMAP_SECURITY must be ssl or starttls")
        if not approval.approver_emails():
            problems.append("PAYPILOT_APPROVER_EMAILS is empty: nobody is told a draft is waiting")
        notes.append(
            "imap mode needs the mail server reachable from where PayPilot runs; a Proton "
            "Bridge on a merchant's laptop is not reachable from Fly (docs/approval.md)"
        )
    return problems, notes


def main(argv: list[str]) -> int:
    if "--env-file" in argv:
        i = argv.index("--env-file")
        try:
            n = load_env_file(argv[i + 1])
        except (IndexError, OSError) as exc:
            print(f"COULD NOT RUN --env-file: {type(exc).__name__}")
            return 2
        print(f"note: loaded {n} non-blank settings from the env file (values not shown)")
    problems, notes = check()
    for n in notes:
        print(f"note: {n}")

    if "--send-test" in argv:
        from app.mailer import send_operator_alert

        if not approval.approver_emails():
            print("COULD NOT RUN --send-test: no approvers configured")
            return 2
        for addr in approval.approver_emails():
            r = send_operator_alert(
                to=addr, subject="PayPilot approval setup test",
                body="This is a test. No customer was contacted and nothing needs approving.\n",
            )
            print(f"test notice to {addr}: {r['status']} {r.get('error') or ''}".rstrip())
            if r["status"] != "sent":
                problems.append(f"test notice to {addr} was not delivered ({r.get('error')})")

    if "--gmail-live" in argv:
        try:
            client = gmail_drafts.get_client()
            did = client.create_draft(gmail_drafts.build_raw(
                recipient="preflight@example.invalid", subject="PayPilot preflight (delete me)",
                body="Throwaway draft created by approval_preflight.py.", marker="0:preflight"))
            client.delete_draft(did)
            print("gmail live: token refresh, draft create and draft delete all succeeded")
        except Exception as exc:  # noqa: BLE001 - report whatever stopped it
            print(f"COULD NOT RUN --gmail-live: {type(exc).__name__}: {exc}")
            return 2

    if "--imap-live" in argv:
        try:
            from app.mailbox_approval import build_raw

            client = imap_drafts.get_client()
            ext = client.create_draft(build_raw(
                recipient="preflight@example.invalid", subject="PayPilot preflight (delete me)",
                body="Throwaway draft created by approval_preflight.py.",
                marker="0:preflight"))
            found = client.draft_exists(ext)
            client.delete_draft(ext)
            gone = not client.draft_exists(ext)
            print(f"imap live: draft created, found={found}, deleted={gone}")
            if not (found and gone):
                problems.append("imap round trip did not find or did not delete the throwaway draft")
        except Exception as exc:  # noqa: BLE001 - report whatever stopped it
            print(f"COULD NOT RUN --imap-live: {type(exc).__name__}: {exc}")
            return 2

    if problems:
        print("NOT READY")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("READY")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
