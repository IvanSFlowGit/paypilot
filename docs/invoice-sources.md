# Invoice sources

PayPilot's decision core takes a failure code. An unpaid invoice does not arrive
as a code: it arrives as a status, a due date and sometimes silence. This layer
turns any invoice source into one record and ages it. It reads, counts and
prints. It does not decide, send or write, and it does not touch the decision
table.

Status as of 2026-10-25: built and tested offline against synthetic fixtures.
Never run against a real Mercury account. No strategy rule maps an unpaid
invoice to an action yet.

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
2. **Mercury** (`app/mercury_map.py`, `app/mercury_client.py`). Read-only.
   See the next section.
3. Others (Xero, QuickBooks, Stripe invoices): not built. Each would be a CSV
   map first.

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
