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
author and not to an investor. Slide 6: the four recovery companies are recorded
as relayed in `docs/claims.md` rows 23 to 30 and are read first hand before
17 November; Stuut and Adfin were read first hand on 2026-10-17. Slide 12: one
signed pilot before 17 November is worth more than every edit to this deck,
because it turns slide 11 from a plan into a first data point.

<!-- deck:slides-begin -->
## Slide 1. Title

**PayPilot**

Lost subscription revenue. Period.

Ivan Skachek. StreamFlow Solutions. November 2026.

---

## Slide 2. The problem

A subscription charge fails. Nobody is in the room.

The customer never chose to leave. The billing system cancels them.

---

## Slide 3. What is lost

**22%** of subscription churn is involuntary. A payment failed. The customer did not decide.

On 10 million of revenue a year at 38% annual churn, that is 836,000 a year.

Stripe and Churnkey subscription data, 2024, SaaS average. Industry data, not PayPilot data. Read 2026-10-18.

---

## Slide 4. One customer, start to finish

A mid market subscription merchant on Stripe or Recharge.

**Problem.** Failed renewals cancel customers nobody chased.

**Today.** The platform's retry schedule, a point solution on revenue share, or a person. Triggers: charge fails, retry fires, customer cancels.

**PayPilot.** One recorded decision per failure, from a rules table.

**What they get.** Recovery measured against a held out control group, on their own export.

---

## Slide 5. What a merchant does about it today

Three options, and each is a reason this product has a buyer.

**Nothing.** The retry schedule fires and the rest cancel. Nobody measures the loss.

**A point solution on revenue share.** A percentage of recoveries nobody can verify.

**A person.** Chases by hand. Stops on holiday.

---

## Slide 6. Who is actually in this market

Four funded point solutions: Butter Payments, Gravy, Churn Buster and Revaly.

Stripe and Recharge are not competitors. They are what PayPilot plugs into.

Stuut and Adfin chase invoices a named person chose not to pay. A different product, a different buyer.

---

## Slide 7. What PayPilot is

A decision layer. It decides what to do when a payment fails, and records why.

A rules table picks the action. The model only drafts the words.

ACH and SEPA tables come from the schemes' own text. Bacs ships a refusal, because the sources disagree.

---

## Slide 8. Why the audit trail is the thing that sells

Every decision is recorded before it is returned.

No audit row, no decision. One client cannot read another's decisions.

---

## Slide 9. Pricing

A platform fee, plus a fee per recovered payment.

Recovery is measured against a held out control group, so either side can recompute it.

The holdout share is zero on the live app today, because it carries no traffic.

---

## Slide 10. The ask

**EUR 750,000**

24 months. About EUR 31,000 a month.

---

## Slide 11. Milestones

| Month | Milestone |
|---|---|
| M3 | 5 design partners, each named in writing. A design partner shares 90 days of failed payment data. |
| M6 | 3 paid pilots at EUR 5,000, credited against production |
| M9 | First 2 production customers, first commercial hire |
| M12 | 6 production customers |
| M18 | 12 production customers |
| M24 | 20 production customers |

Customer counts, not revenue. Planning assumptions until customers validate them.

---

## Slide 12. Where it actually stands

Zero customers. Zero pilots. Zero revenue.

Deployed at paypilot.fly.dev. No client traffic.

Built, not yet carrying traffic: the outcome route, the ACH and SEPA rails, Chargebee and row level security. Chargebee has never seen a live payload. Row level security has never run on a real Postgres server.

The test and evaluation suite runs offline in one pass with no API key.

---

## Slide 13. Terms

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
