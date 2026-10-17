# PayPilot investor deck, November 2026

Source of record for the deck. Written 2026-10-17 for the story meeting on the
18th and the investor event on or about 17 November.

This file is the deck, not a description of one. It exists because the PDF that
was presented up to now was produced by a ReportLab script that is on no machine
in this estate and in no repository, so the deck rested on an artefact nobody
could open or amend. The content lives here, in a pushed repository, and the
rendering is a separate step named at the bottom.

Decision taken 2026-10-17: GROW THE PLAN rather than shrink the raise. Every
number below follows from that and from nothing else.

Slide boundaries are explicit. A renderer splits on the horizontal rules.

---

## Slide 1. Title

**PayPilot**

Recovering the subscription revenue that fails when nobody is watching.

Ivan Skachek. StreamFlow Solutions. November 2026.

---

## Slide 2. The problem

A subscription charge fails. Nobody is in the room.

The card expired, the bank declined, the mandate lapsed. The customer is not
refusing to pay and does not know anything happened. No human is present at the
moment of failure on either side, which is the whole difference between this
and every other kind of unpaid money.

A failure somebody is watching gets fixed in a minute, because the payer is
sitting at a checkout with another card in their hand. A failure nobody is
watching stays failed until a process finds it, and the subscription is
cancelled by the billing system rather than by the customer.

That is involuntary churn: revenue lost from customers who never chose to leave.

---

## Slide 3. Where the money goes

The leak is not the failed charge. It is the lifetime of the customer that
failed charge cancelled.

A merchant can see the failed charge in their processor. What they cannot see
is which of those failures their platform's own retry schedule was never going
to recover, and what each one cost them after the cancellation.

Every figure on the following slides is filled from the customer's own export,
not from an industry average. PayPilot's own report refuses to print a recovery
rate when it has no control group to compare against, which is the same
discipline applied to this deck: there is no claimed recovery percentage
anywhere in it.

---

## Slide 4. What a merchant does about it today

Three options exist and each one is a reason this product has a buyer.

**Nothing.** The platform's built in retry schedule fires, recovers what it
recovers, and the rest cancel. Nobody can say what was lost because nothing was
measured.

**A point solution on revenue share.** The vendor takes a percentage of what is
recovered. The merchant cannot verify the attribution, because the vendor and
the merchant disagree about which invoices would have self healed.

**A person.** Somebody in support chases failed payments by hand. It works and
it does not scale, and it stops the day that person is on holiday.

---

## Slide 5. Who is actually in this market

The funded point solutions in failed payment recovery are Butter Payments,
Gravy, Churn Buster and Revaly. Butter and Revaly price on revenue share.
Gravy is a managed human led service. Churn Buster has been at it since 2013.

Stripe and Recharge are not competitors. They are what PayPilot plugs into,
and their own retry schedules are the baseline PayPilot is measured against.

The adjacent companies that look similar and are not: Stuut is enterprise order
to cash and Adfin is UK SME invoicing. Both address an invoice a named human has
chosen not to pay, which is a reachable, chaseable person. That is a different
product with a different buyer.

Adfin gives credit control away free because it owns the payment rail at
20p plus 1 per cent. PayPilot owns no rail and never touches the money, so that
model is structurally unavailable here and the pricing on slide 8 is the right
instrument rather than a weaker one.

Status of this slide: the four recovery companies are recorded as relayed in
`docs/claims.md` rows 23 to 30 and are read first hand before 17 November. Stuut
and Adfin were read first hand on 2026-10-17.

---

## Slide 6. What PayPilot is

A decision layer. It decides what to do when a payment fails, and records why.

A failed charge arrives with a reason code. A deterministic rules table reads
that code and returns exactly one action: retry on a named date, ask for a new
card, or stop and hand the invoice to a person. The model drafts the message the
customer reads. It never picks the action.

Every money decision is a rules table. The model handles the language.

An unmapped reason code does not error. It falls through to a default that names
itself in the response, so a gap in the table arrives as a scoped piece of work
with a before and an after rather than as a silent wrong answer.

Three bank rails are registered. ACH and SEPA each ship a real table read from
the scheme's own published text, with the scheme's own suggested creditor action
per code rather than an action inferred here. Bacs ships a refusal instead of a
table, because no Bacs published list is freely readable and three secondary
sources disagree with each other about which codes exist. A refusal is the
honest output when the sources do not agree, and the registry refuses to import
if that declaration is ever quietly flipped.

---

## Slide 7. Why the audit trail is the thing that sells

Every decision lands in an append only row before it is returned. A decision
whose audit row cannot be written is refused, so nothing goes out unrecorded.

That is not a compliance feature bolted on. It is what makes this sellable to a
buyer whose finance team has to answer for the money, and it is the reason the
deterministic rules table is not a limitation. A buyer in payments cannot deploy
something that decides about money and cannot say why.

One client cannot read another's decisions. The client identity is resolved from
the bearer token and from nowhere else, and a client id supplied in the request
body is refused rather than trusted. A lookup under the wrong client returns 404
rather than 403, so it cannot confirm that somebody else's invoice exists.

With no token configured the service returns 503 and never an allow.

---

## Slide 8. Pricing, and the attribution problem answered

A platform fee scaled by failed payment volume, plus a fee per recovered
payment.

The second component is the one that normally cannot be defended, because the
merchant and the vendor disagree about which invoices would have been paid
anyway. PayPilot settles that in code rather than in a negotiation.

A configurable share of failures is assigned to a holdout arm: recorded in full,
never messaged. Assignment is a deterministic hash of the invoice id and a seed,
not a random draw, so the split is reproducible and a disputed result can be
recomputed from the invoice ids alone. The report compares the treated arm
against that randomised control, labels an observational baseline as
observational rather than passing it off as an experiment, and returns no lift
figure at all when there is no baseline to compare against.

So the outcome component is billed against a measured difference that either
party can recompute. That is the argument for charging on outcomes without the
revenue share dispute, and it is shipped code rather than a roadmap item:
`app/attribution.py` and `app/report.py`.

---

## Slide 9. The ask

**EUR 750,000.**

EUR 750,000 is a team sized cheque. The plan it funds has to be a team sized
plan, and that is the change in this version of the deck.

The previous milestone table reached EUR 225,000 of annual recurring revenue at
month 24, which is about 0.3 times ARR for every euro invested and is a plan one
founder can execute alone. A seed instrument against a solo plan is a mismatch
no presentation fixes.

The plan funded here reaches EUR 1,000,000 to EUR 1,500,000 at month 24, and it
gets there on roughly 20 customers at an average of EUR 60,000 rather than on 80
customers at EUR 15,000. Fewer customers, each materially larger.

That is a change of buyer, stated plainly: mid market subscription businesses
rather than small ones. A mid market buyer has a procurement process and asks
who decided and why, which is the question slide 7 answers and a smaller buyer
never asks. The audit trail is what makes the larger contract reachable.

The cheque funds four people for 24 months: the founder, two engineers and one
commercial hire.

---

## Slide 10. Milestones

| Month | Milestone |
|---|---|
| M3 | 5 design partners, each named in writing |
| M6 | 3 paid pilots at EUR 5,000, credited against production |
| M9 | First 2 production customers, first commercial hire |
| M12 | 6 production customers |
| M18 | 12 production customers |
| M24 | 20 production customers |

Every row is a customer count. The previous version put an ARR figure at M18 and
a customer count at M24, which made month 18 promise more revenue than month 24
once both were read against the EUR 15,000 contract on the pricing slide. A
count is a milestone the company controls. An ARR figure is the product of two
assumptions and goes stale the moment either moves.

ARR follows from the count and the average contract value, and both are stated
on slide 9 so the reader can do the multiplication themselves.

---

## Slide 11. Where it actually stands

Zero customers. Zero pilots. Zero revenue.

The product is deployed and running at paypilot.fly.dev and carries no client
traffic. `/decide` and `/decisions/{invoice_id}` are in the published OpenAPI
document. The test and evaluation suite is 1,555 automated checks across 49
modules, running offline in one pass with no API key.

The same decision code deploys two ways, on Fly and as an arm64 Lambda behind a
throttled API Gateway with Postgres in private subnets, and one contract suite
runs against both.

Built and not yet carrying live traffic, stated so nobody has to ask: the
outcome reporting route, the SEPA and Bacs rails, Chargebee, and row level
security on the Postgres path. Row level security has never been executed
against a real Postgres server. Chargebee has never seen a live payload.

One signed pilot before 17 November is worth more than every edit to this deck,
because it turns slide 10 from a plan into a first data point.

---

## Slide 12. Terms

A SAFE. The signed documents define the economics.

All targets in this deck are planning assumptions until customers validate them.
There is no claimed recovery rate anywhere in these twelve slides, because
nothing has recovered a payment for a paying customer yet, and a recovery figure
with no control group behind it is unfalsifiable by construction. The instrument
that will produce that figure is built, and it is named on slide 8.

---

## Rendering

No slide renderer exists in this estate. The previous PDF came from ReportLab,
that script is on no machine here, and ReportLab cannot be installed on this
machine because PyPI is unreachable from it: pip fails with
`SSLCertVerificationError('OSStatus -26276')`, the same error flyctl and the
GitHub CLI give, so it is the sandbox's TLS interception rather than anything
about the package.

A reading copy renders with `make deck` from the repository root, and that is
the whole command. It produces an A4 document rather than slides, which is the
right artefact for the story meeting and the wrong one for the room on
17 November.

`make deck` has to be run from a terminal rather than by an agent. Measured
twice on 2026-10-17: Chrome cannot start under the agent sandbox, which denies
it the Mach bootstrap and the creation of a unix socket, so it aborts on
`Failed to create a ProcessSingleton for your profile directory` whatever
`--user-data-dir` is pointed at. The two CONTENT gates in the renderer do run
under the sandbox and both pass on this file, zero banned characters and no
unfilled placeholders, so what needs a human is the rendering and not the
checking.

For slides on 17 November, paste the twelve sections into whichever tool
produced the earlier PDF. Note while doing it that the renderer reports
`Chromium produced no file` and discards the five lines of actual cause,
because it captures the subprocess output and never prints it. That is a defect
in a different repository and it cost a diagnosis here.
