# The PayPilot decision contract

PayPilot is not a Stripe product with a Recharge add-on. It is a decision service with
two required fields, and the two provider modules in `app/` are convenience adapters onto
it. The platform never enters the decision, so the platform is never the constraint.

Everything below was measured against the code on 2026-10-03, not read off a description.

## The request

`POST /decide`, `Content-Type: application/json`, bearer auth.

| Field | Required | Type | Constraint | Default |
|---|---|---|---|---|
| `invoice_id` | yes | string | 1 to 64 characters of `[A-Za-z0-9_-]` | none |
| `failure_code` | yes | string | 1 to 64 characters of `[a-z0-9_]` | none |
| `attempt` | no | integer | 1 or more | `1` |
| `prior_failures` | no | integer | 0 or more | `0` |

Any other field is refused with `422 invalid_input`. The error names the field and the
constraint and never echoes the rejected value, so a caller who pastes something sensitive
does not get it back in a response or an access log.

**Two fields are enough.** A caller who tracks no retry history at all sends an invoice id
and a failure code and is integrated. `attempt` and `prior_failures` sharpen the churn
score when the caller has them and are not required to get a decision. This is asserted in
`tests/test_decision_is_provider_neutral.py` rather than stated here, because it is the
sentence the product is sold on.

## The response

`200` with the decision, which is recorded before it is returned:

```json
{
  "invoice_id": "INV-1",
  "rule_fired": "card_expired",
  "input": {"invoice_id": "INV-1", "failure_code": "card_expired", "attempt": 1, "prior_failures": 0},
  "churn_risk": "low",
  "strategy": {"action": "...", "retry_in_days": 3, "offer": "...", "escalated": false},
  "decided_at": "2026-10-03T12:00:00.000+00:00",
  "audit_id": "..."
}
```

`GET /decisions/{invoice_id}` returns the recorded decisions for one invoice, newest first,
or `404` when there are none.

## An unknown failure code is a decision, not an error

A code the rules table has never seen fires `rule_fired: "default"` and returns a strategy.
It does not raise.

That is the integration story for a new rail. A table that refused unknown codes would make
every new platform a blocker; falling through to `default` makes an incomplete mapping a
scoped job with a before and an after a client can read, where `default` becomes their own
code. Send the codes your platform emits and the mapping is the work.

## Failure modes, all fail closed

| Status | Meaning |
|---|---|
| `422 invalid_input` | a field is missing, malformed, or not in the four |
| `400 invalid_body` | the body is not valid JSON |
| `413 body_too_large` | over the configured byte ceiling |
| `401 unauthorised` | bearer token missing or wrong |
| `503 not_configured` | no token is configured on the server |
| `503 audit_unavailable` | the decision could not be recorded, so it is not returned |

The last one is the design point. A decision with no audit trail is refused rather than
returned unrecorded, because an unrecorded money decision is the one thing this path must
never produce. The audit row is what proves which rule fired.

## What the decision does NOT touch

`app/decision.py` imports `hmac`, `re` and `datetime` and nothing else. `decide()` is pure:
no network, no database, no provider SDK. No rule's response names a payment provider,
which is swept across every rule in both the plain and the escalated path by
`tests/test_decision_is_provider_neutral.py`.

Until 2026-10-03 one of the seven rules did name one. The offer text for
`direct_debit_not_retried` read "and Stripe is not retrying it", so a merchant on any other
rail got advice naming a processor they do not use. It is now "the billing system", matching
the neighbouring rule, and the test above exists so it cannot come back.

## The limit, stated rather than discovered

**Authentication is a single shared token** read from the `DECISION_API_TOKEN` environment
variable and compared in constant time. There is no tenant concept anywhere in this path:
no client id in the request, no per-token scoping, and nothing in the audit row that
identifies which caller made the decision.

So today this serves one integrator. Two agencies on the same deployment would share a
token, and their traffic would be indistinguishable in the audit trail, which is the thing
the product is sold on. Fixing it is a decision about shape rather than a bug to patch, and
it is the only outstanding blocker on this path. It is not about the rails.

## Seeing it live

The route is in the OpenAPI document, so `GET /docs` and `GET /openapi.json` on a running
instance list `POST /decide` and `GET /decisions/{invoice_id}` with their schemas. Until
2026-10-03 both carried `include_in_schema=False` and were invisible there, which meant
anyone evaluating PayPilot could read every page of the documentation and never find the
one route that makes it work with any platform.

## Reproducing these claims

```bash
cd paypilot
.venv/bin/python -m pytest tests/test_decision_is_provider_neutral.py -q
```

The provider sweep walks every key in `STRATEGY_RULES` plus one unknown code, and a second
test fails if a rule is added to the table without the sweep being regenerated, so the
sweep cannot silently cover a stale subset.
