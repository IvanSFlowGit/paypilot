# Handoff: update the outward surfaces when the bank rails and Chargebee are LIVE

STATUS ON 2026-10-18: STILL NOT ACTIONABLE. Re-measured on that date, see the
dated section below; the 2026-10-16 table is kept as the record of what was
true then. STATUS ON 2026-10-16: NOT ACTIONABLE. Nothing below may be written to any
surface yet, and the reason is in the preconditions rather than in anybody's
judgement.

This handoff exists because the plan it comes from asked for one, and because
the failure it guards against has already happened in this estate twice: a
listing claimed open and click tracking for six weeks after the transport that
produced those events had been swapped out, and a CV carried a status string
that went false while sitting in twelve outward-facing files. Both were true
when written. Neither had a condition attached.

## The three preconditions, all of which must hold

A surface is not updated when the code exists. It is updated when all three of
these are true, and the third is the one that gets assumed.

| # | Condition | Command that settles it | State on 2026-10-16 |
|---|---|---|---|
| 1 | BUILT | `.venv/bin/python -m pytest -q` and the adapter's own module docstring no longer says it has never seen a live payload | bank seam and Chargebee adapter both BUILT |
| 2 | DEPLOYED | fetch the running code and diff it against the source by marker, never by version number or dashboard state | NOT DEPLOYED |
| 3 | LIVE | one real payload has arrived from the provider and left a row: a Chargebee delivery with a real event id, or an AIS transaction read through a production aggregator credential | NOT LIVE |

Condition 3 is not satisfied by a test, a fake provider, a sandbox credential
or a payload we constructed. `app/chargebee_map.py` and `app/bank/fake.py` both
say in their own docstrings that nothing real has reached them, and those
sentences are the gate: while either is still true, the matching surface
sentence below stays unwritten.

## Why DEPLOYED and LIVE are separate conditions

A commit is not the files, a deploy is not the served page, and the served
page is not the running process. Beyond that, a deployed adapter that has never
received a delivery is a door nobody has walked through: the Chargebee route
has never authenticated a real request, and the shared secret it compares is
unset, so a claim that the integration works would be a claim about code
rather than about a system. The distinction is the same one the Recharge line
already has to make and it is the reason that line is worded as it is.

## Per surface, the exact sentence, and nothing more than it

Each row is written out in full so that whoever actions this is copying rather
than composing. A surface sentence composed fresh is how a credential sharpens
one retelling at a time.

| Surface | Sentence to add, verbatim | Condition it waits on |
|---|---|---|
| CVs, all four | `Chargebee, Stripe and Recharge payment-failure adapters in production.` | Chargebee 1, 2 and 3 |
| CVs, all four | `PSD2 account-information integration, read-only, consented.` | bank 1, 2 and 3 |
| README | `Adapters: Stripe, Recharge, Chargebee. Any other system posts two fields to /decide.` | Chargebee 1, 2 and 3 |
| README | `Bank rails: SEPA and Bacs return reasons, and an AIS feed where the ASPSP publishes one.` | bank 1, 2 and 3 |
| Site FAQ | `Which providers does it work with?` answered with the three adapters and the two-field route | Chargebee 1, 2 and 3 |
| Site copy | the sector list may add payments platforms other than Stripe | Chargebee 1, 2 and 3 |
| Public listing | nothing. The listing is governed by its own submission rules and is not touched by this handoff | n/a |

Two sentences are deliberately absent from that table. Nothing claims a
recovery percentage, because no verified figure exists. Nothing claims the
bank seam initiates a payment, because the protocol cannot express one, and a
negative capability claim is much harder to walk back than a positive scope
claim.

## Re-measured 2026-10-18: what changed and what did not

The 2026-10-16 table above said NOT DEPLOYED for both. That is superseded, and
it is superseded rather than overwritten so the reader can see the move.

| Piece | BUILT | DEPLOYED | LIVE | How each was established |
|---|---|---|---|---|
| Bank consent seam, FAKE provider only | yes | yes | NO | `/health` returned commit `a4fed2f`, equal to the pushed HEAD. A walk on the live host filed a consent and read it back through `GET /bank/consents/{id}`; the same id still read back after a redeploy and after `flyctl apps restart`, so the row is on the `/data` volume. |
| Bank seam, REAL aggregator adapter | yes (`berlin_group_ais`, AIS only) | registered, refuses | NO | `authorize_url` and `exchange_code` refuse without a credential. Gated on a data processing agreement. |
| Chargebee adapter and route | yes | yes (route in the published schema) | NO | Per `docs/claims.md` row 20 the secrets were unset and no delivery has arrived; not re-read on 2026-10-18, so that clause is carried forward and labelled as such. |

LIVE stays NO for all three because condition 3 asks for a payload that came
from the provider, and a fake provider, a walk we ran ourselves and a test are
each excluded by the condition's own wording. Moving DEPLOYED to yes changes
nothing that may be written on a surface.

Facts learned on the way that bound future wording:

- **Adapters register by import side effect.** Until 2026-10-17 nothing in
  `app/` imported them, so the live registry was empty and `provider=fake` was
  unknown in production while every test passed. A sentence saying an adapter
  is available is only as true as the fresh-interpreter test that pins it.
- **The fake provider is the only bank adapter that can run.** It talks to
  `bank.invalid`, which cannot resolve, and carries a FAKE marker on every value.
  Nothing it returns is evidence about a bank.
- **Pricing model decided 2026-10-18:** a platform fee scaled by failed-payment
  volume plus a fee per recovered payment, backed by the holdout arm. The site
  and the deck now say it and no figure appears on either. The holdout share is
  zero on the live app, so no lift can yet be reported.
- **No recovery percentage exists**, so none may appear on any surface.
- **No client traffic has reached the deployment.** README and the homepage say
  so; a CV sentence must not imply otherwise.

The verbatim sentence table above is unchanged and still waits on condition 3.

## What is already true and may be said today

These need no condition, because they are properties of this repository rather
than of a provider:

- The decision core is provider agnostic: two required fields, no vendor name
  in the input, and an unmapped failure code lands on a default row that names
  itself.
- The SEPA table is built from the European Payments Council's own current
  guidance, and the decision column comes from that document's own suggested
  creditor action column.
- Row level security is written and has never been executed against a Postgres
  server, which is the honest form of that sentence and is already in
  `docs/claims.md` row 15.

## When this handoff is actioned

Update every surface in ONE pass, because a fact that is true on the CV and
stale on the site is worse than one that is missing from both. Then delete the
row from this table, so the next reader cannot mistake a completed item for an
outstanding one.
