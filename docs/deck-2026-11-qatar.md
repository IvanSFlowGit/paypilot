# PayPilot deck, Startup Qatar Investment Program, START track

Source of record for the Qatar deck. Written 2026-10-24 to be the Pitch Deck
attachment on the QDB application form at `f6s.com/sqinvestmentprogram`.

WHY THIS IS A SECOND FILE RATHER THAN AN EDIT. `deck-2026-11-investor.md` is
reviewed, pinned by `tests/test_deck.py` at exactly fourteen slides, and asks for
EUR 750,000 on a SAFE. QDB does not take a SAFE: its F6S profile states it
"Takes 5-50% equity" and its own FAQ says "The amount of funding shall be
trenched based on mutually agreed milestones". Different instrument, different
reader, and a reader who has already asked three questions in the form this deck
attaches to. Editing the reviewed deck to serve both would have given one file
two jobs.

AND THE RISK OF TWO FILES IS NAMED RATHER THAN IGNORED: a copy made for
distribution keeps the old behaviour after the source is fixed. The mitigation is
`tests/test_deck_qatar.py`, which asserts the properties that must hold across
BOTH decks rather than demanding the files match, so they are free to differ on
the ask and not free to differ on honesty.

WHAT CHANGED FROM THE INVESTOR DECK, so a reader of both can see it at once.
Slide 7 carries two more competitors, slide 8 is new and states the axis PayPilot
loses on, slide 10 is new and answers what the form asks about Qatar, slide 14
restates the same milestones as the tranche schedule QDB funds against, and slide
16 drops the SAFE. The amount is unchanged at EUR 750,000: QDB's START ceiling is
higher, about EUR 1.01M at today's rate, and asking for the ceiling rather than
the plan would be a number with no plan behind it.

THE THREE ANSWERS THIS DECK RESTS ON came from Ivan on 2026-10-24, asked
directly: he will set up operations in Qatar, hire locally, and commit full time
for the first two years. Nothing beyond those three is asserted. There is no
headcount, no month of relocation and no named local hire, because he has not
said one and the form asks for those in its own fields rather than here.

THE TWO COMPETITORS ADDED ON SLIDE 7 are the two the registers call the hardest,
and they were added because a table showing only the axes we win is a sales asset
rather than a comparison. Chargebee Revive, from `chargebee.com/payments/retries-and-dunning/`
read 2026-10-04: "Revive is available for Chargebee Billing customers", which is
the lock-in PayPilot's four-field API exists to avoid. FlyCode, from
`flycode.com/pricing.md` read 2026-10-04: it charges "only on revenue it actually
recovers above your existing baseline recovery rate", which is the closest thing
on this market to the control group this deck treats as its own. The four original
rows are `docs/claims.md` rows 37 to 40, all MEASURED on 2026-10-18.

AND THE DECISION RECORD COLUMN WAS DROPPED RATHER THAN THE CEILING RAISED. With
seven rows and four columns the slide came to 91 words against a ceiling of 70
that `test_deck.py` set by measuring the longest slide in the investor deck at
62. Raising the ceiling to fit this slide would be moving a threshold until the
control goes green. The column that went carried ONE bit, PayPilot yes and six
Not stated, and slide 11 is entirely about that bit, so the table was repeating
a whole slide six times. Trimming the charge cells and the two prose lines did
the rest. If this slide ever needs the column back it needs two slides, not a
higher ceiling.

EVERYTHING ABOVE THE MARKER IS INTERNAL AND IS NEVER RENDERED.

<!-- deck:slides-begin -->
## Slide 1. Title

**PayPilot**

PayPilot reclaims lost subscription revenue.

Ivan Skachek. StreamFlow Solutions. Startup Qatar Investment Program, START.

---

## Slide 2. The problem

A subscription charge fails. Nobody is in the room.

The customer never chose to leave. The billing system cancels them.

---

## Slide 3. What is lost

**836,000** a year in involuntary churn, on 10 million of revenue.

22% of subscription churn is involuntary. A payment failed. The customer did not decide. Every 1% of it won back is 8,360.

Arithmetic on Stripe and Churnkey subscription data, 2024, SaaS average. Industry data, not PayPilot data. Read 2026-10-18.

---

## Slide 4. One customer, start to finish

A B2B SaaS company billing cards through Stripe.

**Problem.** Failed renewals cancel customers nobody chased.

**Today.** The platform's retry schedule, a point solution on revenue share, or a person. Triggers: charge fails, retry fires, customer cancels.

**PayPilot.** One recorded decision per failure, from a rules table.

**What they get.** Recovery measured against a held out control group, on their own export.

---

## Slide 5. One failed charge

Synthetic example, not customer data.

1. A renewal charge fails. The reason: the card has expired.
2. The rules table picks one action: send a one-click link to update the card, and retry in 1 day.
3. The decision is written down with its reason, then returned.

---

## Slide 6. What a merchant does about it today

Three options, and each is a reason this product has a buyer.

**Nothing.** The retry schedule fires and the rest cancel. Nobody measures the loss.

**A point solution on revenue share.** A percentage of recoveries nobody can verify.

**A person.** Chases by hand. Stops on holiday.

---

## Slide 7. Who is actually in this market

Stripe and Recharge are what PayPilot plugs into, not competitors.

| | How they charge | Control group |
|---|---|---|
| Churn Buster | Flat fee by MRR | Not stated |
| Butter Payments | Share of recoveries | Not stated |
| Gravy | Custom flat fee | Not stated |
| Revaly | Flat plus performance | Not stated |
| Chargebee Revive | Chargebee Billing only | Not stated |
| FlyCode | Above your baseline | Baseline, not a control |
| PayPilot | Subscription | Built |

Read from their own sites, 2026-10-18 and 2026-10-04.

---

## Slide 8. Where PayPilot is behind

Churn Buster has run since 2013. It lists 15 integrations and over 1,000 accounts.

PayPilot has two live adapters and no customers.

What is being funded is the decision layer and the proof of it, not a head start.

---

## Slide 9. What PayPilot is

PayPilot decides what to do when a payment fails, and writes down why.

It reads the account's payment status and picks the next step from fixed rules. The model only drafts the words.

Bank debit rules come from the schemes' own published text. Where sources disagree, PayPilot says so instead of guessing.

---

## Slide 10. Why Qatar

Both founders will set up operations in Qatar, hire locally, and work on this full time for the first two years.

PayPilot takes four fields and returns a decision. It needs no local payment rail to run, so where it is built does not limit who it can serve.

---

## Slide 11. Every decision shows its reason

You can see why each payment was retried, chased or stopped.

Nothing is returned until the reason is written down.

---

## Slide 12. Pricing

A SaaS subscription, priced by failed payment volume.

Prices are set with the first design partners.

Value is measured against a control group, so either side can recompute it.

---

## Slide 13. The ask

**EUR 750,000**

24 months. About EUR 31,000 a month.

The money buys the data and reports that prove the numbers.

---

## Slide 14. The milestones it tranches against

| Month | Milestone |
|---|---|
| M3 | 5 design partners, each named in writing |
| M6 | 3 paid pilots at EUR 5,000, credited against production |
| M9 | First 2 production customers, first commercial hire |
| M12 | 6 production customers |
| M18 | 12 production customers |
| M24 | 20 production customers |

A design partner: an operations lead with live failed payment data. Shares 90 days of it, helps integrate, introduces customers.

Customer counts, not revenue. Assumptions until customers validate them.

---

## Slide 15. Where it actually stands

Zero customers. Zero pilots. Zero revenue.

Deployed at paypilot.fly.dev. No client traffic.

Built, not yet carrying traffic: the outcome route, the ACH and SEPA rails, Chargebee and the separation of one client's data from another's. Chargebee has never seen a live payload. The separation has never run on a real Postgres server.

The test and evaluation suite runs offline in one pass with no API key.

---

## Slide 16. Terms

Equity, on the stake and valuation QDB proposes.

All targets are planning assumptions until customers validate them. No recovery rate is claimed, because nothing has recovered a payment for a paying customer yet.

<!-- deck:slides-end -->

---

## Rendering and the other two attachments (internal, never rendered)

Same constraint as the investor deck and the same cause: no slide renderer exists
in this estate, ReportLab is on no machine here, and PyPI is unreachable from this
one because pip fails with `SSLCertVerificationError('OSStatus -26276')`, which is
the sandbox's TLS interception rather than anything about the package. `make deck`
renders a reading copy and has to be run from a terminal, because Chrome cannot
start under the agent sandbox. The CONTENT gates do run here.

THE FORM WANTS THREE ATTACHMENTS, maximum 30MB each: a Pitch Deck, a Founder(s)
Profile and Historical & Projected Financials. This file is the first. The other
two do not exist, and the application must not be STARTED before they do, because
the form carries "I hereby commit to finalizing this application within 30 days
from the starting date". Starting it early spends the clock on waiting.

AND THE FORM ASKS FOR THE SIZE OF THE CUSTOMER BASE. The answer is zero, slide 15
says so, and START's published bar is market validation. That is the one thing in
this application nothing in the deck can fix, and the honest ordering is that one
signed design partner is worth more than every edit to this file.
