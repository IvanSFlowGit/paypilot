# PayPilot - GDPR compliance

This document describes what PayPilot processes, where each field lives, why it is kept, and how a data subject's rights are served.
It is written to be honest about the running system, not aspirational.
Where a control is not yet fully evidenced, it is called out plainly in the open-items section rather than glossed over.

PayPilot is deployed single-tenant, one instance per client.
Streamflow Solutions operates the software as a **processor** on behalf of the client, who is the **controller** of the customer data.
The lawful basis for the processing is the client's **legitimate interest** in recovering its own failed payments and the **performance of the client's contract** with its customer.
PayPilot does not seek fresh consent from the client's customers, and does not run a consent flow, because it acts on the client's existing customer relationship (see `docs/legal/dpa-template.md`).

## 1. What PayPilot processes

PayPilot recovers failed subscription payments.
To do that it receives Stripe webhook events for a client's own Stripe account, drafts a dunning email, and records whether the invoice was later recovered.

The data it touches falls into three groups.

- **Billing metadata** about a failed invoice: invoice id, amount, currency, failure code, attempt count, and the recovery state over time.
- **Pseudonymous identifiers** that tie an invoice to a person via the client's Stripe account: a local customer id, the Stripe customer id, and the subscription id.
- **Direct identifiers** used only to draft and send one email: the customer's name and email address, and an operator-supplied `plan` label.

PayPilot never collects or stores card data.
The only payment surface is a Stripe-hosted page (a billing portal session or the invoice's `hosted_invoice_url`), and any card-shaped digit run that appears in free text is masked before it can reach the model (`app/pii.py`, `scrub_freeform`).

## 2. Data map

The critical honesty point is this: **the customer's name and email are not persisted in the ledger at all.**
They arrive on the webhook or are read from Stripe at request time, are masked to placeholders before the model sees them (`app/pii.py`), are used to draft and send one email, and are then discarded when the request ends.
Nothing writes them to disk.
The recipient address is not even stored on the message record; only the provider's own message id is kept.

### 2.1 Persisted data (SQLite ledger, `app/store.py`)

Table: `failures`

| Field | Category | Why it is retained |
| --- | --- | --- |
| `invoice_id` | Billing metadata (primary key) | Identifies the failed invoice across events; the join key for the whole loop. |
| `customer_id` | Pseudonymous identifier | Local/demo customer id resolved from event metadata; needed to group a customer's invoices. |
| `stripe_customer_id` | Pseudonymous identifier | The real Stripe customer id; closing events name it, so matching a recovery needs it. |
| `subscription_id` | Pseudonymous identifier | Churn events name a subscription, not an invoice; needed to resolve which invoices to close. |
| `amount_minor` | Billing metadata | Value at risk, in integer minor units; drives the recovered-amount figure. |
| `currency` | Billing metadata | Currency code; a mixed-currency run must never be summed into one number. |
| `attempt_count` | Billing metadata | Stripe fires one event per retry; kept so a repeat is an attempt, not a second failure. |
| `failure_code` | Billing metadata | The Stripe decline reason; drives which deterministic template is chosen. |
| `state` | Billing metadata | Current point in the recovery state machine. |
| `holdout` | Billing metadata | Whether the invoice is in the no-contact control group (attribution baseline). |
| `recovered_amount_minor` | Billing metadata | The amount actually recovered, when recovered. |
| `failed_at`, `updated_at`, `recovered_at` | Billing metadata | Timestamps; time-to-recovery is a reported number and must not be settable by a payload. |

Table: `messages`

| Field | Category | Why it is retained |
| --- | --- | --- |
| `id` | Internal | Row id. |
| `invoice_id` | Billing metadata | Which invoice the delivery attempt was about. |
| `channel` | Operational | Delivery channel (email). |
| `status` | Operational | `sent`, `dry_run`, `suppressed`, `failed`, or `bounced`; drives the sequence cap and cooldown. |
| `provider_message_id` | Operational | Resend's own id; needed to reconcile a later bounce webhook. No recipient address is stored. |
| `attempt` | Operational | Which touch in the sequence this was. |
| `error` | Operational | Why a send was suppressed or failed; never carries the recipient or body. |
| `created_at`, `sent_at` | Operational | When the attempt happened. |

Table: `events`

| Field | Category | Why it is retained |
| --- | --- | --- |
| `event_id` | Operational | Processed Stripe event id; the idempotency key that stops a redelivery double-counting revenue. |
| `event_type` | Operational | The Stripe event type. |
| `invoice_id` | Billing metadata | The invoice the event concerned. |
| `received_at` | Operational | When the event was processed. |

Table: `transitions`

| Field | Category | Why it is retained |
| --- | --- | --- |
| `id` | Internal | Row id. |
| `invoice_id` | Billing metadata | Which invoice moved state. |
| `from_state`, `to_state` | Billing metadata | The state move; this table is append-only so a disputed number can be traced to the events that produced it. |
| `reason` | Operational | Why the move happened. |
| `at` | Operational | When it happened. |

### 2.2 Transient data (runtime only, never persisted)

| Field | Category | Lifetime |
| --- | --- | --- |
| `name` | Direct identifier | Held in graph state for one request; masked to `{{NAME_1}}` before the model; discarded at end of request. |
| `email` | Direct identifier | Held in graph state for one request; masked to `{{EMAIL_1}}` before the model; used once at send time; not written to the ledger. |
| `plan` | Operator free text | Passed through the same `_safe_field` gate as the templates; reaches the model as text, never persisted. |

### 2.3 Logs and derived stores

- **Audit and application logs** (`app/audit.py`) carry a boundary-normalized prompt hash, the guard verdict, node, model, and duration, and **never the customer name, email, or prompt text.** These are emitted to stdout and captured by the platform log stream.
- **The RAG index** (`app/ingest.py`) is built over the static recovery playbook, not over customer data, so no customer PII lives in the FAISS index.

## 3. Data minimization

Two design choices carry the minimization story.

- The ledger schema stores only what the recovery loop and the honest dashboard need.
- Direct identifiers (name, email) are never persisted; they exist only for the duration of one draft-and-send.

Before prompt assembly, both the event and the customer record are reduced to an explicit **allowlist** of keys (`app/nodes.py`), so a field nobody vetted (a phone number, a billing address) is dropped rather than carried.
This is the canon rule "guard what you extract" applied to prompt inputs.

## 4. Data-subject rights procedures

The client (controller) receives access, portability, and erasure requests from its customers and forwards them to the PayPilot operator, or runs the scripts itself.
PayPilot keys every record on `customer_id`, `stripe_customer_id`, and `invoice_id`, so a request resolves to a definite set of rows.

Note on status: the three scripts below are implemented and covered by tests.
The suite proves that erasure removes every ledger row (a subsequent read finds nothing) and that the purge expires terminal records after the configured window.

### 4.1 Right of access and portability

Command: `scripts/gdpr_export.py`

- Input: a `customer_id`, `stripe_customer_id`, or email.
- Output: a machine-readable export (JSON) of every ledger row that concerns that data subject, drawn from `failures`, `messages`, `events`, and `transitions`.
- Because name and email are not persisted, the export reflects what PayPilot actually holds: billing metadata and pseudonymous identifiers, not a stored copy of the person's name or address.

### 4.2 Right to erasure

Command: `scripts/gdpr_erase.py`

- Input: a `customer_id` or `stripe_customer_id`.
- Effect: deletes the matching rows in `failures`, `messages`, `events`, and `transitions` so the identifiers and billing metadata are removed end to end.
- Logs need no separate scrub for name or email, because the logs never contained them; they hold only hashes and identifiers.
- The erasure request itself is recorded as an audit event (a hashed id, before deletion) so the controller can evidence that the request was honored.

### 4.3 Retention

Command: `scripts/retention_purge.py`, run on a schedule.

- Policy: recovered, churned, and exhausted (terminal) records auto-expire after a configurable window.
- Default window: **12 months** from the terminal timestamp, configurable per deployment.
- Rationale: the recovery numbers a client needs for a rolling year stay available; anything older is purged rather than kept indefinitely.
- The window must be a config value, not a hardcode, so a client can set its own retention period.

## 5. EU data residency

- The deployment region is set in `fly.toml` (`primary_region = "lhr"`, London), an EU/UK region.
- Region should be treated as a config value per deployment, so a client that requires a specific jurisdiction can pin it.
- The email provider (Resend) region and the data-processing locations should be confirmed against the client's residency requirement at onboarding and recorded in the DPA schedule.

## 6. Lawful basis and documents

- Lawful basis: legitimate interest (the client recovering its own revenue) and performance of the client's contract with its customer.
- No fresh consent is collected; PayPilot acts on the client's existing customer relationship.
- Client-facing documents live in `docs/legal/`: a privacy notice template and a Data Processing Agreement template that positions Streamflow as processor and the client as controller.

## 7. Open items (see also the SOC2-readiness gaps list)

- `hash_pii` in `app/pii.py` is salted via `PAYPILOT_PII_SALT`; when the var is unset it falls back to a known default salt and warns, so production must set a secret salt for the control to fully hold.
- At-rest encryption of the ledger volume is a platform control that this deployment has not yet independently evidenced (see `soc2-readiness.md`).
- Alerting, branch-protection evidence, and an incident-response/key-rotation runbook remain open (see the SOC2-readiness gaps list).
</content>
</invoke>
