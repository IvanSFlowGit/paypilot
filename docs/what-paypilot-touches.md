# What PayPilot touches

One page for a client's IT or finance lead. Every statement here is pinned by
`tests/test_egress_inventory.py`: if the code changes what it can reach, that test fails until the change is reviewed and this page is updated.

## The short version

PayPilot reads failed-payment events, decides what to do, and writes a message for a person to approve.
By default **it sends nothing**, and by default **a person approves every message**.

## What goes out of PayPilot

| To | What | Why | When |
|---|---|---|---|
| Resend (`api.resend.com`) | One email per approved message | The approved message is delivered to your customer | Only when sending is switched on AND a person approved it AND the recipient is on your allowlist |
| Stripe (`api.stripe.com`) | One call: create a billing-portal session | Gives the customer a card-update link | Only when drafting a message that needs the link. Creates a link. Moves no money. |
| Google (`gmail.googleapis.com`, `oauth2.googleapis.com`) | Create, read and delete **drafts** in a mailbox you connect; read the Subject, To and one PayPilot header of recent sent messages to the customer in question | Puts the proposed message in your own Drafts folder so you review it in Gmail, and notices when you sent it | Only in Gmail mode. No send call exists in the code. |
| Your IMAP server | Add, find and delete PayPilot's own draft in your Drafts folder | Same, for Proton Mail Bridge or any IMAP mailbox | Only in IMAP mode. |
| OpenAI (`api.openai.com`) | The wording task, with names and addresses masked | Polishes the message text | Only when LLM drafting is switched on. Off by default. |
| Mercury (`api.mercury.com`) | Read-only account lookup | Confirms a payment arrived | Only when you connect a Mercury token. |

Nothing else. There is no SMTP, no raw socket and no other HTTP client in the application code.

One addition, only when the operator turns it on: with `PAYPILOT_DB_BACKEND=postgres` the application also opens a verified-TLS connection to the operator's own Postgres server (through the `pg8000` driver, not a raw socket in application code). That connection carries the ledger and goes nowhere near your systems. It is off by default and is not used in a per-client SQLite deployment.

## What PayPilot never does

* It does not charge a card, refund, capture, transfer or pay out. The only Stripe write is the
  billing-portal link above.
* It does not send from your Gmail or IMAP mailbox. In those modes it **drafts**; a person presses Send.
* It does not read your inbox or message bodies. In Gmail mode it reads headers (Subject, To and its own marker) of recent sent mail to the one customer address, to see whether you sent the draft. In IMAP mode it looks for its own draft by marker.
* It does not contact your customers on its own. A person approves each message, or the message does not go.
* It does not write to your systems. Your Stripe account, your billing system and your CRM are read
  from the events you send us; nothing is written back.

## Safe defaults

* Sending is **off** until the operator switches it on for your account.
* Recipients must be on an allowlist. Anyone else is refused.
* Every message passes an output check: no unexpected links, no secrets, no placeholders.
* A suspended account is never mailed, and its pending approvals expire.
* The signing secret for your events is write-only: it is stored encrypted and cannot be read back.

## What you control

* Which events you send (a webhook URL specific to your account).
* Who approves, and by which route: the emailed link, or your own Gmail / IMAP Drafts folder.
* The allowlist of customers who may be mailed.
* Pausing or ending the account: suspension stops everything; offboarding exports your data and erases it.

## Data

Per-customer events and messages are stored in your account's own partition of the ledger. PayPilot masks names and
addresses in text sent to a language model (see `app/pii.py`). Right-of-access export and erasure are one command each.

Questions go to the operator listed in your onboarding sheet.
