#!/usr/bin/env python3
"""Scaffold one merchant's private install kit, and move it through its stages.

WHY ONE DEPLOYMENT PER MERCHANT. PayPilot is single-tenant by construction: one
Fly machine, one SQLite ledger on one volume, and every setting (approvers,
recipient allowlist, link hosts, business name) is a process-wide environment
variable. Two merchants in one app would share a ledger, a send cap and a
cooldown, and each would be approving the other's mail. The public demo app is
off limits for the same reason from the other side: it must only ever hold fake
data. So a merchant gets their own Fly app, their own volume, their own secrets.

WHERE THE KIT LIVES. NOT IN THIS REPOSITORY, which is public. The merchant's
name, their approver's address and their webhook secret go to
``$PAYPILOT_MERCHANT_HOME`` (default ``~/streamflow-private/clients``), which is
gitignored on the laptop. This script is generic and holds no merchant data.

SECRETS NEVER PASS THROUGH A CHAT OR THE COMMAND LINE. ``init`` generates the
two it can (the admin token and the PII salt) straight into ``secrets.env`` at
mode 0600 and prints neither. The two it cannot (the Resend key and the Stripe
webhook secret) are left blank for the operator to paste into the file locally.

STAGES, because the first live message to a stranger's customer is the
expensive one:

    dry    sending off. Deploy, point the Stripe webhook, run preflight.
    test   sending on, but ONLY to the approver and a test address.
    live   sending on to any customer. A person still approves every message.

``live`` refuses unless told the data processing agreement is signed and that a
test message was approved and delivered. Those are claims the operator makes
out loud, so they are flags and not prompts.

Run:   python3 scripts/new_merchant.py init SLUG --name "Merchant Ltd" \\
           --approver owner@merchant.example --update-url https://merchant.example/billing
       python3 scripts/new_merchant.py stage SLUG test|live [--dpa-signed --test-passed]
       python3 scripts/new_merchant.py show SLUG
Exit:  0 done, 1 refused (reason printed), 2 could not run
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
RESERVED = frozenset({"demo", "paypilot", "test", "staging", "prod", "production"})
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,22}[a-z0-9]$")
EMAIL_RE = re.compile(r"^[^@\s,<>\"']+@[^@\s,<>\"']+\.[^@\s,<>\"']+$")
MODES = ("email", "imap", "gmail")
STAGES = ("dry", "test", "live")
STRIPE_HOSTS = ("billing.stripe.com", "invoice.stripe.com", "pay.stripe.com")
REQUIRED_BEFORE_DEPLOY = ("RESEND_API_KEY", "STRIPE_WEBHOOK_SECRET")


class Refused(Exception):
    pass


def home() -> Path:
    return Path(os.getenv("PAYPILOT_MERCHANT_HOME") or Path.home() / "streamflow-private" / "clients")


def app_name(slug: str) -> str:
    return f"paypilot-{slug}"


def kit_dir(slug: str) -> Path:
    return home() / app_name(slug)


def volume_name(slug: str) -> str:
    return f"paypilot_{slug.replace('-', '_')}_data"


def check_slug(slug: str) -> None:
    if slug in RESERVED:
        raise Refused(f"'{slug}' is reserved. The demo app must stay fake-data only.")
    if not SLUG_RE.match(slug or ""):
        raise Refused("slug must be 4 to 24 characters: lowercase letters, digits, hyphens, "
                      "starting with a letter and not ending in a hyphen")


def check_emails(raw: str, what: str) -> list[str]:
    out = []
    for part in (raw or "").split(","):
        addr = part.strip().lower()
        if not addr:
            continue
        if not EMAIL_RE.match(addr):
            raise Refused(f"{what}: '{addr}' is not an email address")
        if addr not in out:
            out.append(addr)
    if not out:
        raise Refused(f"{what}: at least one address is required")
    return out


def check_https_url(raw: str, what: str) -> str:
    parts = urlsplit((raw or "").strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise Refused(f"{what} must be an https URL with a host and no credentials")
    return raw.strip()


# -- rendering ---------------------------------------------------------------


def render_fly_toml(slug: str, template: str | None = None) -> str:
    """The repo's fly.toml with this merchant's app and volume, and no fake bank."""
    text = template if template is not None else (REPO / "fly.toml").read_text(encoding="utf-8")
    text, n_app = re.subn(r'(?m)^app = "paypilot"$', f'app = "{app_name(slug)}"', text)
    text, n_vol = re.subn(r'(?m)^(\s*source = )"paypilot_data"$',
                          rf'\1"{volume_name(slug)}"', text)
    text = re.sub(r'(?m)^\s*PAYPILOT_FAKE_BANK = "1"\n', "", text)
    # A comment in the demo's file tells you how to create the DEMO volume on the
    # DEMO app. Copied out of a merchant's file it would do exactly that.
    text = text.replace("fly volumes create paypilot_data -a paypilot",
                        f"fly volumes create {volume_name(slug)} -a {app_name(slug)}")
    if n_app != 1 or n_vol != 1:
        raise Refused("fly.toml no longer has the app and volume lines this script rewrites; "
                      "refusing to emit a config that would point at the demo app")
    return text


def secrets_lines(m: dict, *, admin_token: str, pii_salt: str, stage: str) -> list[str]:
    allowed = ",".join(dict.fromkeys([m["test_recipient"], *m["approvers"]]))
    hosts = ",".join(dict.fromkeys([*STRIPE_HOSTS, urlsplit(m["update_url"]).hostname]))
    return [
        f"PAYPILOT_APPROVAL_MODE={m['mode']}",
        f"PAYPILOT_APPROVER_EMAILS={','.join(m['approvers'])}",
        f"PAYPILOT_PUBLIC_BASE_URL=https://{m['app']}.fly.dev",
        f"PAYPILOT_ALLOWED_RECIPIENTS={'*' if stage == 'live' else allowed}",
        f"PAYPILOT_SEND_EMAIL={'0' if stage == 'dry' else '1'}",
        "PAYPILOT_AI_DISCLOSURE=1",
        f"PAYPILOT_BUSINESS_NAME={m['name']}",
        f"PAYPILOT_FROM_EMAIL={m['from_email']}",
        f"PAYPILOT_UPDATE_URL={m['update_url']}",
        f"PAYPILOT_ALLOWED_LINK_HOSTS={hosts}",
        "PAYPILOT_HOLDOUT_PCT=0",
        f"ADMIN_TOKEN={admin_token}",
        f"PAYPILOT_PII_SALT={pii_salt}",
        "RESEND_API_KEY=",
        "STRIPE_WEBHOOK_SECRET=",
        "STRIPE_API_KEY=",
    ]


def render_runbook(m: dict) -> str:
    base = f"https://{m['app']}.fly.dev"
    return f"""# {m['name']}: PayPilot install kit

Created {m['created']}. Stage when created: dry. Nothing here is a secret.

App `{m['app']}`, volume `{m['volume']}`, region lhr. Approvals go to
{', '.join(m['approvers'])} in `{m['mode']}` mode.

## Before anything is switched on
* Data processing agreement signed: ____
* Merchant knows every message needs {m['approvers'][0]}'s approval before it sends: ____
* Merchant's own customers are covered by their terms for payment reminders: ____

## Steps
1. Fill `secrets.env` locally (never in chat): `RESEND_API_KEY` and
   `STRIPE_WEBHOOK_SECRET`. `STRIPE_API_KEY` is optional: without it the email
   links to the invoice's hosted page instead of a billing-portal session.
2. Run `ppmerchant {m['slug']}`. It checks, deploys, sets secrets, and reads the
   live host back.
3. In the merchant's Stripe dashboard add a webhook endpoint
   `{base}/webhooks/stripe` for `invoice.payment_failed`, `invoice.paid`,
   `invoice.payment_succeeded` and `customer.subscription.deleted`. Its signing
   secret is the `STRIPE_WEBHOOK_SECRET` from step 1.
4. `python3 scripts/new_merchant.py stage {m['slug']} test`, then
   `ppmerchant {m['slug']}` again. Sending is now on for {m['test_recipient']} and
   the approvers only.
5. Fail one test payment. Confirm the approval email arrives, the page shows the
   text, Send delivers exactly one email, and the invoice moves to `messaged`.
6. `python3 scripts/new_merchant.py stage {m['slug']} live --dpa-signed --test-passed`,
   then `ppmerchant {m['slug']}` again. A person still approves every message.

## Stop everything
`fly secrets set PAYPILOT_SEND_EMAIL=0 -a {m['app']}` stops all sending at once.
Approvals already open expire on their own.
"""


# -- commands ----------------------------------------------------------------


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def cmd_init(args: dict) -> int:
    slug = args["slug"]
    check_slug(slug)
    name = (args.get("name") or "").strip()
    if not name or len(name) > 80 or any(c in name for c in "\r\n\x00<>"):
        raise Refused("--name is required, 80 characters or fewer, no angle brackets or newlines")
    approvers = check_emails(args.get("approver", ""), "--approver")
    update_url = check_https_url(args.get("update_url", ""), "--update-url")
    mode = args.get("mode") or "email"
    if mode not in MODES:
        raise Refused(f"--mode must be one of {', '.join(MODES)}")
    test_recipient = check_emails(args.get("test_recipient") or approvers[0], "--test-recipient")[0]
    from_email = (args.get("from_email") or f"{name} via PayPilot <billing@streamflow.solutions>").strip()
    if any(c in from_email for c in "\r\n\x00"):
        raise Refused("--from-email contains a control character")

    d = kit_dir(slug)
    if d.exists():
        raise Refused(f"{d} already exists; refusing to overwrite a merchant's kit")
    m = {
        "slug": slug, "name": name, "app": app_name(slug), "volume": volume_name(slug),
        "approvers": approvers, "mode": mode, "update_url": update_url,
        "from_email": from_email, "test_recipient": test_recipient,
        "stage": "dry", "created": date.today().isoformat(),
    }
    fly = render_fly_toml(slug)
    d.mkdir(parents=True, mode=0o700)
    write_private(d / "merchant.json", json.dumps(m, indent=2) + "\n")
    write_private(d / "fly.toml", fly, 0o644)
    write_private(d / "secrets.env", "\n".join(secrets_lines(
        m, admin_token=secrets.token_urlsafe(32), pii_salt=secrets.token_hex(32), stage="dry")) + "\n")
    write_private(d / "RUNBOOK.md", render_runbook(m), 0o644)
    print(f"kit written: {d}")
    print("files: merchant.json fly.toml secrets.env RUNBOOK.md")
    print("secrets.env holds a generated admin token and PII salt (not printed) and two "
          "blanks you fill locally: RESEND_API_KEY, STRIPE_WEBHOOK_SECRET")
    return 0


def _load(slug: str) -> tuple[Path, dict]:
    check_slug(slug)
    d = kit_dir(slug)
    if not (d / "merchant.json").is_file():
        raise Refused(f"no kit for '{slug}' at {d}; run init first")
    return d, json.loads((d / "merchant.json").read_text(encoding="utf-8"))


def _set_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0]
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def cmd_stage(args: dict) -> int:
    d, m = _load(args["slug"])
    stage = args.get("stage")
    if stage not in STAGES:
        raise Refused(f"stage must be one of {', '.join(STAGES)}")
    if stage == "live":
        if not args.get("dpa_signed"):
            raise Refused("live needs --dpa-signed: the merchant's customers' names and "
                          "addresses pass through this service")
        if not args.get("test_passed"):
            raise Refused("live needs --test-passed: one approved test message must have "
                          "been delivered first")
        if m["stage"] != "test":
            raise Refused(f"live follows test, and this merchant is at '{m['stage']}'")
    if stage == "test" and m["stage"] == "live":
        raise Refused("stepping live back to test: use stage dry to stop sending first")
    updates = {
        "PAYPILOT_SEND_EMAIL": "0" if stage == "dry" else "1",
        "PAYPILOT_ALLOWED_RECIPIENTS": "*" if stage == "live" else ",".join(
            dict.fromkeys([m["test_recipient"], *m["approvers"]])),
    }
    _set_env(d / "secrets.env", updates)
    m["stage"] = stage
    (d / "merchant.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    print(f"{m['app']} is now at stage '{stage}' in the local kit.")
    print(f"run ppmerchant {m['slug']} to push it to Fly.")
    return 0


def cmd_show(args: dict) -> int:
    _, m = _load(args["slug"])
    print(json.dumps(m, indent=2))
    return 0


def parse(argv: list[str]) -> tuple[str, dict]:
    if len(argv) < 2 or argv[0] not in ("init", "stage", "show"):
        raise Refused(__doc__.split("Run:")[1].strip())
    cmd, args = argv[0], {"slug": argv[1]}
    rest = argv[2:]
    if cmd == "stage":
        if not rest:
            raise Refused("stage needs dry, test or live")
        args["stage"], rest = rest[0], rest[1:]
    flags = {"--name": "name", "--approver": "approver", "--update-url": "update_url",
             "--mode": "mode", "--test-recipient": "test_recipient", "--from-email": "from_email"}
    bools = {"--dpa-signed": "dpa_signed", "--test-passed": "test_passed"}
    i = 0
    while i < len(rest):
        if rest[i] in bools:
            args[bools[rest[i]]] = True
            i += 1
        elif rest[i] in flags and i + 1 < len(rest):
            args[flags[rest[i]]] = rest[i + 1]
            i += 2
        else:
            raise Refused(f"unknown or incomplete argument: {rest[i]}")
    return cmd, args


def main(argv: list[str]) -> int:
    try:
        cmd, args = parse(argv)
        return {"init": cmd_init, "stage": cmd_stage, "show": cmd_show}[cmd](args)
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return 1
    except OSError as exc:
        print(f"COULD NOT RUN: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
