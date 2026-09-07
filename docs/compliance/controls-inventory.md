# PayPilot - verifiable controls inventory

Every control below is real code in this repo, with the file and line to open.
Each entry ends with "Show it:" - the exact file to open or command to run in an interview.
The honest gaps are listed at the end; do not claim more than this document states.

Test suite: a test and evaluation suite in CI, offline, no network, no API key (`.venv/bin/python -m pytest -q`).

## 1. PII masking (scope: name and email only; identifiers hashed; nothing sensitive persisted)

- Raw customer values live only in graph state; they are masked to placeholders at prompt assembly, never sent raw to the model.
  `app/pii.py:109` `mask_structured_pii()` masks `name -> {{NAME_1}}`, `email -> {{EMAIL_1}}` (plan and tier are not PII).
- After the guards run, values are restored and re-checked.
  `app/pii.py:134` `rehydrate()`, `app/pii.py:159` `unresolved_placeholders()` (any `{{...}}` left fails the draft).
- Card-shaped digits in free text are caught by a Luhn check and masked to `{{CARD}}`.
  `app/pii.py:164` `scrub_freeform()`, `app/pii.py:68` `_luhn_ok()`.
- The mask/rehydrate/re-guard chain is wired into the LLM node.
  `app/nodes.py:39-44` (imports), `app/nodes.py:651` (rehydrate after the model), `_guard_rehydrate_recheck` re-runs URL and secret guards on the hydrated text.
- Identifiers are hashed, never logged raw, with a salted hash.
  `app/pii.py:227` `hash_pii()`, salt from `PAYPILOT_PII_SALT` (`app/pii.py:193-220`); used at `app/tracing.py:91` (Langfuse user id) and `app/pii.py:300` (`*_sha256` fields).
- Logs carry only allowlisted fields.
  `app/pii.py:250` `LOG_SAFE_FIELDS`; enforced at `app/audit.py:134` `safe_log_fields()`.

Show it: open `app/pii.py`; run `.venv/bin/python -m pytest tests/test_gdpr_compliance.py -k "salt or mask" -q`.

## 2. Audit events (append-only, queryable, no PII)

- `app/audit.py:96` `AuditEventLog` - append-only by API surface: it exposes only append and read.
  `app/audit.py:119` `record()` (INSERT at `:138`), `app/audit.py:154` `query()`. There is no update or delete method (immutability note at `app/audit.py:76`, `:103`).
- One structured event per LLM call, with a boundary-normalized prompt hash and the guard verdict, and no PII.
  `app/audit.py:283` `audit_llm_call()`, hash at `app/audit.py:57` `prompt_sha256()`.
- Security events for signature failures, missing secret, blocked recipient, and output-guard refusals.
  `app/audit.py:247` `audit_security_event()`; both funnel to `app/audit.py:228` `_persist()` (best-effort append to the durable log).
- The invoice `transitions` table is also append-only, so any recovery number traces to the state moves that produced it (`app/store.py`).

Show it: open `app/audit.py` (point at the class having only `record` and `query`).

## 3. Retention (configurable auto-expiry + tested purge)

- Closed/terminal records expire after `PAYPILOT_RETENTION_MONTHS` (default 12).
  `app/store.py:718` `expired_invoice_ids()`, `app/store.py:736` `purge_expired()`.
- CLI purge job with a dry-run.
  `scripts/retention_purge.py` (113 lines).

Show it: `.venv/bin/python -m pytest tests/test_gdpr_compliance.py:271 -q` (`test_retention_purge_deletes_old_closed_keeps_recent_and_open`).

## 4. Right to access, portability, and erasure (scripted + proven by test)

- Export every row for a subject by id or email.
  `app/store.py:660` `export_customer()`; CLI `scripts/gdpr_export.py` (85 lines).
- Erase every row end to end, across ledger and roster; the erasure request is audited (hashed id) before deletion.
  `app/store.py:690` `erase_customer()`; CLI `scripts/gdpr_erase.py` (117 lines, `--dry-run`).
- The test proves deletion actually removes the rows and a fresh read finds nothing.
  `tests/test_gdpr_compliance.py:191` `test_erase_removes_every_ledger_row_and_read_finds_nothing`; plus match-on-stripe-id (`:219`), by-email (`:227`), dry-run no-op (`:239`), leaves-others-untouched (`:252`).

Show it: `.venv/bin/python -m pytest tests/test_gdpr_compliance.py -k erase -q`.

## 5. Access control and least privilege

- Webhooks are HMAC-SHA256 verified in constant time.
  `app/auth.py:37` `verify_webhook_signature()`, `hmac.compare_digest` at `app/auth.py:54`, keyed by `WEBHOOK_SECRET`.
- Admin and metrics routes are bearer-token gated in constant time.
  `app/auth.py:57` `verify_bearer()` (`:77`), keyed by `ADMIN_TOKEN`.
- Sending is off by default (dry run) and recipients must be explicitly allowlisted.
  `app/mailer.py:77` `sending_enabled()` (`PAYPILOT_SEND_EMAIL`), `app/mailer.py:88` `is_allowed_recipient()` (`PAYPILOT_ALLOWED_RECIPIENTS`; empty means nobody, the wildcard must be written out).
- The client's Stripe key is restricted to invoices (read), customers (read), and billing portal (write) only (README, deployment model).

Show it: open `app/auth.py` (both checks use `hmac.compare_digest`).

## 6. Output guard / prompt-injection protection (fail-closed, single choke point)

- Every outbound message passes one transport function where a host-allowlist output guard runs; a link to any non-Stripe host or a secret-shaped token is refused.
  `app/safety.py:48` `_DEFAULT_ALLOWED_HOSTS` (billing/invoice/pay `.stripe.com`, exact-host match, homoglyph/tab/CRLF hardened), enforced at `app/mailer.py:138` inside `_post_to_resend`.
- The canonical poisoned payload is committed as a permanent regression test.
  `tests/test_injection_regression.py`.

Show it: `.venv/bin/python -m pytest tests/test_injection_regression.py -q`.

## 7. Encryption, residency, tenant isolation

- TLS in transit: `fly.toml:17` `force_https = true`.
- EU/UK residency: `fly.toml:6` `primary_region = "lhr"`.
- Single-tenant isolation: `fly.toml:24` `max_machines_running = 1` (one machine, one volume, no shared store between client deployments); volume `fly.toml:38-39` `[mounts] source = "paypilot_data"`.

Show it: open `fly.toml`.

## 8. Data minimization (the strongest single point)

- The ledger does not persist customer name or email at all.
  The `failures` / `messages` / `events` / `transitions` tables hold only pseudonymous identifiers (`customer_id`, `stripe_customer_id`, `invoice_id`) and billing metadata (`app/store.py`).
- Name and email exist only transiently in graph state for one draft-and-send, masked to placeholders before the model, then discarded; the recipient address is not stored on the message row (only Resend's `provider_message_id`).

Show it: open `app/store.py` and point out that no table has a `name` or `email` column.

## 9. Change-management gates (CI)

- Every push and PR runs ruff, a house-style gate that rejects em and en dashes, and the full offline suite (`.github/workflows/ci.yml`, `scripts/lint_style.py`).
- Zero-token gates assert a full recovery constructs no chat model and a new inference call site without a written classification fails the build.

Show it: open `.github/workflows/ci.yml`.

## 10. EU AI Act transparency (AI-assistance disclosure, configurable, tested)

- Dunning emails can carry an AI-assistance disclosure line, appended at the single body-composition point so it passes the same output guard as the rest of the email.
  `app/loop.py:163` `ai_disclosure()`, applied in `app/loop.py:187` `compose_email_body()`.
- Configurable per client and per jurisdiction via `PAYPILOT_AI_DISCLOSURE`: custom text ships verbatim, `1` ships the default line (`app/loop.py:158` `DEFAULT_AI_DISCLOSURE`), unset means off.
- This is the Article 50 transparency control: the recipient of AI-drafted content can tell it was AI-assisted.
- Tested: off by default, default text when enabled, custom text per jurisdiction, passes the output guard, and sits last after the link.
  `tests/test_ai_disclosure.py` (6 tests).

Show it: `.venv/bin/python -m pytest tests/test_ai_disclosure.py -q`.

## Honest gaps (state these plainly, do not paper over them)

- At-rest volume encryption is a Fly platform control this deployment has not independently evidenced.
- `PAYPILOT_PII_SALT` must be set in production; unset falls back to a known default salt and warns.
- Alerting is log-based, not paged; no incident-response/key-rotation runbook yet; branch-protection required-check is a GitHub setting not visible in the repo.
- This is SOC2-ready (controls built and evidenced), not SOC2 certified. No audit has been performed.
