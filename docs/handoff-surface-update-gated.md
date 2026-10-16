# Handoff: update the outward surfaces when the bank rails and Chargebee are LIVE

STATUS ON 2026-10-16: NOT ACTIONABLE. Nothing below may be written to any
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
