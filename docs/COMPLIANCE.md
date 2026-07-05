# PayPilot - Compliance & Governance

EU-AI-Act-aligned, GDPR-aware, secure-by-design. Public statement: https://paypilot.fly.dev/compliance

## Risk classification (EU AI Act)
**Limited risk.** AI-assisted content and decision-support: PayPilot drafts a diagnosis, retry strategy and recovery email for a subscription the customer already agreed to. It takes no autonomous action on a person, does no biometric/employment/law-enforcement/essential-service scoring. Transparency obligations (Art. 50) apply and are met.

## Human oversight (Art. 14 spirit)
Agent DRAFTS; a human reviews and approves before anything is sent. Nothing reaches a customer autonomously. Operator can override or discard any output.

## Transparency (Art. 50)
Outputs are clearly AI-generated + human-approved. Purpose, inputs, limitations documented here + in /llms.txt.

## PETs (Privacy-Enhancing Technologies)
- Data minimisation: only failed-charge fields (amount, failure code, plan, retry history). No browsing/location/special-category data.
- No unnecessary retention: stateless per request; PayPilot stores no personal data of its own.
- Synthetic data: demo + full test suite run on synthetic fixtures, offline (no key, no network) - no real PII.
- Encryption in transit (TLS).

## Governance
- Documented purpose, risk tier, limitations.
- Grounded/deterministic: RAG over a written dunning playbook; recommendations trace to a source.
- Versioned + tested: 7-node LangGraph, full offline test suite + eval harness.

## Security (secure by design)
- Non-root container; read-only code/data at runtime.
- No secrets in code; least privilege.
- Tests run with no network + no credentials.

## GDPR
On real data the deploying merchant is the data controller (lawful basis: legitimate interest - recovering an agreed payment) and supports data-subject rights. PayPilot is a stateless processor that retains nothing.

_Design intent + technical measures; not legal advice._
