# PayPilot - SOC2-readiness

PayPilot is **SOC2-ready**, not SOC2 certified.
The distinction is deliberate and it is the honest posture: the controls a SOC2 examiner looks for exist in the architecture and are evidenced in code, and formal certification is a paperwork exercise run when a signed deal requires it.
Nothing in this document should be read as a claim of certification or of a completed audit.

This document maps each Trust Services Criterion that matters to PayPilot to the real mechanism that implements it, cites the file, and then states plainly what is not yet true.
The gaps list at the end is the part a reviewer, and the owner, should read first.

## Control-by-control

### 1. Access control and least privilege

- The client provisions a **restricted** Stripe key scoped to invoices (read), customers (read), and billing portal sessions (write), and nothing else (`README.md`, deployment model).
- The Stripe key and webhook signing secret live in the client's own deployment environment, never in Streamflow's, never in code.
- Admin and reporting endpoints (`/metrics`, `/report`, `/recovery-report`) are gated by a bearer token (`ADMIN_TOKEN`) and webhook requests by an HMAC signature (`app/auth.py`).
- Fail-closed behavior is evidenced: with no `STRIPE_WEBHOOK_SECRET` set, every event is rejected with a 400, and the only override (`PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS=1`) exists for the credential-free public demo only.

### 2. Audit logging

- One structured JSON audit event per LLM call (`app/audit.py`, `audit_llm_call`) records the node, model, a boundary-normalized prompt hash, the guard verdict, and the injection/fallback flags, and never any PII or prompt text.
- Security events (`audit_security_event`) are emitted for webhook signature failures, a missing signing secret, a blocked non-allowlisted recipient, and an outbound message that failed the output guard.
- Every delivery attempt is written to the `messages` table whether it sent, was suppressed, or failed (`app/mailer.py`), so the tool can always say what it attempted for which invoice.
- The `transitions` table is append-only, so any disputed recovery number can be traced back to the state moves that produced it (`app/store.py`).
- Security and LLM audit events also persist to an append-only, queryable event log (`app/audit.py`, `AuditEventLog`: SQLite, no update or delete method), enabled by env; app-layer immutability is enforced by the absence of any mutation path.

### 3. Tenant isolation

- One deployment per client, single-tenant; there is no multi-tenant control plane.
- Each client holds its own Stripe credentials, its own webhook secret, and its own SQLite ledger.
- `fly.toml` pins `max_machines_running = 1`, which is documented as isolating the per-machine volume: a second machine would get its own ledger and split the state, so scaling is deliberately blocked until a shared database exists.
- No client's data or keys can reach another, because there is no shared store between deployments.

### 4. Encryption

- **In transit:** `fly.toml` sets `force_https = true`, so all traffic is TLS.
- Outbound calls to Stripe and Resend are HTTPS REST calls.
- **At rest:** the ledger is a SQLite database on a Fly volume. See the gaps list: this deployment has not independently evidenced that the volume and any backups are encrypted at rest.

### 5. Change management

- CI runs on every push and every pull request to `main` (`.github/workflows/ci.yml`): a `ruff` lint, a house-style gate that rejects em and en dashes (`scripts/lint_style.py`), and the full offline test suite (`pytest -q`).
- The zero-token gates assert that a full recovery constructs no chat model and that a new inference call site without a written classification fails the build (`README.md`, testing section).
- A commit-message hook (`.githooks/commit-msg`) strips co-author trailers deterministically, enforced by git rather than by memory.
- Together these are a change-management control: the documented path is that no code reaches `main` without passing the checks.

### 6. Monitoring and availability

- A health check is configured in `fly.toml` (`/health`, 15s interval) so an unhealthy machine is detected.
- `/metrics` and the report endpoints expose operational figures behind auth.
- Security events for webhook signature failures are logged at warning or error severity, loud enough to alert on.
- An operator-alert path exists (`app/mailer.py`, `send_operator_alert`) that mails a fixed operator address through the same output-guarded transport as everything else.

### 7. Credential handling

- Keys are read from the environment only (Stripe key, `STRIPE_WEBHOOK_SECRET`, `WEBHOOK_SECRET`, `ADMIN_TOKEN`, `RESEND_API_KEY`, `OPENAI_API_KEY`), never hardcoded.
- Logs never echo a key, a signature, a recipient address, or a response body that could contain one (`app/mailer.py`, `app/audit.py`).
- A missing key fails closed (a blocked send, not a crash).

## Closed in this build

These items were open in an earlier draft of this document and are now addressed in code.
Each is stated with its remaining caveat, because "addressed" is not "audited."

- **`hash_pii` is now salted.**
  `app/pii.py` reads a salt from `PAYPILOT_PII_SALT` and prepends it before hashing, so a low-entropy identifier is no longer reversible by a plain dictionary.
  Caveat: when the env var is unset the code falls back to a known default salt and warns once at use; production must set a secret salt for the control to hold.

- **GDPR export, erasure, and retention-purge jobs exist, with tests.**
  `scripts/gdpr_export.py`, `scripts/gdpr_erase.py`, and `scripts/retention_purge.py` are implemented, and the suite proves erasure removes every ledger row (a subsequent read finds nothing) and that the purge expires terminal records after `PAYPILOT_RETENTION_MONTHS`.

- **Security and LLM audit events now persist to an append-only table.**
  `app/audit.py` adds `AuditEventLog` (SQLite, no update or delete method), enabled by env, so auth and security events have an in-database immutable store rather than depending only on the platform log stream.
  Caveat: the store is enabled by env and writes best-effort; file-level immutability still depends on the host.

## Honest gaps list

These are the items that are **not yet true** or **not yet evidenced**.
They are the difference between "SOC2-ready in architecture" and "audit-ready with evidence in hand."

1. **At-rest encryption is not independently evidenced.**
   The ledger sits on a Fly volume.
   Fly states that volumes are encrypted at rest, but this deployment has not captured its own evidence of that, nor of encrypted backups.
   Fix: confirm and document the volume encryption mechanism and the backup encryption, and attach the evidence.

2. **Alerting is log-based, not paged.**
   Webhook signature failures and guard refusals are logged loudly, but there is no automated paging or alerting pipeline wired to them beyond the operator-alert email used for copy detection.
   Fix: wire signature-failure and error-spike alerts to a monitored channel and document who is paged.

3. **Merge protection is not evidenced in the repository.**
   CI runs on every pull request, but "no code reaches production without passing" also depends on a branch-protection rule requiring the check, which is a GitHub setting not visible in the repo.
   Fix: enable and screenshot required-status-check branch protection on `main`.

4. **Incident-response and key-rotation runbook is not yet written.**
   A rotation procedure and an incident-response basics doc (who is paged, how a key is rotated) are named as SOC2 artifacts but are not yet in the repo.
   Fix: add a short runbook, and rotate any test credentials that were exposed during development as the first entry.

## Posture statement

The controls exist and are evidenced in code.
Formal SOC2 certification is a paperwork step Streamflow will run when a client's contract requires it; it is not being pursued speculatively, and no audit has been performed.
The honest answer to a client's compliance team is: "the controls are built and demonstrable, and the certificate is a step we complete when you need it."
</content>
