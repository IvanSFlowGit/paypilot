# SOP: onboarding a PayPilot merchant

Generic on purpose. This repository is public, so a merchant's name, addresses
and keys never go in it. They live in `~/streamflow-private/clients/paypilot-SLUG/`,
which is gitignored.

Read [`approval.md`](approval.md) for what the approval step guarantees and which
mode fits which merchant. This document is the order of work.

## The shape

There are two ways to host a merchant. Decide which before the first command.

* **Their own app (default).** One merchant, one Fly app, one volume, one set of
  secrets, single mode (`PAYPILOT_TENANCY` unset). The steps below are this path.
  Choose it when the merchant wants database-level separation, or on a first install.
* **A tenant of a shared deployment.** `PAYPILOT_TENANCY=multi`, one ledger holding
  several merchants, each with its own webhook URL, secret, tokens and approvers.
  The runbook is "Hosting a merchant as a tenant" at the end of this document.
  Isolation is enforced in the application and checked by tests, not by the
  database, and no production deployment has yet carried more than one merchant.

Never run two merchants in one SINGLE-mode app: they would share a send cap and a
cooldown and approve each other's mail. The public demo app (`paypilot`) holds fake
data only and is never a merchant's home: the installer refuses it by name.

Every message to the merchant's customers needs the merchant's approver to press
Send first, in every stage. The stages control only who could be mailed.

| Stage | Sending | Who can be mailed |
|---|---|---|
| `dry` | off | nobody. Deploy, webhook, preflight |
| `test` | on | the approver and one test address |
| `live` | on | any customer (a person still approves each message) |

## Before the first command: the conversation

Settle these with the merchant. None is a technical step and none can be skipped.

1. **Data processing agreement signed.** Customer names, email addresses and the
   message text pass through the service. `live` refuses without the flag that says
   this is done.
2. **Roles, as a working position to confirm in the DPA:** the merchant decides
   why their customers are contacted (controller) and we process on their
   instruction (processor). These are service messages about an existing
   subscription, not cold marketing, but the lawful basis is the merchant's to
   state, not ours to assume. A solicitor reads the DPA before the first one is
   signed.
3. **Who approves.** One named person with a mailbox they read. If that person is
   on holiday, requests expire after 48 hours and nothing sends. Say so.
4. **Where customers update a card.** The fallback link in the email. If the
   merchant has no page, the Stripe billing portal is the answer and the email links
   to the invoice's hosted page instead.
5. **Who creates the Stripe webhook.** Either the merchant adds the endpoint and
   gives us the signing secret, or they give us dashboard access. Prefer the first.
6. **No promised recovery rate.** Nothing has been measured on this merchant's own
   data, so none is quoted. The holdout arm is off (`PAYPILOT_HOLDOUT_PCT=0`)
   unless the merchant agrees to withhold dunning from a share of invoices.

## What to give me

Everything here is non-secret and can be said in a message.

| Detail | Example shape | Default if not said |
|---|---|---|
| Short name (the slug) | `acme` (4 to 24 chars, lowercase, hyphens) | none, required |
| Business name | `Acme Ltd` | none, required |
| Approver address(es) | `owner@acme.example` (comma separated for more than one) | none, required |
| Card-update page | `https://acme.example/billing` (https only) | none, required |
| Approval mode | `email` | `email` |
| Test address | any inbox you can read | the first approver |
| From address | only if the merchant verified a domain in Resend | `Acme Ltd via PayPilot <billing@streamflow.solutions>` |

Two secrets are NOT said in a message, ever. They go into the kit file locally:

* `RESEND_API_KEY`, the mail transport key.
* `STRIPE_WEBHOOK_SECRET`, from the endpoint created in step 3 below.
* Optional: `STRIPE_API_KEY`, a restricted key, for billing-portal links.

A key that appears in a transcript is burned, so a key is never pasted into a
chat, a command or a commit.

## The steps

1. **Make the kit.** `python3 scripts/new_merchant.py init SLUG --name "..."
   --approver ... --update-url ...` writes `merchant.json`, `fly.toml`,
   `secrets.env` and `RUNBOOK.md` into the private folder. It generates the admin
   token and PII salt without printing them. Stage is `dry`.
2. **Fill the two blanks** in `secrets.env` with an editor, locally.
3. **Install.** `ppmerchant SLUG`. It checks the kit, runs the offline tests and
   preflight, creates the app and volume if missing, pushes secrets over stdin,
   deploys, and reads the live host back. Exit 0 means installed and verified.
4. **Stripe webhook.** On the merchant's account add the endpoint
   `https://paypilot-SLUG.fly.dev/webhooks/stripe` for `invoice.payment_failed`,
   `invoice.paid`, `invoice.payment_succeeded`, `customer.subscription.deleted`.
   Put its signing secret in `secrets.env`, run `ppmerchant SLUG` again.
5. **Test stage.** `python3 scripts/new_merchant.py stage SLUG test`, then
   `ppmerchant SLUG`. Sending is now on for the approver and the test address only.
6. **One real test.** Fail one test payment. Check, and write down: the approval
   email arrives; the page shows the exact text; Send delivers exactly one email;
   the invoice moves to `messaged`; a second tap does nothing.
7. **Go live.** `python3 scripts/new_merchant.py stage SLUG live --dpa-signed
   --test-passed`, then `ppmerchant SLUG`. The flags are statements you are making;
   the tool cannot check them.
8. **Cron the sweep.** `POST /approvals/sweep` with the admin token every 15
   minutes, so overdue requests expire. (Mailbox modes need it to settle sends.)

## After it is live

* **First week:** read `GET /approvals?status=pending` daily. A pending request
  nobody has opened is the approver not seeing the email, not a bug.
* **Stop everything:** `fly secrets set PAYPILOT_SEND_EMAIL=0 -a paypilot-SLUG`,
  or `stage SLUG dry` and run the installer. Open requests expire by themselves.
* **A customer asks to be forgotten:** `scripts/gdpr_erase.py` against that
  merchant's app. It removes the ledger rows, including approval requests.
* **The merchant leaves:** export what they are owed, erase with the same script,
  then `fly apps destroy paypilot-SLUG` and delete the kit folder. Do both.

## Hosting a merchant as a tenant

Run on the machine that holds the ledger (`PAYPILOT_DB_PATH`, `PAYPILOT_TENANT_KEY`
set). `scripts/tenants.py` prints every token once and no secret ever.

1. **First time only: platform token.** `python3 scripts/tenants.py bootstrap`.
   Store the token in the password manager. After this, `ADMIN_TOKEN` opens nothing.
2. **Create the tenant.** `python3 scripts/tenants.py create SLUG --name "Merchant Ltd"
   --base-url https://YOUR-HOST`. It prints the three webhook URLs and the merchant
   admin's first token. The tenant sends NOTHING yet.
3. **Store the signing secret.** `echo "whsec_..." | python3 scripts/tenants.py secret
   SLUG stripe_webhook_secret`. The value is never shown again. Add `resend_api_key`
   the same way. Until the signing secret is stored, the webhook refuses.
4. **Set what it may do.** `python3 scripts/tenants.py platform SLUG --send off
   --recipient approver@merchant.example --from-domain merchant.example`. Keep
   `--send off` through the dry stage.
5. **Approvers and settings.** The merchant admin sets approvers, business name and
   update link with the token from step 2 (`PUT /admin/tenants/SLUG/config`).
6. **Point the merchant's Stripe endpoint** at `https://YOUR-HOST/t/SLUG/webhooks/stripe`.
7. **Check.** `python3 scripts/tenants.py doc SLUG` prints the sheet generated from live
   state: sending state, webhook secret state, tokens. Fail one test payment, confirm
   the approval request reaches the approver and nothing reaches a customer.
8. **Test, then live.** Same stage rules as above: `--send on` with only the approver
   and a test address, then widen the allowlist once the DPA is signed and a test
   message was approved and delivered.
9. **Cron the sweep.** `POST /approvals/sweep` with a platform token every 15 minutes.

Stop a tenant: `python3 scripts/tenants.py suspend SLUG --reason "..."`. It expires
pending approvals, drops dunning events and keeps recording payments that arrive, so
nobody who has paid is dunned. `resume` reverses it.

Remove a tenant: `python3 scripts/tenants.py offboard SLUG --yes-erase-everything
--export-to FILE`. It takes the tenant out of service, writes what the tenant is owed to FILE
(created first, so a bad path stops everything untouched), then erases its ledger rows,
configuration, decision history and bank consents. This cannot be undone. Set
`PAYPILOT_DECISION_AUDIT_DB_PATH` and `PAYPILOT_CONSENT_DB_PATH` in the shell so those two stores
are reached. If the erase fails part-way the tenant stays `offboarding`: run the same command again
with `--skip-export`.

## Things this SOP will not do

* Put a merchant on the demo app, or two merchants on one single-mode app.
* Quote a recovery rate, a lift or a saving. None has been measured.
* Send without a person pressing Send.
* Accept a secret in a chat. If one lands in one, it is rotated by the person who
  owns the account.
