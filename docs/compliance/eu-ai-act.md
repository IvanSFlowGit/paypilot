# PayPilot - EU AI Act readiness

PayPilot is **EU-AI-Act-ready**, not certified or legally assessed.
This document is an engineering readiness posture, not legal advice.
It classifies PayPilot under the EU AI Act (Regulation (EU) 2024/1689), justifies that classification against the actual code, maps the transparency and oversight obligations to the mechanisms that already exist, and states plainly what is not yet true.
The gaps list at the end is the part a reviewer, and the owner, should read first.
Nothing here should be read as a claim that a regulator, a notified body, or counsel has reviewed the system.

## 1. What the system actually is

PayPilot recovers failed subscription payments for a client's own Stripe account.
On a failed-invoice webhook it selects dunning copy, composes one email, and records whether the invoice was later recovered.
Two facts decide the whole classification, and both are evidenced in code.

- **No model decides money.** Which template is used is a deterministic lookup keyed on the Stripe failure code (`app/nodes.py`, the strategy table around line 63; `app/templates.py`, `get`).
  Whether an email is sent at all is decided by deterministic rules only: a terminal-state guard, a sequence cap, a per-customer cooldown, and a holdout control arm (`app/loop.py`, `deliver_recovery`; `app/attribution.py`, `is_holdout`).
  The actual payment happens on a Stripe-hosted page, never inside PayPilot.
- **The model only drafts text, and only when explicitly switched on.** The default runtime path performs no inference at all: it renders committed, human-reviewed copy from `data/templates/dunning.json` (`app/nodes.py`, `llm_drafting_enabled`, `use_mock`; `app/templates.py`).
  A live model drafts an email only when `PAYPILOT_LLM_DRAFT=1` is set with a key, which is off by default in the demo and in production.

So the LLM is an assistive drafting component whose output is gated, guarded, and human-approved before it can reach a person, not an automated decision-maker over anyone's finances.

## 2. Risk classification

**Classification: LIMITED-RISK (transparency obligations under Article 50).**
Justification in one line: PayPilot uses an LLM only to draft recovery email text that deterministic rules and a human/allowlist gate control before send, so it is not an Annex III high-risk system and not a prohibited practice, leaving it in the limited-risk band whose obligation is transparency.

### 2.1 Why it is not a prohibited practice (Article 5)

Article 5 bans subliminal or purposefully manipulative techniques that cause harm, exploitation of the vulnerabilities of specific groups, social scoring, untargeted facial-image scraping, emotion inference in work or education, certain biometric categorisation, and real-time remote biometric identification in public spaces.
PayPilot does none of these.
It sends a plainly worded billing email that identifies the sender, states the payment problem, and links to a Stripe-hosted page to update payment.
The copy is committed and diffable (`data/templates/dunning.json`), so what a customer reads is reviewable rather than an opaque, individually optimised persuasion attempt.
There is no profiling of vulnerability, no scoring of persons, and no biometric processing anywhere in the system.

### 2.2 Why it is not high-risk (Annex III)

The only Annex III category that comes close is point 5(b): AI systems intended to evaluate the creditworthiness of natural persons or establish their credit score (excluding fraud detection).
PayPilot does neither.
It does not score a person, assess creditworthiness, or decide who gets credit.
It acts only on invoices that have **already** failed on the client's existing customers, and the model's role is to draft the wording of a recovery email, not to evaluate the person.
Every consequential branch (send or not, which template, when to stop) is a deterministic rule keyed on Stripe's failure code and the recovery state machine (`app/loop.py`, `app/nodes.py` strategy table, `app/store.py` state machine), not a model output.
No other Annex III category (biometrics, critical infrastructure, education, employment, access to essential services, law enforcement, migration, administration of justice) is engaged by a payment-recovery email tool.
PayPilot is therefore outside Annex III and is not high-risk.

### 2.3 What that leaves

Removing prohibited and high-risk leaves the limited-risk band, whose obligation is transparency under Article 50: a person should be able to know when they are dealing with AI-generated or AI-assisted content.
The rest of this document treats Article 50 as the live obligation and everything else (oversight, logging) as supporting good practice that the architecture already provides.

## 3. Transparency obligation (Article 50)

Article 50 requires, in substance, that providers and deployers make AI interaction and AI-generated content recognisable to the people affected: systems that interact with natural persons should make the AI nature apparent, and artificially generated or manipulated content should be marked or disclosed, with a relaxation where the content has undergone human review and a natural or legal person holds editorial responsibility.

Mapping to PayPilot's reality:

- **Default path (no runtime inference).** The email is filled from committed copy that a human reviewed and approved before it was committed (`data/templates/dunning.json`, `app/templates.py`).
  On the strongest reading, this is human-authored templated content under clear editorial responsibility, much like any mail-merge, so the marking obligation is at its lightest here.
  The copy was, however, drafted with model assistance at build time, so an honest deployer may still choose to disclose AI assistance.
- **Live-draft path (`PAYPILOT_LLM_DRAFT=1`).** Here a model drafts the specific email text a customer reads, which is where the Article 50 disclosure expectation most plausibly engages.
- **The email is one-way, not an interactive agent.** PayPilot does not run a chatbot that converses with the customer, so the "interacting with an AI system" limb of Article 50 (aimed at conversational systems) is a weak fit; the relevant limb is the disclosure of AI-generated or AI-assisted content.

**The transparency control now exists.**
`app/loop.py` `ai_disclosure()` appends a plain-language AI-assistance line to the dunning body at `compose_email_body()`, the single composition point, so it passes the same output guard as the rest of the email.
It is configurable per client and per jurisdiction through `PAYPILOT_AI_DISCLOSURE` (custom text, `1` for the default line, or unset for off) and is covered by `tests/test_ai_disclosure.py`.
It is off by default so a deployer switches it on where the obligation applies; a client enabling the live-draft path should enable it.
The remaining item is therefore operational (enable it per jurisdiction), not a missing mechanism. See the gaps list.

## 4. Human oversight

Article 14-style oversight is not required of a limited-risk system, but PayPilot provides meaningful human control anyway, and it is the strongest reason the LLM is not making consequential decisions.

- **Sending is off unless a human switches it on.** `PAYPILOT_SEND_EMAIL` defaults to a dry run that records exactly what would have gone out and sends nothing (`app/mailer.py`, `sending_enabled`, `send_dunning_email`).
- **Every live recipient must be on an allowlist a human maintains.** An unset `PAYPILOT_ALLOWED_RECIPIENTS` blocks every send rather than permitting all of them, and the wildcard must be written out explicitly (`app/mailer.py`, `is_allowed_recipient`).
- **The model's output is guarded before it can leave.** Every send passes through one output guard that refuses a body carrying an unsanctioned link or a secret-shaped token, and the drafting path pins the exact minted link so only the sanctioned URL passes (`app/mailer.py`, `_post_to_resend`; `app/safety.py`, `message_violations`; `app/loop.py`, `deliver_recovery`).
  When a draft fails the guard, the deterministic template is used instead of the model's text (`app/nodes.py`, fail-closed templates).
- **The money decisions are deterministic, not model-driven.** Template choice, the sequence cap, the cooldown, the holdout arm, and the terminal-state guard are all code rules a human can read and test (`app/loop.py`, `app/nodes.py`, `app/store.py`).

Net effect: a human decides whether the system sends at all, to whom, and the deterministic rules decide the money-affecting branches; the model only proposes wording, which is checked and can be overridden by a template.

## 5. Record-keeping and logging

- **One structured audit event per LLM call.** `app/audit.py` (`audit_llm_call`) records the node, model, a boundary-normalised prompt hash, the guard verdict, and the injection and fallback flags, and never any PII or prompt text.
  This is the artifact that evidences, per drafting call, that the guard ran and whether the model's text or a fallback template shipped.
- **Security events are recorded too.** `audit_security_event` fires for a blocked non-allowlisted recipient and for an outbound message that failed the output guard, among others.
- **A durable, append-only, queryable trail exists.** `AuditEventLog` (SQLite, no update or delete method, enabled by env) persists these events so "what did the AI component do, and when" is answerable after the fact (`app/audit.py`).
- **No raw PII in any of it.** Log and audit payloads pass through an allowlist (`app/pii.py`, `safe_log_fields`); customer name and email are masked before the model sees them and are never written to the ledger (`app/pii.py`, `mask_structured_pii`; see `docs/compliance/gdpr.md`).

This is more record-keeping than a limited-risk system is obliged to keep, and it is the same trail the SOC2-readiness doc relies on.

## 6. Phased applicability

The AI Act entered into force on 1 August 2024 and applies in phases:

- **2 February 2025:** the Article 5 prohibited practices and the AI-literacy duty (Article 4) apply.
- **2 August 2025:** general-purpose AI model obligations, the governance and notified-body framework, and the penalty provisions apply.
- **2 August 2026:** general application of the Regulation, including Annex III high-risk obligations and the **Article 50 transparency obligations** that are the live item for PayPilot.
- **2 August 2027:** obligations for high-risk AI systems embedded in regulated products under Annex I apply.

PayPilot's relevant date is 2 August 2026, when the Article 50 transparency obligations apply.
These dates should be confirmed against the published Official Journal text and any Commission guidance before they are relied on for a compliance commitment; they are stated here for planning, not as legal advice.

## 7. Honest gaps list

These are the items that are **not yet true** or **not yet evidenced**.

1. **AI-assistance disclosure: mechanism shipped, off by default (operational item).**
   `app/loop.py` `ai_disclosure()` / `compose_email_body()` append a configurable AI-assistance line (`PAYPILOT_AI_DISCLOSURE`), tested in `tests/test_ai_disclosure.py`.
   It defaults to off, so the remaining action is operational: enable it (custom text or `1`) in every deployment where the Article 50 disclosure applies, and always on the live-draft path.
   This closes the earlier "no mechanism" gap; what is left is turning it on per jurisdiction.

2. **The classification is self-assessed, not legally reviewed.**
   The limited-risk classification is argued here against the code, but no lawyer, regulator, or notified body has confirmed it.
   Fix: have counsel review this classification before it is relied on in a contract or a public claim, and record the review date.

3. **No deployer-facing transparency notice yet.**
   Even where the obligation is light, a client acting as deployer may want a one-paragraph notice it can show its own customers or its DPA schedule describing that recovery emails may be AI-assisted.
   Fix: add a short transparency-notice template to `docs/legal/` alongside the existing privacy-notice and DPA templates.

4. **The default-path human-review record is procedural, not evidenced in the tree.**
   The claim that committed copy was human-reviewed rests on the pull-request history and the `generate_templates.py` draft-first flow, not on a signed review artifact.
   Fix: record, per template revision, that a named person reviewed the copy, so editorial responsibility under Article 50 is evidenced rather than asserted.

## Posture statement

The controls that matter for a limited-risk classification exist and are evidenced in code: deterministic money decisions, a human/allowlist send gate, an output guard, and a per-call audit trail.
The AI-assistance disclosure control is built and tested; it is off by default and is enabled per jurisdiction, which is the one operational item that remains.
The honest answer to a client's compliance team is: "we have classified this as limited-risk with the reasoning in hand, the oversight and logging are built, and the AI-assistance disclosure is shipped and configurable, switched on where the Article 50 date and jurisdiction require it."
No regulator or counsel has signed anything, and this document is readiness, not legal advice.
