# Tenant configuration (M3)

Status: code done 2026-10-27 (rework 2). The milestone row is in `docs/multi-tenancy.md`.

## What exists

* `app/tenant_config.py`: `TenantConfig` (frozen, typed, parsed once; secrets hidden from `repr`
  and `str`), `config_for(tenant_id, db)` (the one builder), `config_of(store)` (the config of
  the tenant a store is bound to), `current_config()` (single mode only), `save_tenant_config`
  (tenant-facing writer: MERGES into the stored row and refuses platform-only fields),
  `set_platform_fields` (operator-only: the send switch and recipient allowlist),
  `customers_allowed(tenant_id)`, `behaviour_view()` (the config with every secret blank) and
  `running_config` / `run_config` (the full config for one running recovery, held in a context
  variable, never in graph state).
* `TENANT_ENV_FIELDS` in the same module is the closed list of tenant-level environment names
  and the field each one parses into. The guard tests and the inventory both read it.
* `tenant_config` table: additive, created by `Database()` through the same `IF NOT EXISTS`
  path every table uses. New and empty, so opening an existing file changes no row. Legacy
  files are still refused, never migrated, by `Database()`.
* Every call site that acts for a tenant takes that tenant's config from the store it is
  bound to (`config_of(store)`), or receives it as an argument. Nothing reads a tenant-level
  environment name except `app/tenant_config.py` and the deferred set below.

## Modes

* Single mode, the tenant named by `PAYPILOT_TENANT`: the environment is the base, and a
  `tenant_config` row lays OVER it field by field. A field the row does not set keeps its
  environment value. The send switch keeps its exact old rule: only `1`, `true` or `yes`, case
  sensitive after stripping. `True`, `TRUE` and `Yes` do NOT turn live sending on.
* Multi mode: the row is the only source for tenant settings. An unknown tenant raises
  `UnknownTenant`. The platform defaults below are the only environment values a tenant
  inherits. The reserved `demo` tenant has built-in defaults (sending off, no secrets).
* A call with no tenant bound (`current_config()` in multi) raises. Where a path cannot yet
  carry a config, it fails closed rather than reading the environment (see Known gaps).

## Single-mode behaviour changes against main

This is the list of differences a single-mode deployment with an UNCHANGED environment can see,
compared with main. It was assembled by reading the code paths changed in this milestone, not by
a machine diff of behaviour, so an item missing from it is a gap in this list, not a claim that
nothing else changed. The send switch and the LLM switch keep main's exact rule. Everything not
listed here reads the same value it read on main.

HTTP statuses:

* `/health` returns 503 with the error class name when the tenant configuration cannot be built
  (a stored row whose secrets do not decrypt, a missing or rotated `PAYPILOT_TENANT_KEY`, or a
  mismatched ledger). Main returned 200 whenever the ledger opened. Multi mode is still 200.
* A stored tenant row whose secrets cannot be decrypted, or with `PAYPILOT_TENANT_KEY` unset,
  makes `/config` and the stateless `/payment-failed` return 500. Main returned 200, because main
  never read the row. A deployment with no row is not affected by this item.
* An unopenable ledger makes `/config`, `/payment-failed`, `/payment-failed/batch`,
  `/portfolio-impact`, `/webhooks/recharge`, `/webhooks/chargebee` and `/metrics` return 500,
  EVEN WITH NO TENANT ROW. Main returned 200. Every tenant setting is now read through the bound
  tenant's configuration, and building it opens the ledger. "Unopenable" means an unmigrated
  legacy file, a file owned by another tenant, or a lock that outlasts the timeout. `/health`
  reports the same state as 503, so a deploy in that state fails its health check rather than
  serving 500s. A deployment whose ledger opens is unaffected.

Defaults and values:

* Customer fixtures (`data/customers.json`) are read only when the tenant is `demo`
  (`PAYPILOT_TENANT=demo`). The README quickstart and `.env.example` set it. Any other tenant
  gets an empty customer list, and the refusal is logged ONCE per process as a warning.
* `PAYPILOT_LLM_BUDGET_USD` that is not a number (for example `5O`) or is negative, or `nan`, is a
  ZERO ceiling, which refuses all model use. Main raised a price-table error. Blank or unset is
  still no ceiling. `inf` was an unlimited ceiling on main and now refuses all model use as well,
  which is intentional: a ceiling that cannot be met is not a ceiling.
* A zero or negative LLM ceiling refuses model use even for an unpriced model call. Main allowed
  an unpriced call as "unknown".
* `PAYPILOT_SEND_EMAIL=1` on the `demo` tenant does nothing: the demo deployment never sends and
  never creates an approval (see the demo statement below).
* `PAYPILOT_IMAP_PORT` accepts ASCII digits only, as main did. A non-ASCII digit is refused.
* `PAYPILOT_UPDATE_URL` set to the empty string stays empty, so every link is refused, as on main.
  Unset uses the built-in default, as on main. The boot warning is emitted for both unset and
  empty, as on main (a test pins it).
* `PAYPILOT_FROM_EMAIL` set but blank is a REFUSAL: the send is suppressed with
  `missing_from_address` and nothing is posted. Main posted a blank From, which the provider
  rejects, so no mail went out on main either. Unset keeps the built-in sender.
* `PAYPILOT_ALLOWED_LINK_HOSTS` set to a list of only separators (for example `, ,`) allows no
  host, as on main. Blank or unset uses the Stripe defaults, as on main.
* `PAYPILOT_IMAP_SECURITY` is lower-cased before use, as the environment always was.
* `PAYPILOT_AI_DISCLOSURE` keeps main's lower-case rule for the switch words.

Startup and logs:

* A startup hook that fails no longer stops the process. It is logged once as an ERROR line
  `startup_step_failed` with the step name and the error class, and the process boots. Main
  stopped at boot. A normal multi-mode boot logs no such line.
* The PII salt falls back to the documented unset salt, with its one warning, on ANY configuration
  error, not only when the salt is unset. Main had no configuration to fail.
* The fixture refusal warning is new (one line per process).

Sending and approvals:

* The canary's operator alert, when it supplies its own key and sender, no longer reads the
  ledger. A legacy or foreign ledger no longer stops the canary.
* A send the provider accepted (2xx) is never turned into a failure by the ledger write that records
  it. The write is retried up to three times; if it still fails the result is `sent` with
  `message_id` None and `ledger_write_failed` True, the class-only log line `sent_but_unrecorded`
  is written, and a `sent_but_unrecorded` security audit event is emitted on the separate audit
  store. Cooldown and the touch cap read the messages table and cannot see such a send, so the
  mailer remembers it in process memory for the UTC day and suppresses a second send for that
  invoice (`sent_unrecorded_today`). A process restart forgets that memory; the audit event is the
  durable trace. The retry can block the calling thread for up to about 0.15 s plus the lock wait.
  In approval mode off, a Stripe redelivery with a new event id for the same invoice meets the same
  day guard. A failed COMMIT is rolled back before any retry, so a retry never records two rows.
* A draft that may already exist at the mail provider (the provider answered 200 but with an
  unreadable body) is recorded as a FAILED approval with `draft_create_failed`; a human reconciling
  FAILED approvals should look in the mailbox before re-requesting.
* A stored `from_address` that is refused (outside `allowed_from_domains`, or without a domain)
  falls back to the base config's sender: the environment value in single mode, `DEFAULT_FROM` in
  multi mode.
* Saving a blank `update_url` is refused; a blank one already in a row is ignored and the base
  link is used.
* A blank From in approval mode: the approval request is recorded `approval_request_undeliverable`
  and operator alerts return FAILED. In direct (off) mode the send is suppressed with
  `missing_from_address`. The preflight reports a blank `PAYPILOT_FROM_EMAIL` as a problem.
* `PAYPILOT_PII_SALT` and `PAYPILOT_IMAP_PASSWORD` keep a whitespace-only value exactly; only the
  empty string is unset.
* An approval whose send raises after the claim is recorded FAILED, not left APPROVED. An approval
  whose mail was delivered and whose state transition then fails is recorded SENT.
* A tenant's stored `from_address` is honoured only inside the platform's `allowed_from_domains`.
  This applies to stored rows only. The environment's `PAYPILOT_FROM_EMAIL` is always honoured.
* Gmail draft clients are cached per tenant and credential. A superseded client is closed after
  its last in-flight request finishes, not immediately. Main built one client per call.

Operator-only settings (new in this milestone):

* `llm_draft`, `llm_budget_usd`, `allowed_from_domains` (and `send_enabled`, `allowed_recipients`
  as before) are platform-only. A tenant row cannot set them. The operator sets them with
  `set_platform_fields`. The environment values are unchanged.

Evaluation and scripts:

* The evaluation guard reads the deployment's own card-update link (`PAYPILOT_UPDATE_URL`), not
  the built-in constant. An operator override is now the link the eval accepts.

## The demo deployment (statement, tested)

A deployment whose tenant is `demo` (`PAYPILOT_TENANT=demo`, as the public demo runs) never sends
mail and never creates an approval, whatever `PAYPILOT_SEND_EMAIL` or any other switch says. This
is deliberate: the public demo must not send. Test: `test_f5_a_demo_deployment_never_sends_and_creates_no_approvals`.

## Guards (tests/test_tenant_rework.py, tests/test_tenant_rework2.py, tests/test_tenant_config.py)

* `test_no_tenant_env_name_outside_the_config_module_except_the_deferred_set`: any string that
  names a tenant-level variable, at import or call time, direct, through a helper, a module
  constant, a concatenation or an f-string, and in any file under `app/` at any depth, fails
  unless it is on the deferred set. Docstrings are not reads. Any reference to the closed list
  `TENANT_ENV_FIELDS` outside the config module also fails.
* `test_the_deferred_set_is_exact`: the deferred set is COUNTS per (module, name). A new read
  fails, including a second read of a name already deferred. A read that disappears fails too.
* `test_the_guard_catches_each_planted_form` and `test_the_guard_catches_the_blind_spots_too`:
  controls for every form above, including the subpackage and the built names.
* `test_no_module_assigns_the_stripe_sdk_global_key`: follows `import stripe as x` and
  `setattr(x, "api_key", ...)`. A planted control covers each form.
* `test_the_current_config_fallbacks_are_exactly_the_pinned_set`: every remaining
  `current_config()` fallback is pinned by (module, function). A new one fails until it is
  pinned or the config is passed explicitly. The set may only shrink.
* `test_b1_*`: no secret reaches any state the graph yields, the run_recovery spy sees only the
  behaviour view, the Langfuse `EventSerializer` (positive control first) exports no secret,
  and `_mask_value` redacts a dataclass instead of passing it through.
* `test_b2_*`: the transport guard reads the BOUND tenant's card-update link. The test patches
  `httpx.post`, so the real output guard runs; a foreign link is still refused.
* `test_each_tenant_recovery_uses_its_own_config_and_nothing_leaks`: three tenants in one
  database with different approval modes, approvers, senders, keys, update links, business
  names, holdout shares and cooldowns, run through `loop.handle_payment_failed` with the real
  graph, approval and mailer. A fake Resend transport records who was mailed and with which key.
  Each tenant's output uses its own config, and no environment value leaks into any tenant.
* `test_gmail_and_imap_credentials_come_from_the_tenant_secrets`: a tenant's mailbox
  credentials are its own; an environment credential never makes a tenant look configured.
* `test_a_pre_m3_current_file_gains_the_empty_table_and_keeps_every_row`: a current-schema file
  without the table opens, gains the empty table, and every row is byte-identical by digest.

## Platform defaults a tenant inherits (decision, M5)

The design (docs/multi-tenancy.md) places the shared Resend key, the default touch cap and the
default cooldown at platform level. The code follows it:

* `resend_api_key`: the tenant's own key if its row sets one, else the shared key from the
  environment (`RESEND_API_KEY`).
* `max_touches` and `send_cooldown_hours`: the tenant's own value if its row sets one, else
  the platform default from the environment (`PAYPILOT_MAX_TOUCHES`,
  `PAYPILOT_SEND_COOLDOWN_HOURS`).
* `from_address` stays tenant-level. A tenant with no `from_address` gets the built-in default,
  never the environment's `PAYPILOT_FROM_EMAIL`.

## Stored rows: merge, empty means unset (m3, m4)

* A save merges the fields it names into the stored row and keeps every other field, including
  the operator's platform fields and every secret it does not name.
* A null or blank stored value means unset: it keeps the base value, the environment in single
  mode or the platform default in multi mode. A blank `from_address` is the default, never an
  empty From line.

## Deferred to M4/M5 (pinned by tests)

| Module | Variable | Reads | Milestone | Why |
|---|---|---|---|---|
| `app/api.py` | `PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS` | 1 | M5 | unsigned webhooks become a per-tenant decision in code |
| `app/api.py` | `STRIPE_WEBHOOK_SECRET` | 2 | M5 | per-tenant webhook URL and secret |
| `app/api.py` | `RECHARGE_CLIENT_SECRET` | 2 | M5 | per-tenant webhook secret |
| `app/api.py` | `CHARGEBEE_WEBHOOK_SECRET` | 2 | M5 | per-tenant webhook secret |
| `app/api.py` | `ADMIN_TOKEN` | 2 | M4 | principal tokens replace the static token |
| `app/api.py` | `WEBHOOK_SECRET` | 2 | M4/M5 | legacy shared secret, retired in multi mode |
| `app/decision.py` | `DECISION_API_TOKEN` | 1 | M4 | decision-API tokens become per-principal |

## Known gaps (said plainly)

* Routes in `app/api.py` that call a default configuration (for example the AI-disclosure
  values in the response bodies) fail closed in multi mode, because there is no ambient tenant.
  They are not yet tenant-routed. M4 owns route tenancy. This was not exercised route by route.
* Background sweeps (`approval.start_background_sweep`) read the deployment's mode with no
  store, so they only run in single mode. Multi-mode sweeps are M4/M5 work.
* Log hashing (`pii.hash_pii` for `safe_log_fields`) has no tenant when a log line is written
  with no config. In multi mode it uses the documented unset salt and logs a warning, so the
  log is written. Correlation keys across tenants are therefore not per-tenant on that path.
* The Gmail client is cached per (tenant, credential fingerprint) and the superseded client is
  closed. IMAP opens one connection per operation and logs it out, so it holds no persistent
  socket. No leak test for IMAP is claimed here beyond that structure.
* The Mercury integration token (`MERCURY_API_TOKEN`) is a platform-level credential read in
  `app/mercury_client.py`. It is not a tenant setting in M3. Open item: decide whether each
  tenant brings its own.
* `app/mailer.py` operator alerts use the tenant's config when one is passed, and the bound
  store's config otherwise. Multi mode with no bound tenant raises rather than guessing.
* Tightenings made in this rework, each deliberate. Customer-visible: free-text fields that are
  rendered into a message (a customer name, a plan description) go through `_safe_field`, which
  now refuses ANY URL (`allow_hosts=False`), where main let an allowlisted host through; such a
  field falls back to its neutral default. The business-name check is unchanged from main and
  still allows an allowlisted host. The portal return URL is validated against the tenant's own
  allowlist.
* `scripts/approval_preflight.py` reads the environment only (it builds a configuration with
  `platform_config()`); it never opens or creates a ledger and never reports a stored row's
  values. This is the main behaviour and a test pins it.

## Decisions

* The suite does not run as the demo tenant globally. Fixture tests run as the demo tenant in
  the modules that read `data/customers.json`, and live-send tests run as the default tenant.
* `PAYMENT_UPDATE_URL` stays exported as the platform default constant. Every tenant's value is
  read through `payment_update_url(config)`, and `None` in the output guard now means the BOUND
  tenant's card-update link, not the platform default.
* `set_platform_fields` is the operator path for the send switch and the recipient allowlist.
  It writes them; the tenant-facing writer refuses them.
* The graph state holds the behaviour view only. Secrets reach a node through
  `run_config(...)`, held in a context variable for the one running recovery.

## Tenant-level variables (read in one place)

The scanner above matches literal `os.getenv("NAME")` calls, so it cannot see reads made through the
table-driven loop in `app/tenant_config.py` (`_env_raw`, over `TENANT_ENV_FIELDS`). Those reads are
the tenant-level ones, and they are listed here straight from that table. A test
(`tests/test_tenant_doc.py`) fails if a name in the table is missing from this document, so the
list cannot drift from the code.

| Environment variable | Config field | Kind |
|---|---|---|
| `PAYPILOT_APPROVAL_MODE` | `approval_mode` | behaviour |
| `PAYPILOT_APPROVER_EMAILS` | `approver_emails` | behaviour |
| `PAYPILOT_APPROVAL_TTL_HOURS` | `approval_ttl_hours` | behaviour |
| `PAYPILOT_SEND_EMAIL` | `send_enabled` | behaviour |
| `PAYPILOT_ALLOWED_RECIPIENTS` | `allowed_recipients` | behaviour |
| `PAYPILOT_FROM_EMAIL` | `from_address` | behaviour |
| `PAYPILOT_BUSINESS_NAME` | `business_name` | behaviour |
| `PAYPILOT_UPDATE_URL` | `update_url` | behaviour |
| `PAYPILOT_PORTAL_RETURN_URL` | `portal_return_url` | behaviour |
| `PAYPILOT_ALLOWED_LINK_HOSTS` | `allowed_link_hosts` | behaviour |
| `PAYPILOT_HOLDOUT_PCT` | `holdout_pct` | behaviour |
| `PAYPILOT_HOLDOUT_SEED` | `holdout_seed` | behaviour |
| `PAYPILOT_AI_DISCLOSURE` | `ai_disclosure` | behaviour |
| `PAYPILOT_MAX_TOUCHES` | `max_touches` | behaviour |
| `PAYPILOT_SEND_COOLDOWN_HOURS` | `send_cooldown_hours` | behaviour |
| `PAYPILOT_HIGH_VALUE_ANNUAL` | `high_value_annual` | behaviour |
| `PAYPILOT_LLM_DRAFT` | `llm_draft` | behaviour |
| `PAYPILOT_LLM_BUDGET_USD` | `llm_budget_usd` | behaviour |
| `PAYPILOT_GMAIL_GRACE_MINUTES` | `gmail_grace_minutes` | behaviour |
| `PAYPILOT_IMAP_HOST` | `imap_host` | behaviour |
| `PAYPILOT_IMAP_PORT` | `imap_port` | behaviour |
| `PAYPILOT_IMAP_USER` | `imap_user` | behaviour |
| `PAYPILOT_IMAP_SECURITY` | `imap_security` | behaviour |
| `PAYPILOT_IMAP_DRAFTS_FOLDER` | `imap_drafts_folder` | behaviour |
| `PAYPILOT_IMAP_SENT_FOLDER` | `imap_sent_folder` | behaviour |
| `STRIPE_API_KEY` | `stripe_api_key` | secret (encrypted per tenant) |
| `STRIPE_SECRET_KEY` | `stripe_api_key` | secret (encrypted per tenant) |
| `STRIPE_WEBHOOK_SECRET` | `stripe_webhook_secret` | secret (encrypted per tenant) |
| `RECHARGE_CLIENT_SECRET` | `recharge_client_secret` | secret (encrypted per tenant) |
| `CHARGEBEE_WEBHOOK_SECRET` | `chargebee_webhook_secret` | secret (encrypted per tenant) |
| `RESEND_API_KEY` | `resend_api_key` | secret (encrypted per tenant) |
| `PAYPILOT_PII_SALT` | `pii_salt` | secret (encrypted per tenant) |
| `PAYPILOT_GOOGLE_CLIENT_ID` | `gmail_client_id` | secret (encrypted per tenant) |
| `PAYPILOT_GOOGLE_CLIENT_SECRET` | `gmail_client_secret` | secret (encrypted per tenant) |
| `PAYPILOT_GMAIL_REFRESH_TOKEN` | `gmail_refresh_token` | secret (encrypted per tenant) |
| `PAYPILOT_IMAP_PASSWORD` | `imap_password` | secret (encrypted per tenant) |

## Inventory of environment reads in `app/`

Generated from the code by the scanner (`scripts` are not in it). Disposition is one of:
`tenant-level` (parsed in `TenantConfig`), `tenant-level (deferred)` (listed above),
`platform-level` (stays in the environment, with the reason), or `helper argument` (the name is
passed in by a caller whose literal is in the table). No row is `NOT MOVED`, and none is
unclassified. Reads made through the table-driven loop are listed in the previous section, not here.

| File | Line | Variable | Disposition | Status |
|---|---|---|---|---|
| `app/api.py` | 365 | `?name` | helper argument | name is passed in by a caller; the caller's literal is in the table |
| `app/api.py` | 372 | `?name` | helper argument | name is passed in by a caller; the caller's literal is in the table |
| `app/api.py` | 999 | `?DECISION_TOKEN_ENV` | helper argument | name is passed in by a caller; the caller's literal is in the table |
| `app/bank/consent.py` | 167 | `?env` | helper argument | name is passed in by a caller; the caller's literal is in the table |
| `app/lambda_handler.py` | 133 | `?DECISION_TOKEN_ENV` | helper argument | name is passed in by a caller; the caller's literal is in the table |
| `app/mailer.py` | 66 | `?name` | helper argument | name is passed in by a caller; the caller's literal is in the table |
| `app/tenant_config.py` | 359 | `?env_name` | helper argument | name is passed in by a caller; the caller's literal is in the table |
| `app/api.py` | 377 | `RATE_LIMIT_MAX` | platform-level | platform cap on this process's request rate (one limiter for every tenant) |
| `app/api.py` | 378 | `RATE_LIMIT_WINDOW_SECONDS` | platform-level | platform rate-limit window |
| `app/api.py` | 379 | `RATE_LIMIT_MAX_IPS` | platform-level | platform cap on tracked client addresses (memory bound) |
| `app/api.py` | 380 | `RATE_LIMIT_GLOBAL_MAX` | platform-level | platform cap on total request rate |
| `app/api.py` | 460 | `IDEMPOTENCY_MAX` | platform-level | platform memory bound of the idempotency cache, keyed by tenant in the key |
| `app/api.py` | 956 | `PAYPILOT_COMMIT` | platform-level | deployment identity: the commit this process runs |
| `app/approval.py` | 111 | `PAYPILOT_PUBLIC_BASE_URL` | platform-level | one public origin for approval links; platform by design |
| `app/audit.py` | 108 | `PAYPILOT_AUDIT_DB_PATH` | platform-level | storage location of the platform audit log |
| `app/audit.py` | 216 | `PAYPILOT_AUDIT_DB_PATH` | platform-level | storage location of the platform audit log |
| `app/bank/consent.py` | 201 | `PAYPILOT_CONSENT_DB_PATH` | platform-level | storage location of the platform consent store |
| `app/bank/fake.py` | 128 | `PAYPILOT_FAKE_BANK` | platform-level | demo and test flag of the bank simulator, not a merchant setting |
| `app/bank/http.py` | 153 | `PAYPILOT_BANK_FAILURE_STATE` | platform-level | demo and test flag of the bank simulator, not a merchant setting |
| `app/decision_audit.py` | 163 | `PAYPILOT_DECISION_AUDIT_DB_PATH` | platform-level | storage location of the platform decision log |
| `app/ingest.py` | 124 | `OPENAI_API_KEY` | platform-level | model provider key: platform-level (the per-tenant drafting switch is tenant config) |
| `app/lambda_handler.py` | 111 | `PAYPILOT_COMMIT` | platform-level | deployment identity: the commit this process runs |
| `app/mailer.py` | 71 | `PAYPILOT_SEND_MAX_ATTEMPTS` | platform-level | transport retry policy of this process, shared by every tenant |
| `app/mailer.py` | 73 | `PAYPILOT_SEND_BACKOFF_SECONDS` | platform-level | transport retry backoff of this process, shared by every tenant |
| `app/mercury_client.py` | 98 | `MERCURY_API_TOKEN` | platform-level | credential of the Mercury bank integration; platform-level and NOT in M3 scope (open item) |
| `app/meter.py` | 87 | `PAYPILOT_MODEL_PRICES` | platform-level | model price table: platform-level, the same prices for every tenant |
| `app/migrate_tenancy.py` | 455 | `PAYPILOT_DB_PATH` | platform-level | where this deployment keeps its database file |
| `app/migrate_tenancy.py` | 456 | `PAYPILOT_LEGACY_TENANT` | platform-level | migration-only: names the owner of a legacy file |
| `app/nodes.py` | 147 | `OPENAI_API_KEY` | platform-level | model provider key: platform-level (the per-tenant drafting switch is tenant config) |
| `app/nodes.py` | 699 | `OPENAI_MODEL` | platform-level | model name: platform-level, the same model for every tenant |
| `app/store.py` | 342 | `PAYPILOT_TENANT` | platform-level | names the tenant a single-mode deployment is bound to |
| `app/store.py` | 395 | `PAYPILOT_DB_PATH` | platform-level | where this deployment keeps its database file |
| `app/store.py` | 1307 | `PAYPILOT_TENANCY` | platform-level | selects the tenancy mode itself |
| `app/tenant_config.py` | 419 | `PAYPILOT_TENANT_KEY` | platform-level | platform key that encrypts tenant secrets; not itself a tenant secret |
| `app/tracing.py` | 38 | `PYTEST_CURRENT_TEST` | platform-level | set by pytest; read to keep test runs from sending real alerts |
| `app/tracing.py` | 40 | `LANGFUSE_PUBLIC_KEY` | platform-level | observability backend credential for this deployment |
| `app/tracing.py` | 40 | `LANGFUSE_SECRET_KEY` | platform-level | observability backend credential for this deployment |
| `app/tracing.py` | 87 | `LANGFUSE_BASE_URL` | platform-level | observability backend address for this deployment |
| `app/tracing.py` | 88 | `LANGFUSE_HOST` | platform-level | observability backend address for this deployment |
| `app/tracing.py` | 89 | `LANGFUSE_HOST` | platform-level | observability backend address for this deployment |
| `app/tenant_config.py` | 589 | `PAYPILOT_MAX_TOUCHES` | tenant-level | parsed once in TenantConfig.max_touches |
| `app/tenant_config.py` | 591 | `PAYPILOT_SEND_COOLDOWN_HOURS` | tenant-level | parsed once in TenantConfig.send_cooldown_hours |
| `app/tenant_config.py` | 593 | `RESEND_API_KEY` | tenant-level | parsed once in TenantConfig.resend_api_key |
| `app/api.py` | 171 | `WEBHOOK_SECRET` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 176 | `ADMIN_TOKEN` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 181 | `STRIPE_WEBHOOK_SECRET` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 194 | `RECHARGE_CLIENT_SECRET` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 199 | `CHARGEBEE_WEBHOOK_SECRET` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 244 | `WEBHOOK_SECRET` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 261 | `ADMIN_TOKEN` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 1328 | `PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 1350 | `STRIPE_WEBHOOK_SECRET` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 1453 | `RECHARGE_CLIENT_SECRET` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
| `app/api.py` | 1554 | `CHARGEBEE_WEBHOOK_SECRET` | tenant-level (deferred) | DEFERRED to M4/M5, pinned by tests/test_tenant_rework.py |
