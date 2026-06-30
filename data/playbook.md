# PayPilot Dunning Playbook

Best-practices reference for recovering failed subscription payments. This document is
the RAG knowledge source: the retriever chunks it and feeds relevant snippets into the
diagnosis and message-drafting nodes. Keep it concise, actionable, and grounded in real
dunning practice.

## Guiding principles

- **Recover revenue, not goodwill.** A failed payment is almost never a churn decision -
  it's friction. Most involuntary churn is recoverable with a well-timed, polite nudge.
- **Lead with help, not blame.** The customer wants their service to keep working. Frame
  every message as "let's fix this together," never "you owe us."
- **Right message, right time.** Match retry cadence and tone to the *reason* the payment
  failed. A pushy reminder for a temporary funds issue erodes trust; a slow nudge for an
  expired card loses recoverable revenue.
- **Make the fix one click.** Always include a clear, single call to action (update card,
  retry now) and a direct link. Reduce every step the customer has to take.
- **Escalate gently.** Early attempts are soft reminders. Only later attempts mention
  service interruption, and even then with a clear path to stay subscribed.

## Failure reasons and how to handle them

### card_expired
The card on file has passed its expiration date. This is the most recoverable failure -
the customer almost always still wants the service and just needs to add a current card.

- **Diagnosis:** Expired payment method; no action possible until card details are updated.
- **Action:** Ask the customer to update their card. Retrying the same card will not help.
- **Retry timing:** Retry quickly (about 1 day) once a new card is expected, but the real
  unblock is the update-card link, not the retry.
- **Tone:** Friendly and matter-of-fact. This is routine housekeeping, not a problem.
- **Offer help?** Usually unnecessary; a clear update-card link is enough.

### insufficient_funds
The card is valid but the charge was declined for lack of available funds. This is almost
always temporary - paydays, pending deposits, and balance timing resolve on their own.

- **Diagnosis:** Temporary funding shortfall; the payment method itself is fine.
- **Action:** Wait, then retry. Do not pressure the customer or imply they did something wrong.
- **Retry timing:** Space retries out (about 3 days) to land after a likely paycheck or
  balance top-up. Retrying too soon just fails again and annoys the customer.
- **Tone:** Soft, understanding, low-pressure. Reassure them their account is safe for now.
- **Offer help?** If it recurs across multiple attempts, gently offer flexibility such as a
  short grace period, a smaller plan, or a pause - recovering some revenue beats churn.

### generic_decline
The processor declined the charge without a specific reason (bank risk rules, a temporary
hold, or an issuer-side block). Cause is ambiguous, so handle it as a recoverable middle case.

- **Diagnosis:** Unspecified decline; could be a temporary bank block or a card issue.
- **Action:** Retry once, and prompt the customer to check with their bank or try another card.
- **Retry timing:** A moderate gap (about 2 days) balances "issuer hold clears" against
  "card genuinely needs replacing."
- **Tone:** Calm and helpful. Acknowledge it may be on the bank's side, not theirs.
- **Offer help?** If the second attempt also fails, invite them to update their card or
  reply for assistance.

## Retry cadence summary

| Failure code        | Retry in | Primary action            | Tone               |
|---------------------|----------|---------------------------|--------------------|
| card_expired        | ~1 day   | Update card on file       | Friendly, routine  |
| insufficient_funds  | ~3 days  | Wait and retry            | Soft, no pressure  |
| generic_decline     | ~2 days  | Retry / check with bank   | Calm, helpful      |

As a rule, never hammer a card with rapid back-to-back retries - it raises decline rates and
can flag the account as fraudulent with the issuer. Fewer, better-timed attempts recover more.

## Tone and message guidelines

- Open warmly and reference the specific plan or service, so the email feels personal.
- State plainly that a payment didn't go through - no jargon, no shaming.
- Give one clear next step and a single button-style link. Avoid competing CTAs.
- Reassure the customer about what happens to their service in the meantime.
- Keep it short: a few sentences. Long dunning emails get ignored.
- Sign off as a helpful team, and invite a reply if they need anything.

## When to offer extra help

Offer accommodations when the standard nudge isn't working or the customer is clearly valuable:

- **Repeat failures** (multiple attempts on the same invoice) - escalate from reminder to a
  personal, human offer of help rather than another automated retry.
- **High-value customers** (higher MRR or long tenure) - bias toward generous, white-glove
  handling; a brief outreach or concession is cheaper than losing the account.
- **Insufficient-funds patterns** - proactively offer a pause, downgrade, or short grace
  period before they decide to cancel.

The goal of every interaction is the same: keep the customer subscribed and the service
running, with the least friction possible.
