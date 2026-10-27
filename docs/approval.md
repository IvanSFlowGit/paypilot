# Human approval of outbound mail

PayPilot drafts the recovery email. A named person reads the exact text and says
send or skip. Nothing reaches the merchant's customer before that.

This is the default. `PAYPILOT_APPROVAL_MODE` unset, or set to anything it does
not recognise, means a person approves. Unattended sending is `off`, and `off`
has to be written out.

## Modes

| Mode | What the approver does | Needs |
|---|---|---|
| `email` (default) | Opens the email PayPilot sent them, taps the link, reads the page, presses **Send this email** or **Skip** | A Resend key, an approver address, a public https URL. Works in the Gmail app, Proton, anything |
| `gmail` | Opens the draft in their own Gmail Drafts folder and presses Send there | Everything above, plus Google OAuth credentials and, beyond a small test-user list, Google app verification |
| `imap` | Opens the draft in their mail app and presses Send there. Proton Mail via Bridge, or any IMAP mailbox | An IMAP host PayPilot can reach. See the IMAP section |
| `off` | Nothing. Mail sends unattended | Written agreement with the merchant. Preflight refuses to call it ready |

**Which mode for which merchant**

| Merchant | Use | Why |
|---|---|---|
| Gmail or Google Workspace, hosted PayPilot | `email` | Approver taps a link in the Gmail app. No Google verification, no tokens that expire |
| Proton Mail, hosted PayPilot | `email` | Proton has no public mail API, and Bridge cannot be reached from Fly |
| Proton Mail, PayPilot on the merchant's own machine | `imap` | Bridge is a local mail server on that machine |
| Wants the approval inside their own Drafts folder | `gmail` or `imap` | Only after the live checks below |

The approval email lands in Proton or Gmail like any other mail, and the page
opens in any browser, so `email` mode needs nothing from either provider.

## What `email` mode guarantees

* **Opening the link sends nothing.** Mail scanners and link previewers fetch
  every URL. The link shows the draft. Sending is a POST from the page.
* **What was read is what is sent.** The body is frozen and hashed when the
  request opens. A changed body is refused, not sent.
* **One decision, once.** The claim is one atomic database update. A double tap
  or a replayed request loses after the first.
* **Re-checked at the moment of sending.** If the customer paid in the hours
  between request and tap, the invoice is closed and nothing is sent. The touch
  cap and the cooldown are checked again too.
* **48 hours, then nothing.** `PAYPILOT_APPROVAL_TTL_HOURS`. An expired request
  sends nothing and is recorded as `approval_expired`.
* **Skip is remembered.** Stripe retries emit a new failure event each time. A
  skipped invoice is not asked about again.
* **The token is stored as a hash.** The raw token exists only in the email.
* **Fails closed.** No approver, no public URL, or an undeliverable request
  means the message is suppressed with a reason.

## Onboarding a merchant

1. Merchant connects Stripe (or Recharge). Set their webhook secret.
2. Set, on the deployment: `PAYPILOT_ALLOWED_RECIPIENTS`, `RESEND_API_KEY`,
   `PAYPILOT_APPROVER_EMAILS` (the person who will approve),
   `PAYPILOT_PUBLIC_BASE_URL` (https), `ADMIN_TOKEN`, `STRIPE_WEBHOOK_SECRET`.
3. Run `python3 scripts/approval_preflight.py --send-test`. Exit 0 and
   `READY`, and the approver should have received the test notice.
4. Switch on `PAYPILOT_SEND_EMAIL=1`.
5. Trigger one real failed payment on a test customer. Confirm the approval email
   arrives, the page shows the text, and Send delivers exactly one email.
6. Cron `POST /approvals/sweep` (admin token) every 15 minutes. It expires
   overdue requests. It is also what settles Gmail drafts.

A data processing agreement with the merchant is signed before step 1.
Customer names, addresses and the message text pass through this service.

## Admin

* `GET /approvals?status=pending`: requests, newest first. No bodies, no hashes.
* `POST /approvals/sweep`: expire overdue requests, reconcile Gmail drafts.

Both need `Authorization: Bearer $ADMIN_TOKEN`.

## Gmail mode: what is and is not verified

Read from Google's documentation on 2026-10-27:

* `gmail.compose`, `gmail.readonly` and `gmail.metadata` are **Restricted**
  scopes. Detecting a send needs a read scope as well as compose.
* On send, "the draft is automatically deleted and a new message with an updated
  ID is created". A draft cannot be followed to its sent message by id.
* An external OAuth app in **Testing** status is "limited to up to 100 test
  users", and "authorizations by a test user will expire seven days from the time
  of consent", refresh tokens included. A production gmail-mode merchant cannot
  run on Testing: PayPilot would stop being able to see sends every week.
* In production but unverified, the app shows an "Unverified apps warning" and
  is capped at "100 new users in total", for the life of the project.
* A refresh token is also invalidated when "the user changed passwords and the
  refresh token contains Gmail scopes", after six months unused, and beyond 100
  tokens per account per client the oldest is dropped "without warning".

So Gmail API mode needs Google's app verification before it is a product, and
until then it is for an internal pilot with accounts on the test-user list,
re-authorised weekly. Not read: the verification page's own criteria and any
security assessment requirement, which the page returned truncated.

PayPilot puts a custom header, `X-PayPilot-Approval`, on the draft and finds the
sent message by search. Tested here with a fake client and a mock transport.
**Not tested against a real mailbox**, and not yet known:

1. Whether Gmail keeps that header when a person sends the draft from the app.
   If not, matching falls back to recipient, subject and time, and the approval
   is recorded as `gmail:subject` instead of `gmail:header`.
2. How long after a send the message is searchable. A vanished draft is held as
   pending for `PAYPILOT_GMAIL_GRACE_MINUTES` (15) before it counts as skipped.
3. Whether a person editing the draft before sending changes the match.

To settle them, a human with a Google account runs, once:

```
python3 scripts/approval_preflight.py --gmail-live
```

(one token refresh, one throwaway draft created and deleted), then triggers one
real failure, sends the resulting draft from the Gmail app, and runs
`POST /approvals/sweep`. The approval's `resolved_by` says `gmail:header` or
`gmail:subject`. Record which.

## IMAP mode: Proton Mail (via Bridge) and any IMAP mailbox

PayPilot speaks plain IMAP: it appends the draft to a Drafts folder, and later
searches Drafts and Sent for the `X-PayPilot-Approval` header.

**Proton Mail.** Read from proton.me/mail/bridge on 2026-10-27: Bridge "is
available only with a paid plan", is "a desktop application that runs in the
background", sets up "a local email server on your computer", and uses "a
separate password from your login password". So:

* A free Proton account cannot use this mode.
* PayPilot has to run where Bridge runs. A Bridge on a merchant's laptop is not
  reachable from Fly. Hosted Proton merchants use `email` mode.
* The Bridge host, port and connection security are **not** in the pages read, so
  there are no defaults. Read them from the Bridge window. Preflight refuses to
  call the setup ready until host, port, user and password are all set.
* Bridge's certificate is self-signed. PayPilot skips certificate verification
  for loopback addresses only (`127.0.0.1`, `localhost`, `::1`), where nothing
  outside the machine can intercept the connection, and verifies everything else.

**Gmail over IMAP with an app password** avoids Google's restricted-scope
verification entirely. Google says app passwords "can only be used with accounts
that have 2-Step Verification turned on", may not be offered on "a work, school,
or another organization account", and "aren't recommended and are unnecessary in
most cases" (support.google.com/accounts/answer/185833). Use it for an individual
Gmail account PayPilot runs next to, not as a general product path. Set
`PAYPILOT_IMAP_DRAFTS_FOLDER=[Gmail]/Drafts` and
`PAYPILOT_IMAP_SENT_FOLDER=[Gmail]/Sent Mail`.

Settings (all read by `app/imap_drafts.py`): `PAYPILOT_IMAP_HOST`,
`PAYPILOT_IMAP_PORT`, `PAYPILOT_IMAP_USER`, `PAYPILOT_IMAP_PASSWORD`,
`PAYPILOT_IMAP_SECURITY` (`ssl` or `starttls`, anything else refuses to connect),
`PAYPILOT_IMAP_DRAFTS_FOLDER`, `PAYPILOT_IMAP_SENT_FOLDER`.

Tested against an in-memory IMAP server that answers the same command shapes
`imaplib` sends: search quoting and injection refusal, exact-marker matching on a
substring-matching search, exact-time filtering, CRLF on append, TLS rules,
connection closing. **Not tested against a real server**, so the human-run check
is:

```
python3 scripts/approval_preflight.py --imap-live
```

which connects, appends a throwaway draft, finds it, deletes it and confirms it
is gone. Then trigger one real failure, send the draft from the mail app, run
`POST /approvals/sweep`, and record whether `resolved_by` says `imap:header` or
`imap:subject`. The same two open questions as Gmail apply, plus whether the
server's `HEADER` search finds the marker at all (every hit is re-checked for an
exact match, but a server that never finds it would look like a refusal after the
grace period).

## Disclosure

With approval on, the AI disclosure line reads "drafted with AI assistance and
reviewed before it was sent", which is true. With `off`, it reads "drafted with
AI assistance", because nobody reviewed it.
