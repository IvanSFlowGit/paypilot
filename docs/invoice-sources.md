# Invoice sources

PayPilot's decision core takes a failure code. An unpaid invoice does not arrive
as a code: it arrives as a status, a due date and sometimes silence. This layer
turns any invoice source into one record and ages it. It reads, counts and
prints. It does not decide, send or write, and it does not touch the decision
table.

Status as of 2026-10-25: built and tested offline against synthetic fixtures.
Never run against a real Mercury account. The five-label reader that sits on top of this record is
[`docs/invoice-adoption.md`](invoice-adoption.md); the decision table still maps no unpaid
invoice to an action.

## The record

`app/invoice_source.py`, `InvoiceRecord`:

| Field | Rule |
|---|---|
| `source`, `source_id`, `invoice_no` | Short id strings, validated. |
| `customer_ref` | 16 hex characters of a salted hash. A name or address never enters a record. The salt is passed in and has no default. |
| `amount_minor`, `currency` | Integer minor units parsed from a decimal string. A float, a bare integer, `12,50`, a thousands separator, or too many decimals is refused. |
| `status` | `open`, `processing`, `paid`, `cancelled`, `unknown`. A source status not taught to the mapper is carried as `unknown` (CSV) or returns `Unmapped` (Mercury), never guessed. |
| `issued`, `due`, `paid_at` | ISO dates or None. |

Ageing buckets: `no_due_date`, `closed`, `processing`, `not_due`,
`overdue_1_7`, `overdue_8_30`, `overdue_31_60`, `overdue_61_plus`. A missing due
date is its own bucket and is never read as on time. The clock is always passed
in.

## Sources, smallest ask first

1. **CSV export** (`app/invoice_csv.py`). Every invoicing tool can export one,
   and it needs no credential. The caller supplies the column map; there are no
   vendor presets because nobody here has measured another vendor's export.
   Each row is accepted or rejected on its own with a line number and a field
   name, never the cell value, and `total == accepted + rejected` is asserted on
   every return. A mapped header that is missing refuses the whole file.
   Dates must be ISO and amounts plain digits unless the profile opts in: `date_format`
   (one of `%Y-%m-%d`, `%d/%m/%Y`, `%m/%d/%Y`, `%d.%m.%Y`, `%d-%m-%Y`) and `amount_style = "uk"`
   (optional leading currency symbol, comma only as a thousands mark in groups of three).
   The format is stated, never detected. Negatives, credits and two-digit years are still
   refused. Tested on invented exports only.
2. **Mercury** (`app/mercury_map.py`, `app/mercury_client.py`). Read-only.
   See the next section.
3. **Mailbox reader** (built 2026-11-10 for IMAP: Gmail app password, Proton Bridge, any IMAP; tested against a recording fake server and synthetic mail only, NEVER RUN AGAINST A LIVE MAILBOX; Gmail's API and any messenger are NOT built). Most B2B invoices and
   their chasing live in email threads, not in a ledger, so a CSV misses the
   "promised by Friday" and "we are disputing this" that decide what happens next.
   Shape, smallest first:
   - Read-only. One scope, read, never send, never modify, never label.
   - A data processing agreement is signed before any credential, as above.
   - Match first, store second: a message is kept only if it matches a known invoice
     (an invoice number, or a hashed customer plus an amount). Everything else is
     dropped at ingest and never stored or logged.
   - Facts before labels. Deterministic extraction (amounts, dates, a reply from the
     debtor, a bounce, an out-of-office) sets the flags the rules table already reads:
     `chases sent`, `replies received`, `promised-by`. A model is allowed only to propose
     "disputed" and "stuck in approval", and both map to *hand it to a person*, never to
     escalate or suppress a chase on their own.
   - The model sees message text with names, addresses and account numbers masked,
     and its inference does not run in a region the client has not accepted.
   - Absence is a signal only with a feed-alive check: a mailbox that stops
     returning mail reads "unreachable", never "nobody replied".
   Built: `app/mailbox_facts.py` (match, classify, promised-by, masking), `app/mailbox_imap.py`
   (read-only IMAP: only SELECT read-only, UID SEARCH, UID FETCH with BODY.PEEK and LOGOUT are ever issued),
   `scripts/mailbox_facts_export.py` (writes the facts file `invoice_rules_report.py --facts` reads; an
   incomplete read leaves replies empty, never zero). Also built: `app/mailbox_gmail.py` (Gmail API, read-only
   scope, which Google classes as restricted), `app/mailbox_graph.py` (Outlook, Mail.Read with a strict scope
   allowlist) and `app/dispute_proposer.py` (a model may only PROPOSE disputed or stuck-in-approval, into a
   separate review file, from masked text, strict JSON, fail closed; a person sets `disputed`). NOT built:
   any messenger. `mailbox_facts_export.py --provider imap|gmail|graph` wires all three readers (credentials
   from environment variables, read only inside the script). Unit sandbox proof, 2026-11-10, final run: a webhook (id 45472) pointing at a quick tunnel to a local PayPilot
   received FOUR real signed deliveries from Unit's address, all 200, after Unit's Send-application test created
   a customer; and a SYNTHETIC signed `payment.returned` was decided once and recorded once (one audit row,
   `acted: false`), its replay acknowledged with `duplicates: 1` and no second row. The synthetic event proves
   the path, not Unit's real return payload. The audit store was in memory for this run, so the row is gone
   when the process stops. Unit, measured 2026-11-10 in the sandbox: signed webhook deliveries are accepted (200) and retried
   up to 7 times within seconds, now deduplicated on the payment id (the event id when none). Unit's support
   chat says the originated `payment.returned` event carries no return reason and that the sandbox cannot
   simulate a return, so every real return would currently be HELD FOR A PERSON until the reason is read from
   the returned transaction, which is not built and not confirmed. Unverified against any live account: Gmail folder names, Bridge's port and security,
   Gmail and Graph error bodies and throttling, and whether real mail headers match what the classifier
   expects; `default_complete` (the real model call) has never run without a key.
4. **Bank feed, as an in-flight signal only** (`app/in_flight.py`, `BerlinGroupAis.parse_transactions`;
   built 2026-11-23, offline, synthetic bodies only, NEVER RUN against a live bank). It answers one
   question, whether money for this invoice is already moving, and returns HOLD, RELEASE or UNKNOWN.
   Rules: only a credit whose description carries the invoice number as a whole token holds; an
   amount-only match is counted as `possible` and never holds; debits are ignored; a currency
   mismatch is UNKNOWN; a dead feed is UNKNOWN `feed_unreachable`, never "nothing pending". EVERY
   HOLD EXPIRES: 3 business days from first seen for pending, 5 from booking for booked. Those two
   numbers are starting values, not measurements, and there is no bank-holiday calendar. A credit
   returned later is released only by expiry. `InvoiceFacts.money_in_flight` carries the hold into
   the rules table: it yields UNKNOWN `money_in_flight` (no chase), outranks a promise, and does not
   outrank a dispute. The adapter's live `transactions` call still refuses (no consent, no
   credential), and nothing here marks an invoice paid. The UK open banking route is not chosen.
5. **Xero, QuickBooks Online, Sage** (`app/accounting_scopes.py`, `app/accounting_map.py`; built
   2026-11-23, offline, synthetic bodies only, NEVER RUN against a real account; vendor facts are
   claims rows 82 to 85). Read-only is enforced differently, and the code says so: Xero and Sage have
   `.read` / `readonly` scopes, and the guard refuses every write-capable scope, including Xero's
   deprecated broad ones. QuickBooks has NO read-only scope, so a QuickBooks token can write and the
   only guard is `ensure_read_request` (GET on the `query` endpoint with a `select` statement, nothing
   else). Xero's Remote MCP carries write tools behind the same URL; `check_xero_mcp_tool` allows five
   named reading tools and refuses the rest. The mappers take parsed JSON and return an
   `InvoiceRecord` or `Unmapped`: a Xero DRAFT or SUBMITTED invoice and any ACCPAY bill are refused, not
   chased; QuickBooks status is derived from `Balance` and `PrivateNote`; Sage needs the caller to name the
   invoice-number, due-date and total keys and its status table, because the page read did not state
   them. Parse bodies with `loads_exact` so no amount passes through a float. No live client exists:
   a DPA and a per-client credential come first. The mailbox reader and the in-flight rule above
   already consume the same `InvoiceRecord`. What a customer writes back is read by
   `app/inbound_reader.py`, see [`customer-replies.md`](customer-replies.md).

Run it: `scripts/invoice_age_report.py` (see its docstring). Exit 0 all rows
accepted, 1 some rejected or unmapped, 2 nothing measured and no report printed.

## Mercury

What is documented and used (rows 41 to 50 of [`claims.md`](claims.md), all
RELAYED or NEVER CHECKED, because the raw page re-read was blocked):

- Create a **Read Only** token in the dashboard. The client has no write path:
  GET is the only method it can issue, asserted in source and by test. A Read
  and Write token needs an IP allowlist and can move money; do not use one.
- Webhook scheme: `Mercury-Signature: t=<timestamp>,v1=<signature>`, HMAC-SHA256
  over `<timestamp>.<raw body>`, five minute window, at-least-once delivery.
  There is no invoice event. A `transaction.created` event is a hint to look for
  a matching invoice and never marks one paid.

What the pages do not state, so the code refuses instead of guessing:

- The invoice amount, due-date and currency key names. The mapper takes them
  from the caller and returns `Unmapped` without them.
- The signature encoding (hex is assumed), the event-type field, the
  `Authorization` header scheme, the URL base for receivables, the list
  envelope, pagination, rate limits, sandbox details and plan tier.

The first real call is therefore a measurement, not a deployment: one GET with a
Read Only token, printing only the status and the top-level key names. Row 50
says exactly what to record.

## Consent, before any file or token

An invoice list carries a business's customers' names and amounts. Those people
have no relationship with us. A data processing agreement is signed before any
token or file is accepted, customer identifiers are hashed at ingest, and
nothing from a client's list goes into a prompt or a log.

## Not built, on purpose

- Any strategy rule for an unpaid invoice. That is a product decision.
- Sending, scheduling or chasing anything.
- Any write scope on any source.
- Paging through Mercury lists: the pagination parameters are not stated.
