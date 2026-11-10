# Unit returns

Status as of 2026-11-10: a translation layer, an offline replay tool and a mounted
receiver (`/webhooks/unit`), tried against Unit's sandbox. Real signed notifications
from Unit were accepted and repeats are recorded once. No real returned payment has
been seen: Unit's sandbox cannot create one for payments you originate, so the
decision path was shown with a made-up return, marked as such.

## What it does

`app/unit_map.py` turns a Unit `payment.returned` event into an ACH decision:

1. Unit names the return reason (`InsufficientFunds`); the ACH table is keyed by
   NACHA code (`R01`). `UNIT_REASONS` maps all 41 names on Unit's published return
   table to their codes.
2. The code goes to the ACH table through the rails registry, which returns the
   decision row and whether a person acts.
3. An event with no reason, or a name not on Unit's table, is HELD for a person.
   Nothing is guessed and Unit's API is never called.

```
make unit-demo
python3 scripts/unit_returns_replay.py --events /abs/path/delivery.json [--signature <X-Unit-Signature>]
```

The script reads a saved delivery and prints a decision per event. Exit 0: every
event decided. Exit 1: some held or not a return. Exit 2: the file could not be
read or failed the signature, and nothing was decided. It sends and writes nothing.

## Read from Unit's own pages, 2026-11-09 (neither page carries a date)

- The ACH returns reference: 41 rows of reason name and ACH code, for originated
  and received payments. The page does not say the list is complete.
- Webhooks are signed with `X-Unit-Signature`: an HMAC-SHA1 over the entire request
  body, base64 encoded, and only when a secret token is set on the webhook.
  `verify_unit_signature` fails closed, so an unsigned delivery is refused.

## Not established

- **Whether a `payment.returned` event carries the reason.** `payment.rejected`
  shows `attributes.reason`; the returned example was not on the pages read, and a
  search summary said it shows none. If it does not, every returned event is held
  until a real payload settles it. The adapter reads `reason` then `returnReason`;
  neither is confirmed for this event type.
- **The full delivery envelope.** Only a JSON:API `data` array of events is shown.
  Any other shape is refused.
- **A returns export format, and whether Unit re-presents a returned debit.** The
  pages are silent on both.

## What the receiver does and does not do

- Checks `X-Unit-Signature` before reading the body; a wrong or missing signature is
  refused and logged.
- Decides each `payment.returned` event from the ACH table and records it, with the
  customer message for that code. A write that fails answers 503 so Unit retries.
- Records a repeated delivery of the same event once.
- Holds a return that carries no reason, or an unknown reason name, for a person.
- Sends nothing, retries nothing, and never calls Unit.

## Tried on 2026-11-10 (sandbox)

- Unit's signed notifications reached the receiver through a tunnel and were accepted.
- A made-up signed return was decided once and recorded once; a second send was
  recognised as a duplicate. Decisions are kept in a file, so they survive a restart.

## Still open

- Where the reason sits on a returned payment you originate. Unit's documentation shows
  none on the event; its support gave conflicting answers and a person has not yet
  confirmed. If it must be fetched from the returned transaction, that lookup is not built.
- A screen where a person sees a return and approves the decision.
- The receiver serves one configured client; several clients need an account-to-client mapping.
