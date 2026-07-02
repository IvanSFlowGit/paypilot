# PayPilot

**An AI dunning agent that recovers failed subscription payments.**

**[Live demo -> paypilot.fly.dev](https://paypilot.fly.dev/)** - try it in the browser, no setup or API key required.

[![CI](https://github.com/IvanSFlowGit/paypilot/actions/workflows/ci.yml/badge.svg)](https://github.com/IvanSFlowGit/paypilot/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-79%20passing-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.11-blue)](requirements.txt)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

![PayPilot live recovery demo](docs/demo.png)

When a recurring charge fails, most of that revenue is recoverable - the customer
didn't *decide* to churn, their card just expired or a payment bounced. PayPilot
turns each `invoice.payment_failed` event into a grounded, on-brand recovery
action: it diagnoses *why* the payment failed, picks the right retry strategy for
that reason, and drafts a warm, one-click dunning email - all in a single API call.

It's built as a small, readable [LangGraph](https://langchain-ai.github.io/langgraph/)
agent with retrieval-augmented generation (RAG) over a dunning playbook, exposed
through a [FastAPI](https://fastapi.tiangolo.com/) endpoint. The whole thing runs
its test suite with **no API key and no network**.

---

## How it works

A failed-payment event flows through a seven-node LangGraph `StateGraph`. Each node
enriches a shared, typed `RecoveryState` and hands it to the next:

```mermaid
flowchart LR
    A[retrieve_context] --> R[assess_risk]
    R --> B[diagnose_reason]
    B --> C[choose_strategy]
    C --> S[schedule_retry]
    S --> D[draft_message]
    D --> E[finalize]
    E --> F([END])
```

| Node | What it does |
|------|--------------|
| `retrieve_context` | Loads the customer record and pulls relevant snippets from the dunning playbook via the RAG retriever. |
| `assess_risk`      | **Deterministic** (no LLM): scores churn risk (low/medium/high) from the dunning attempt number and the customer's recent failure streak. |
| `diagnose_reason`  | LLM call: a 1-2 sentence, playbook-grounded diagnosis of *why* the payment failed, reflecting the churn risk. |
| `choose_strategy`  | **Deterministic** (no LLM): maps the failure code to a fixed action + retry cadence, then tightens it when churn risk is high. Stable and unit-testable. |
| `schedule_retry`   | **Deterministic** (no LLM): turns the cadence into a concrete `next_retry_at` UTC time, ready to hand to a scheduler. |
| `draft_message`    | LLM call: a short, warm dunning email with one clear call to action. |
| `finalize`         | Assembles the `{diagnosis, risk, strategy, schedule, message, impact}` response payload. |

### Why RAG?

The recovery quality depends on dunning best-practice - retry timing, tone, when to
offer a grace period. Rather than bake that into prompts, PayPilot keeps it in an
editable knowledge source ([`data/playbook.md`](data/playbook.md)) that the
retriever (FAISS + OpenAI embeddings, `k=3`) feeds into the diagnosis and drafting
nodes. Update the playbook, and the agent's behaviour updates with it - no code change.

### Why a deterministic strategy node?

`choose_strategy` is intentionally *not* an LLM call. Retry cadence and the chosen
action come from a fixed rules table keyed on the Stripe-style failure code:

| Failure code         | Retry in | Action                | Tone              |
|----------------------|----------|-----------------------|-------------------|
| `card_expired`       | ~1 day   | Request card update   | Friendly, routine |
| `insufficient_funds` | ~3 days  | Wait and retry        | Soft, no pressure |
| `generic_decline`    | ~2 days  | Retry / verify        | Calm, helpful     |

The LLM writes the *message*; the *policy* stays predictable.

### Risk-aware escalation

`assess_risk` reads the dunning `attempt` number and the customer's recent
payment history (from `data/customers.json`) and buckets churn risk. When it's
**high** - a third attempt, or a run of recent failures - `choose_strategy`
tightens the retry cadence and marks the strategy `escalated`, the diagnosis
calls out the urgency, and `impact` discounts the recovery odds for a customer
who keeps bouncing. So the agent reasons about *history*, not just the single
event in front of it.

---

## Quickstart

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure (only needed to call the live LLM; tests don't need it)
cp .env.example .env   # then add your OPENAI_API_KEY

# 3. Run the API
uvicorn app.api:app --reload
```

### Call it

```bash
curl -s http://localhost:8000/payment-failed \
  -H 'Content-Type: application/json' \
  -d '{
        "customer_id": "cust_001",
        "amount": 1499.0,
        "currency": "usd",
        "failure_code": "card_expired",
        "attempt": 1
      }' | jq
```

```jsonc
{
  "diagnosis": "The card on file for Acme Robotics has expired, so the Scale renewal couldn't be charged; ...",
  "risk": { "attempt": 1, "prior_failures": 0, "churn_risk": "low", "escalate": false },
  "strategy": { "action": "request_card_update", "retry_in_days": 1, "offer": "...", "escalated": false },
  "schedule": { "retry_in_days": 1, "next_retry_at": "2026-07-02T09:00:00+00:00", "retry_on": "2026-07-02", "timezone": "UTC" },
  "message": "Hi Acme Robotics, we tried to renew your Scale plan but the card we have on file has expired ...",
  "impact": { "amount_at_risk": 1499.0, "currency": "USD", "recovery_likelihood": 0.7, "expected_recovered": 1049.3, "annual_value_at_risk": 17988.0, "churn_risk": "low" }
}
```

`GET /health` returns `{"status": "ok"}` for liveness checks. The full response
schema (typed with pydantic) is browsable at [`/docs`](https://paypilot.fly.dev/docs).

The endpoint is rate limited per client IP, and the response payload is a typed
`RecoveryResponse` (`diagnosis`, `risk`, `strategy`, `schedule`, `message`,
`impact`), so the contract shows up precisely in the OpenAPI docs.

`POST /payment-failed/batch` runs a whole billing run (up to 50 events) in one
call and adds a portfolio `aggregate` - total at risk, total expected recovered,
and how many accounts are high churn risk. `GET /portfolio-impact` rolls that up
across the demo customers and powers the recoverable-revenue headline on the
landing page.

### Speaks Stripe

`POST /webhooks/stripe` accepts a real Stripe `invoice.payment_failed` event. It
verifies the `Stripe-Signature` header (HMAC-SHA256) when `STRIPE_WEBHOOK_SECRET`
is set, acknowledges other event types with a `200` so Stripe won't retry, maps
Stripe decline codes (`expired_card`, `insufficient_funds`, ...) to PayPilot's
failure codes, and runs the recovery graph. Point a webhook (or
`stripe trigger invoice.payment_failed`) at it; add
`metadata.paypilot_customer_id` to resolve a demo customer.

The webhook is **idempotent** on the Stripe event id, so a retried delivery
replays the stored result instead of re-running the graph. `POST /payment-failed`
and `/batch` accept an optional `Idempotency-Key` header for the same guarantee.
Every response carries `X-Process-Time` and `X-Request-ID` headers, emits a
structured JSON access log, and `429`s include `Retry-After`. `GET /metrics`
returns a JSON snapshot (request counts by status, average latency, recoveries
run, total expected recovered). Batches roll up per currency, so a mixed
USD/EUR/GBP billing run stays correct (`aggregate.by_currency`).

For discovery, the app also serves `/robots.txt`, `/sitemap.xml`, and an
[`/llms.txt`](https://paypilot.fly.dev/llms.txt) summary for AI answer engines,
and the landing page ships `SoftwareApplication` + `FAQPage` JSON-LD.

---

## Testing

The two external seams - the chat model (`app.nodes.get_llm`) and the retriever
(`app.nodes.get_retriever`) - are swapped for in-memory fakes in the tests, so the
suite runs offline with no API key:

```bash
pytest -q
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same suite on
every push and pull request.

---

## Project layout

```
app/
  ingest.py   # build/cache the FAISS retriever over the playbook
  nodes.py    # the seven node functions (+ get_llm seam, strategy + risk rules)
  graph.py    # RecoveryState + StateGraph wiring + run_recovery()
  api.py      # FastAPI: POST /payment-failed, GET /health
data/
  playbook.md     # dunning best-practice - the RAG knowledge source
  customers.json  # sample customer + payment-history fixtures
tests/
  test_graph.py              # end-to-end + strategy table + API, all mocked
  test_mock_and_security.py  # offline mock mode + validation, rate limit, headers
```

## Run with Docker

```bash
docker build -t paypilot .
docker run -p 8000:8000 --env-file .env paypilot
```

---

## Design notes

- **One LLM seam.** Every chat call goes through `get_llm()`, so the model is
  configurable (`OPENAI_MODEL`, default `gpt-4o-mini`) and trivially mockable.
- **Graph compiled once.** `app.graph.graph` is built at import and reused; the
  nodes resolve `get_llm` / `get_retriever` by name at call time, which is what
  makes monkeypatching the compiled graph work in tests.
- **Fails safe.** Unknown customers and unexpected failure codes degrade to sane
  defaults instead of raising, so a malformed webhook never takes the endpoint down.

PayPilot is a focused portfolio project: a realistic, testable agentic system -
RAG + LangGraph + FastAPI - applied to a problem (involuntary churn / dunning) where
recovered revenue is directly measurable.
