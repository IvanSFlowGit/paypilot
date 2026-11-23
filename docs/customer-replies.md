# Customer replies

What PayPilot does with a customer's answer to a reminder, and what it refuses to do.
Code: `app/inbound_reader.py`. Public demo: `POST /inbound/read`.

## The flow

1. The reply arrives. Quoted text from earlier emails is removed first.
2. `read` classifies it with fixed phrase rules and extracts the date and amount the customer
   stated. No model runs.
3. If a bank feed is connected, `in_flight.assess` checks for a credit carrying the invoice
   number, and `check_claim` turns that into one of eight results.
4. `draft` composes one reply from fixed sentences, the caller's own fields (first name,
   business name, invoice number, amount, due date) and the extracted facts.
5. A person approves the draft through the existing approval flow. Nothing is sent here.

## Intents

In precedence order: `opt_out`, `hardship`, `dispute`, `repairs`, `cancel`, `already_paid`,
`benefits`, `update_details`, `extension`, `unclear`. More than one distinct intent is `mixed`, which is for a person.

| Intent | Typical wording | Draft | Person must act |
|---|---|---|---|
| opt_out | "stop emailing me" | confirms reminders stopped | yes |
| hardship | "I lost my job", "I can't afford it", a bereavement | reminders paused, a person will make contact; no arrears or legal wording | yes, and the account is flagged vulnerable |
| dispute | "this is incorrect", "charged twice" | holding reply, admits nothing | yes |
| repairs | "the boiler is broken", "there is damp" | reminders paused while a person looks into it; takes no position on withholding rent | yes |
| cancel | "cancel my subscription" | holding reply | yes |
| already_paid | "I have already paid" | depends on the payment check | depends |
| benefits | "waiting for my Universal Credit" | notes the expected payment, reminders paused | yes |
| update_details | "my card expired" | asks them to reply; no link, no card numbers | yes |
| extension | "can I have more time" | notes the request and any date given | yes |
| unclear | anything else | holding reply | yes |

## The paid claim

"I have already paid, but ..." is the paid path with a qualifier. `still_reminded` adds an
apology. `payment_failed` and `wrong_amount` go to a person. A paid claim that also mentions a
double charge is a dispute.

| Payment check | Result |
|---|---|
| `found_full_booked` | says a matching payment is visible; pauses reminders |
| `found_full_pending` | says it is pending and has not reached us; pauses reminders |
| `found_part` | notes the shortfall; a person checks |
| `not_found`, `expired` | none visible yet; asks for the bank reference |
| `unchecked`, `feed_unreachable`, `currency_mismatch` | a person confirms |

A stated amount that differs from the invoice also goes to a person.

## What it refuses

- To say a payment was received without finding a credit carrying the invoice number.
- To mark an invoice paid. That stays with the ledger.
- To copy the customer's words into the reply, so nothing in a message can steer it.
- To use a customer-supplied display name that is not plain letters.
- To send anything. Every result is a draft; `person_must_act` says whether a person has to
  do more than approve it.
- To read anything but English. Other languages go to a person.

## Not proven

Run only on invented messages. The phrase lists are a starting set. The in-flight periods
(3 business days pending, 5 booked) are starting values. The public demo simulates the bank
feed. No real customer reply has been through it.

## Tenants and property management

The same reader and rules work on a rent or service charge ledger. Set the item label to
`rent` or `service charge` and the draft says "rent R-204 (GBP 950.00, due 1 November
2026)". Hardship outranks a payment claim and a dispute, because a tenant who says they
cannot pay should be spoken to by a person, not chased. A vulnerability flag recorded by
a person in the facts file (`vulnerable`) turns any invoice into UNKNOWN with the rule
`vulnerability_hold`, so no automated chase follows; a dispute still shows as a dispute.
`examples/property-demo` is a synthetic ledger that shows this, and `make property-demo`
runs it.

Ledger exports from UK property systems often carry day-first dates and amounts such as
`£1,250.00`. The importer refuses both by default, because a guess about a date or a
separator is how money gets misread. A profile can state the format instead:
`date_format = "%d/%m/%Y"` (one of `%Y-%m-%d`, `%d/%m/%Y`, `%m/%d/%Y`, `%d.%m.%Y`,
`%d-%m-%Y`) and `amount_style = "uk"` (an optional leading £, $ or €, and a comma only as a
thousands mark in groups of three). Nothing is detected: 02/01/2026 is 2 January under one
setting and 1 February under the other, so the profile says which. Negatives, bracketed
credits, trailing symbols, two-digit years and malformed groupings are still refused row by
row. Tested on synthetic exports written for this purpose, not on a real property system's
export.

Not proven: no property management system has been connected. Dwellant was seen only in
search snippets, which describe it as a UK service charge and block management platform;
its pages were not read, and whether it has an API or an export is unknown. The route
assumed here is a CSV export of the ledger. Rent
arrears carry rules that depend on the jurisdiction and the tenancy type, and none of
that is encoded here. The reader detects hardship, repairs and benefits wording from
short phrase lists written without a real tenant mailbox.
