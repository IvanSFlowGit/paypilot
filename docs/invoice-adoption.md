# Label your unpaid invoices

Status as of 2026-11-09: built and tested against synthetic data only. It has
never been run on a real business's export. It labels, prints and stops. It does
not send, write or touch the decision table.

## Try it in one minute

```
make install
make invoice-demo
```

No key, no network, nothing sent. The bundled files are in `examples/invoice-demo/`
and every name in them is invented.

## Use it on your own export

1. Export your open invoices to a CSV from your invoicing tool. You need an
   invoice number, a customer, an amount and a due date. Status and currency are
   optional if you pass `default_currency`.
2. Copy `examples/invoice-demo/profile.toml` and change the `[map]` values to
   your export's column headings. Map your tool's status words in `[status]`;
   a word you do not map is carried as `unknown`, never guessed.
3. Optional: add a facts file with `invoice_no` and any of `chases_sent`,
   `replies_received`, `disputed`, `promised_by`. An empty `replies_received`
   means "not tracked", and `no_reply` only fires on a recorded zero.
4. Set a salt of your own and run it:

```
export PAYPILOT_CUSTOMER_SALT="$(openssl rand -hex 16)"
python3 scripts/invoice_rules_report.py --profile /abs/path/to/your/profile.toml
```

Customer names are hashed with your salt and never printed. Invoice numbers are
printed, so keep the output as private as the export.

## What the labels mean

| Label | Rule | Suggested human action |
|---|---|---|
| `disputed` | your dispute flag is set | stop chasing, a person handles it today |
| `promised_not_paid` | promised-by date is in the past | a person calls today |
| `no_reply` | 2 or more chases and a recorded 0 replies | change channel, phone call |
| `overdue_60` | 60 or more days past due | write to finance, copy the original contact |
| `overdue_30` | 30 to 59 days past due | first chase, ordinary tone |
| `UNKNOWN` | none of the above, with the reason printed | nothing is decided for you |

The 30, 60 and 2 are starting numbers, set as named constants in
`app/invoice_rules.py`. Re-read them against your own export.

## Exit codes

0 every row accepted. 1 the report printed but some rows or facts were rejected.
2 the file could not be read and nothing was measured; no report exists.

## Running it on a schedule

It reads a file you drop, so scheduling is your machine's job. A weekly cron line:

```
0 8 * * 1  PAYPILOT_CUSTOMER_SALT=... /path/to/.venv/bin/python /path/to/scripts/invoice_rules_report.py --profile /path/to/profile.toml >> /path/to/labels.log 2>&1
```

Treat a non-zero exit as a stale or broken export, not as a quiet week.

This repository runs the same CLI on the bundled demo every Tuesday through
`.github/workflows/invoice-rules.yml`, diffs it against `examples/invoice-demo/expected.txt`
and goes red on any drift or a non-zero exit. It uses no secret and no real data.
To do the same for your export, copy that workflow into your own repository and
point the profile at your file there.

## What it does not do

It does not read a mailbox, so replies and chases come from your facts file. It
does not send anything. A mailbox reader needs a signed data processing agreement
before any access, and is not built.

## Licence

PolyForm Noncommercial 1.0.0. Using it inside a business that charges for its
work is commercial use and needs the separate licence; see
`docs/commercial-licence-draft.md`, which is a draft.
