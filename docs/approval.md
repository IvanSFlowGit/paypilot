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
| `off` | Nothing. Mail sends unattended | Written agreement with the merchant. Preflight refuses to call it ready |

Proton has no public mail API, so a Proton merchant uses `email` mode. The
approval email lands in Proton and the page opens in any browser.

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

So PayPilot puts a custom header, `X-PayPilot-Approval`, on the draft and finds
the sent message by search. Tested here with a fake client and a mock transport.
**Not tested against a real mailbox**, and not yet known:

1. Whether Gmail keeps that header when a person sends the draft from the app.
   If it does not, matching falls back to recipient, subject and time, and the
   approval is recorded as `gmail:subject` instead of `gmail:header`.
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

Until Google verifies the app, only accounts on the OAuth consent screen's
test-user list can authorise it, and in testing status the refresh token expires
after a short period. (From memory of Google's policy, not checked here:
re-read it before relying on it.) Use `email` mode for any merchant who is not
on that list.

## Disclosure

With approval on, the AI disclosure line reads "drafted with AI assistance and
reviewed before it was sent", which is true. With `off`, it reads "drafted with
AI assistance", because nobody reviewed it.
