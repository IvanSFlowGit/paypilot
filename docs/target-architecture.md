# PayPilot: target architecture

Read date for every measurement below: 2026-10-16, on this repository at the
commit that added this file. Every number here was produced by running the
code rather than by reading it, and `tests/test_arch_doc_current_state.py`
asserts each one against the code on every run, so the current-state section
cannot go stale while still reading as a measurement.

Two things in the approved plan were refuted by measuring the code, and they
are stated here as the code has them rather than as the plan had them. The
Bacs module ships a table with provenance rather than refusing at import,
because the four readable sources turned out to agree exactly (`docs/claims.md`
row 9). The decision-API outcome went in as its own table, `decision_outcomes`,
rather than as three columns bolted onto `decision_audit`.

## 1. Current state

```
                      +----------------------------+
   Stripe webhook --> |                            |
   Recharge webhook ->|  app/api.py  (FastAPI)     |
   Chargebee webhook->|  17 published paths        |
                      |  20 paths hidden from the  |
   POST /decide ----->|  OpenAPI document          |
                      +-------------+--------------+
                                    |
              +---------------------+---------------------+
              |                     |                     |
     +--------v-------+   +---------v--------+   +--------v---------+
     | app/ingest.py  |   | app/decision.py  |   | app/rails.py     |
     | normalise      |   | DETERMINISTIC    |   | registry over    |
     | + retrieval    |   | strategy table   |   | ach, bacs, sepa  |
     +--------+-------+   +---------+--------+   +--------+---------+
              |                     |                     |
     +--------v-------+   +---------v--------+   +--------v---------+
     | app/graph.py   |   | decision_audit   |   | per-rail tables  |
     | LangGraph:     |   | decision_outcomes|   | sepa 25 codes    |
     | diagnose,      |   | SQLite or        |   | bacs 3 reports   |
     | draft copy     |   | Postgres + RLS   |   | ach code map     |
     +--------+-------+   +------------------+   +------------------+
              |
     +--------v-------+        +--------------------------------+
     | app/mailer.py  |        | app/bank/  READ-ONLY SEAM      |
     | human gate     |        | protocol has 7 methods and     |
     +----------------+        | none of them moves money       |
                               | fake provider runs offline     |
                               +--------------------------------+
```

Measured, each figure asserted by the paired test:

- `app/rails.py` registers exactly three rails: `ach`, `bacs`, `sepa`.
- `app/sepa_return_map.py` carries 25 codes, scoped `core`, `b2b` and `both`,
  with the decision column taken from the scheme's own *Suggested Creditor
  action* column rather than from inference.
- `app/bacs_return_map.py` carries three report types, `ARUDD`, `ADDACS` and
  `AUDDIS`, and 4 symbols that are valid in more than one of them with
  different meanings. `report` is a required argument for that reason.
- `decision_audit` holds `id, invoice_id, client_id, rule_fired, decided_at,
  input, decision`.
- `decision_outcomes` holds `id, invoice_id, client_id, outcome, reported_at,
  report_key, source`. The outcome is three-valued: `paid`, `not_paid`,
  `unknown`. Unknown is the default and means nobody has told us, which is not
  the same as not paid.
- `POST /decide` accepts four fields and REQUIRES two of them, `invoice_id` and
  `failure_code`. `attempt` defaults to 1 and `prior_failures` to 0, so the
  integration surface is smaller than the validation function looks.
- 17 paths are published in the OpenAPI document and 16 are hidden from it. A
  route hidden from the schema does not exist commercially, which is why the
  decision, outcome and bank routes are all published.
- `app/bank/provider.py` declares `authorize_url`, `exchange_code`, `refresh`,
  `accounts`, `transactions`, `verify_webhook` and `parse_return_event`. No
  method moves money, so no adapter can implement one, and an import-time
  assertion refuses any adapter declaring a payment-initiation path.
- `/metrics` publishes `latency_recent` with percentiles beside the old
  `avg_latency_ms`, and an `inference` block carrying token counts and cost.
- `app/ingest.py:102` calls `OpenAIEmbeddings()` with no model argument, so the
  library default decides and moves on a dependency bump.

## 2. Problems with the current state

1. **The outcome loop is closed on the webhook path and open nowhere else.**
   `app/loop.py` writes `recovered_amount_minor` and dedupes redeliveries, so
   the demo loop can be measured. The decision API now has
   `decision_outcomes`, and no client has ever reported one, so the recovery
   claim is measurable rather than measured.
2. **Row level security is written and has never met a Postgres.**
   `app/decision_schema.sql` emits `ENABLE` and `FORCE ROW LEVEL SECURITY` per
   table and the bootstrap probes both directions. No cluster can start in the
   sandbox, so the probe's wiring is proven against a fake and the database
   behaviour is NOT RUN (`docs/claims.md` row 15).
3. **Three adapters have been written and one has seen a live payload.**
   Stripe has. Recharge and Chargebee are written from published
   documentation, and both modules say so in their own docstrings.
4. **The embedding model is whatever the library defaults to**, which is a
   silent dependency on somebody else's release notes.
5. **A rail reason code may not reach us at all.** One ASPSP publishes a
   transaction field carrying the ISO 20022 return reason, and it is marked
   Mandatory N, so a conformant feed can omit it. The rail tables are how the
   reason arrives when the feed does not carry it.

## 3. Future state

```
   Merchant systems                 PayPilot                        Rails
   ----------------                 --------                        -----
   Stripe      ---\            +------------------+
   Recharge     ---+---------> | adapters/        |
   Chargebee   ---/            | one per provider |
                               +--------+---------+
                                        |  normalised failure event
   Any other system ----------> POST /decide
                                        |
                               +--------v---------+        +----------------+
                               | decision core    |<-------| rails registry |
                               | pure, no vendor  |        | ach bacs sepa  |
                               | names, no I/O    |        | scheme tables  |
                               +--------+---------+        +----------------+
                                        |
                       +----------------+----------------+
                       |                                 |
              +--------v--------+              +---------v---------+
              | decision_audit  |              | the caller acts   |
              | decision_outcome|              | we never send on  |
              | RLS per tenant  |              | their behalf      |
              +--------+--------+              +---------+---------+
                       |                                 |
                       |                       POST /decisions/{id}/outcome
                       +<--------------------------------+
                                  the loop closes here

              +--------------------------------------------+
              | app/bank/  AIS only, read-only by          |
              | construction. Consent tokens encrypted,    |
              | refuses when the key is unset. No PIS.     |
              +--------------------------------------------+
```

The shape of the change is not new components. It is that the decision core
stops knowing any provider's name, the adapters become the only place a vendor
appears, and the outcome route is the thing a client is asked to call so the
recovery rate stops being a property of the demo.

## 4. Service boundaries

| Boundary | Testable claim at it | Who may write |
|---|---|---|
| adapter to core | the normalised event carries no provider name | adapter |
| core to rules | the same input gives the same decision, no I/O | nobody, pure |
| core to audit | one row per decision, carrying its rule | core |
| client to outcome | idempotent on the report key | client |
| core to rails | a rail answers only for the report it was asked about | rails |
| app to bank | no method on the protocol moves money | bank adapters |
| bank to consent | tokens only, never bank credentials | consent store |

The seam that matters commercially is adapter to core: it is what makes the
answer to "we are not on Stripe" be two fields rather than a build.

## 5. API contracts

```
POST /decide
  in   { invoice_id, failure_code, attempt?, prior_failures? }
  out  { action, reason, rule_fired, retry_after_days?, message? }
  note unknown fields are refused; an unmapped failure code lands on a
       default row that NAMES ITSELF, so an incomplete mapping is billable
       rather than silent.

POST /decisions/{invoice_id}/outcome
  in   { outcome: paid | not_paid | unknown, report_key, source? }
  out  { recorded: bool, idempotent?: true }
  note client_id comes from the bearer token. A client_id in the body is
       REFUSED, because a caller that declares its own identity makes the
       audit row inherit the lie.

GET  /decisions/outcomes/summary
  out counts per outcome with unknown as its own number, never folded into
      either side. An empty arm reports null rather than 0.0.

POST /webhooks/{stripe,recharge,chargebee}
  auth stripe and recharge verify the body; CHARGEBEE SIGNS NOTHING and its
       route says so rather than implying parity.

GET  /bank/consent/start, /bank/consent/callback
POST /webhooks/bank/{provider}
  all three are reads. Nothing in this package initiates a payment.
```

## 6. Data ownership

| Data | Owner | Where | Retention |
|---|---|---|---|
| decision input and output | the client | `decision_audit`, per `client_id` | audit, append only |
| outcome reports | the client | `decision_outcomes` | audit, append only |
| scheme tables | the scheme | CSV in repo with provenance | versioned with the doc |
| bank consent tokens | the end customer | `app/bank/consent.py`, encrypted | until consent expiry |
| customer name, email | the merchant | never stored by the decision API | n/a |
| inference spend | us | audit rows, re-derived on boot | append only |

The decision API stores no customer name and no email. That is a stronger
sentence than a retention policy and it is what makes the protected-data
question not arise.

## 7. Security and compliance controls

1. The model writes language and never an action. The strategy table decides.
2. `client_id` is resolved from the bearer token and refused in the body.
3. A wrong-client lookup is a 404 rather than an empty 200.
4. RLS per tenant behind the mandatory `WHERE client_id` predicate, never
   instead of it: two independent controls, and a database superuser defeats
   one of them (`docs/claims.md` row 17).
5. `FORCE ROW LEVEL SECURITY` per table, because an owner otherwise bypasses
   the policy while it reports as enabled.
6. Consent storage refuses to run when the encryption key is unset rather than
   storing plaintext.
7. The bank protocol cannot express a payment, and an import-time deny-list is
   the second control behind that.
8. No raw PII in any prompt or log: `safe_log_fields` is an allow-list.
9. Every rejection of an unauthenticated request emits one audit event through
   a single path, so a brute force leaves a trace.
10. GDPR-ready, never compliant and never certified.

## 8. Migration phases

| Phase | Shipped | State |
|---|---|---|
| 1 | outcome loop on the decision API, `decision_outcomes` | done |
| 2 | latency percentiles, inference cost, budget ceiling that refuses | done |
| 3 | SEPA table from the EPC document, Bacs table with provenance | done |
| 4 | `app/bank/` read-only seam, fake provider, consent store | done |
| 5 | RLS on the Postgres path, bootstrap probe both directions | written, NOT RUN against Postgres |
| 6 | Chargebee adapter from published documentation | done, no live payload |
| 7 | this document and its paired test | done |

Blocked on a person rather than on code: aggregator production access, a DPA
with that aggregator before any real account connects, and provider
credentials into the environment by hand. Phases 1 to 7 need none of them.

## 9. Cost and business benefits

- Build cost is already spent for phases 1 to 7. The remaining cost is
  aggregator access and a DPA, both of which are commercial rather than
  technical.
- Inference cost is measured per decision and exposed on `/metrics`, with a
  ceiling that refuses rather than warns. The total is labelled a floor while
  any call is unpriced, because the default model has no first-hand price
  (`docs/claims.md` rows 1 and 2).
- The commercial benefit of the adapter seam is that onboarding a merchant on
  an unsupported provider is two fields rather than an integration, and the
  fall-through names itself so an incomplete mapping is a scoped invoice.
- The benefit of the outcome route is that the recovery claim becomes
  measurable on the path clients integrate, which is the path the listing
  leads on.
- No recovery percentage is claimed anywhere. There is no verified figure, and
  a number nobody measured is worth less than the honest absence of one.

## 10. Risks and rollback

| Risk | Direction it fails | Rollback |
|---|---|---|
| RLS wrong on a real cluster | the bootstrap raises on any false check, so a `terraform apply` is the gate | revert the schema; the application predicate still isolates |
| adapter written from docs meets a real payload | unknown classification, never a wrong email | the fall-through holds the message and names itself |
| Chargebee secret leaks | a caller could post failures | rotate the secret; the route refuses on mismatch |
| embedding default moves | retrieval quality drifts silently | pin the model argument; currently unpinned and recorded as a problem |
| aggregator feed omits the return reason | we learn money came back and not why | the rail tables supply the reason; the feed is not the only source |
| budget ceiling hit | the call refuses and the refusal is audited | raise the ceiling deliberately, which is a decision with a record |

Every row above fails toward refusing rather than toward acting, which is the
only property that makes this safe to put in front of somebody else's money.
