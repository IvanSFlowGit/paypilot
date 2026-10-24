# PayPilot investor deck, November 2026

Source of record for the deck. Written 2026-10-17 for the story meeting on the
18th and the investor event on or about 17 November.

This file is the deck, not a description of one. It exists because the PDF that
was presented up to now was produced by a ReportLab script that is on no machine
in this estate and in no repository, so the deck rested on an artefact nobody
could open or amend. The content lives here, in a pushed repository, and the
rendering is a separate step named at the bottom.

Revised 2026-10-18 after feedback on the first version: as few words as possible, one
customer journey, and an ask slide that is the number and nothing else. The long text
of each slide was cut, not lost: it lives in git history at 6309d25 and in the speaker
notes of the shared deck artefact. No figure was added; the journey slide prints none
because no customer export exists.
Round two, same day: trigger points added to the journey, headcount dropped from the ask
(roles undecided), design partner defined on the milestone slide.

Decision taken 2026-10-17: GROW THE PLAN rather than shrink the raise. Every
number below follows from that and from nothing else.

EVERYTHING ABOVE THE MARKER BELOW IS INTERNAL AND IS NEVER RENDERED. It is
here because a renderer that splits on horizontal rules alone would make this
header slide one, which would open an investor presentation with the sentence
"GROW THE PLAN rather than shrink the raise" and hand the reader the argument
that the ask could have been a third of the size. A positive marker the
document applies to itself, rather than an instruction somebody has to
remember to follow.

Render only what sits between the two markers, splitting on the horizontal
rules inside them.

Notes moved off the slides on 2026-10-17 because they were addressed to the
author and not to an investor. Slide 13: one signed pilot before 17 November is
worth more than every edit to this deck, because it turns slide 12 from a plan
into a first data point.

SLIDE 7 IS DONE AND THIS NOTE USED TO SAY OTHERWISE. It read: the four recovery
companies are recorded as relayed in `docs/claims.md` rows 23 to 30 and are read
first hand before 17 November. Both halves were wrong by 2026-10-24. Rows 23 to
30 are Stuut and Adfin, which row 28 itself says are ACCOUNTS RECEIVABLE products
and not competitors at all. The four on the slide are rows 37 to 40, and all four
are MEASURED rather than relayed: raw reads of `butterpayments.com`,
`churnbuster.io`, `revaly.co` and `gravysolutions.io` on 2026-10-18, each row
carrying its own re-fetch command. So the note was pointing fourteen rows away
from its own evidence and asking for work that had been done the day after it was
written. Corrected rather than deleted, because a stale TODO in a source of
record is read as outstanding and costs somebody four re-reads.

WHAT SLIDE 7 ACTUALLY STILL LACKS, which is a different thing: it compares
pricing and two PayPilot differentiators, and it compares no FEATURES. The
honest feature axes exist in the registers and three of them run AGAINST
PayPilot: Churn Buster has 15 integrations and has run since 2013 against two
live adapters here, and PayPilot has zero customers against its 1,000+ accounts.
A comparison showing only the axes we win is a sales asset rather than a
comparison. Two competitors the registers call the hardest are also absent:
Chargebee Revive, which the jobsearch register calls the real competitor and
which is locked to Chargebee Billing customers, and FlyCode, which bills "above
your existing baseline recovery rate" and so is the closest thing on the market
to the control group this deck treats as unique. Adding them is a positioning
decision and is Ivan's.

<!-- deck:slides-begin -->
## Slide 1. Title

**PayPilot**

PayPilot reclaims lost subscription revenue.

Ivan Skachek. StreamFlow Solutions. November 2026.

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

Stripe and Recharge are not competitors. They are what PayPilot plugs into.

| | How they charge | Control group | Decision record |
|---|---|---|---|
| Butter Payments | Share of recoveries | Not stated | Not stated |
| Gravy | Custom flat fee | Not stated | Not stated |
| Churn Buster | Flat fee by MRR | Not stated | Not stated |
| Revaly | Flat fee plus performance | Not stated | Not stated |
| PayPilot | Subscription | Built | Yes |

Not stated on their websites, read 2026-10-18.

---

## Slide 8. What PayPilot is

PayPilot decides what to do when a payment fails, and writes down why.

It reads the account's payment status and picks the next step from fixed rules. The model only drafts the words.

Bank debit rules come from the schemes' own published text. Where sources disagree, PayPilot says so instead of guessing.

---

## Slide 9. Every decision shows its reason

You can see why each payment was retried, chased or stopped.

Nothing is returned until the reason is written down.

---

## Slide 10. Pricing

A SaaS subscription, priced by failed payment volume.

Prices are set with the first design partners.

Value is measured against a control group, so either side can recompute it.

---

## Slide 11. The ask

**EUR 750,000**

24 months. About EUR 31,000 a month.

The money buys the data and reports that prove the numbers.

---

## Slide 12. Milestones

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

## Slide 13. Where it actually stands

Zero customers. Zero pilots. Zero revenue.

Deployed at paypilot.fly.dev. No client traffic.

Built, not yet carrying traffic: the outcome route, the ACH and SEPA rails, Chargebee and the separation of one client's data from another's. Chargebee has never seen a live payload. The separation has never run on a real Postgres server.

The test and evaluation suite runs offline in one pass with no API key.

---

## Slide 14. Terms

A SAFE. The signed documents define the economics.

All targets are planning assumptions until customers validate them. No recovery rate is claimed, because nothing has recovered a payment for a paying customer yet.

<!-- deck:slides-end -->

---

## Rendering (internal, never rendered)

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
