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


## Update 2026-11-02: Loop Subscriptions, the count of 2,897, and rebuilding the CVs

Written after the Loop adapter landed (commits 764a77a, 0046258, d5129c1). It
does not change the three preconditions above. It applies them to a fourth
adapter and records the one outward surface that was updated, with the wording
that was allowed.

### Loop against the three preconditions

| # | Condition | State on 2026-11-02 | How it was established |
|---|---|---|---|
| 1 | BUILT | yes | `app/loop_subscriptions_map.py` and `POST /webhooks/loop`; `.venv/bin/python -m pytest -q` gave 2897 passed. The module docstring says it has never seen a live payload. |
| 2 | DEPLOYED | `/health` returned commit d5129c1 after the push; the Loop route itself was NOT probed | The deploy check printed `{"status":"ok","commit":"d5129c1..."}`. This session's sandbox cannot reach paypilot.fly.dev, so a request to `/webhooks/loop` with no key (expect 400) has not been made. Make it before any sentence says the route is deployed. |
| 3 | LIVE | NO | No Loop delivery has ever reached the code. Needs a Loop merchant's Admin API key to subscribe a webhook (`docs/claims.md` row 60), which is an owner step. |

### What was allowed on the LinkedIn project text, and why

The verbatim sentences in the table at the top (`... adapters in production`)
stay unwritten, because condition 3 is still NO for Chargebee and for Loop. The
LinkedIn project text instead NAMES Loop beside the three others and states the
limit in the same paragraph: `Chargebee and Loop have never seen a live payload.`
A name carrying its own limit is not an "in production" claim. The same text
says `Recharge and Loop run their own retries, so the message waits until they
stop`, which is a property of the code (claims row 63), not of a live system.
The text is 1,953 characters (1,981 if LinkedIn counts a newline as two) against
a 2,000 limit. Both send gates pass it. Paste it as is:

```
PayPilot decides what to do when a subscription payment fails, and records why.

A failed charge arrives with a reason code.
The rules table returns one action: retry on a named date, ask for a new card, or hand the invoice to a person.
When it is switched on, the model drafts the message. It never picks the action, and a person approves each message before it sends.

Integration is two fields: POST /decide takes an invoice ID and a failure code. Stripe, Recharge, Chargebee and Loop webhooks feed the same table, and an unmapped code falls through to a default that names itself, so a gap arrives as scoped work. Recharge and Loop run their own retries, so the message waits until they stop.

On the decision API, no configured token returns 503, never an allow.

An outcome is paid, not_paid or unknown. Unknown is the default, because nobody telling us is different from not paid.

Attribution uses a randomised holdout assigned by a hash of the invoice ID and a seed, so a disputed result recomputes from the IDs alone. With no baseline, the report returns no lift figure.

Multi-client mode tags each ledger row with its client and scopes queries to it, with an optional Postgres backend that adds forced row-level security.

A real PostgreSQL server found four bugs the offline checks missed, then 52 of 52 isolation and attack checks passed on throwaway PostgreSQL 15 and 18 clusters.

The ACH return-code table is also an MCP server, on PyPI and the official MCP Registry.

The same code deploys on Fly and as an arm64 Lambda. 2,897 automated checks run offline with no API key. Built with AI coding agents under my direction.

Limits: no second client uses the multi-client mode. Managed Postgres has not been tried.

Chargebee and Loop have never seen a live payload. The UK debit rail Bacs ships a refusal, because no Bacs list is freely readable.

Live at paypilot.fly.dev, with no client traffic.

Code: github.com/IvanSFlowGit/paypilot
```

### The count

2,897 automated checks. `pytest -q` and `pytest --collect-only -q` on Python
3.14.7 at d5129c1, none skipped. The register (`Documents/CVWORK/streamflowsnippets.md`)
holds it as the second 2026-11-02 measurement and marks 2,856 superseded.
`check_message_text.js` allows 2,897 and 2897 and refuses 2,856 (commit eb81b700
in the api-gateway clone, unpushed). If the suite grows again before the CVs go
out, re-measure and change the register and the gate TOGETHER: the gate file
says they are one edit, and it goes red on the canonical figure if only one moves.

### Rebuilding the CVs and one-pagers

Every CV variant (CVWORK holds Ivan_Skachek_AI_Automation_CV, AI_Engineer_CV and
FDE_CV as PDFs; the canon says four, and the fourth was not enumerated here, so
list the directory), the agency one-pager and the personal one-pager. Copy these lines; compose nothing fresh,
because a credential sharpens one retelling at a time.

CV bullet:

```
PayPilot: an AI dunning agent for failed subscription payments. A rules table decides every money action, the model writes only the message, and a person approves it before it sends. I added a multi-client mode with per-client encrypted secrets and row-level security on an optional Postgres backend, and published an MCP server over the ACH return-code table to PyPI and the official MCP Registry. 2,897 automated checks pass on Python 3.14, and 52 of 52 isolation and attack checks passed on throwaway PostgreSQL 15 and 18 clusters. A real Postgres server still found four bugs the offline checks missed, so I wrote them up. Built with AI coding agents under my direction.
```

Agency one-pager line:

```
PayPilot is our failed-payment recovery agent. Rules decide every money action, the model only writes the message, and a person approves it before it sends. Every row is tagged with its client and every query is scoped to that client. The multi-client mode is built and tested, and no second client is on it yet.
```

Personal one-pager line:

```
I built PayPilot end to end, an agent for failed subscription payments where rules decide the money and the model only writes the message. 2,897 automated checks passed offline and a real Postgres server still found four bugs, so I wrote them up.
```

Before regenerating:

- Run each line through `python3 ~/.claude/tools/check_message.py FILE` and
  `node ~/Documents/CVWORK/check_message_text.js FILE --claim`. All three above
  passed on 2026-11-02.
- `check_cvs.py` refuses a bare set difference, so a count change must be
  declared there as the retired old figure. Declare the previous PayPilot count
  retired in that file rather than editing around the gate.
- Only after the regeneration, run `check_assets.py` and read the rendered PDF
  text rather than the render log.
- The CV bullet carries no adapter list on purpose. Do not add `Chargebee,
  Stripe and Recharge ... in production` or any Loop equivalent until condition
  3 holds for it.

Do not say, whatever the source of the wording:

- That a second client uses multi-client mode (none does), or "production
  multi-tenant", "SOC2 compliant", "GDPR compliant". The words are readiness
  and ready.
- "Secure", "unhackable", "zero vulnerabilities"; any client name or recovery
  percentage.
- That the reviewers were people or a security firm. They were AI reviewer
  agents.
- That the shared-role weakness is gone everywhere. It is closed only in M9
  mode, which is off by default.
- 0.1.1 as "pending": it is published. PyPI lists 0.1.0 and 0.1.1 and the MCP
  Registry lists 0.1.1 as latest (claims row 59). Listed on Glama, PulseMCP or
  mcp.directory: NOT claimed (row 56 is relayed).
- Any "current rule" retry window: the Nacha text read is 2018 and the current
  rulebook is sold and unread (rows 57 and 58).
- Managed Postgres, RDS, Cloud SQL or Supabase: never checked (row 53).
- The deploy time as a general claim: row 54 is read from the Fly log.

### Posts

- The mail-integration post is live (lnkd.in/p/eu7XebJX, 2026-11-02).
- The Loop post was posted by its owner on 2026-11-02. It claims only that
  PayPilot reads Loop's `order/paymentFailed` webhook, that the message is held
  while Loop retries, and that no live delivery has been seen. Engagement is
  not a claim anywhere.

### Still open

- A real Loop delivery (needs a merchant's Admin API key). Until then rows 60 to
  63 stay as written, and the synthetic fixture in
  `tests/test_loop_subscriptions.py` stays the only payload.
- Probe the Loop route on the live host (no key, expect 400), then record it.
- Push the api-gateway gate commit and apply the copy diff there; both are the
  owner's steps.
- Rotate the Neon `neondb_owner` password pasted into chat on 2026-11-02.
