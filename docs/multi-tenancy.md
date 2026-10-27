# Multi-tenancy: design (v2, after review)

Status: DESIGN, written 2026-10-27, revised the same day after an independent
review returned REWORK with three blockers. Each blocker was checked against the
code before the design changed: `verify_bearer` does allow everyone when
`ADMIN_TOKEN` is unset (`app/auth.py`), the idempotency cache keys carry no tenant
(`app/api.py`), and the Stripe SDK key is a process-global assignment
(`app/stripe_client.py`). Nothing below is built until its milestone says so.

## Why

PayPilot is single-tenant by construction. One SQLite ledger keyed by `invoice_id`,
and every merchant-specific setting a process-wide environment variable. Serving a
second merchant meant a second Fly app: a deploy per merchant, which does not scale.

The decision slice (`/decide`) is already multi-tenant and is the reference pattern.
The recovery ledger, mailer, approvals and webhooks are not.

## The line this design holds

1. **A tenant id is never taken from the request body.** It comes from the token, or
   from a per-tenant webhook URL whose signature only that tenant's secret can
   satisfy. A path segment is a label; the signature is the authentication.
2. **No ambient tenant.** Code that touches tenant data holds a tenant-bound object.
   There is no module-level "current tenant", no store method taking a tenant id,
   and no `store or get_store()` fallback on a tenant path. In multi mode
   `get_store()` raises.
3. **Per-tenant settings never fall back to the environment.** Only the deployment's
   own `default` tenant reads env.
4. **A wrong-tenant lookup is a 404,** with the same body and the same work as a
   missing one.
5. **A role nobody checks is not a role,** and a static shared secret is not a
   principal.
6. **Nothing process-wide may hold tenant data or a tenant credential.** Caches,
   SDK keys, clients and counters are keyed by tenant or hold none.

## Two modes, one code path

`PAYPILOT_TENANCY` is `single` (default) or `multi`.

* **single**: exactly today's behaviour. One tenant, `default`, configured from the
  environment. A dedicated merchant app (the install kit already built) is this mode,
  which some regulated buyers will require.
* **multi**: tenants come from the database. Nothing fails open: no unsigned
  webhooks except the `demo` tenant, no anonymous admin routes, no ambient store.

The public demo runs `single` with its one tenant named `demo` (see Migration).

## Tenant model

`tenants`: `id` (`tn_` plus 16 hex, the only form in URLs), `slug`, `name`, `status`
(`active`, `suspended`, `offboarded`), `status_reason` (ships with the state column),
`config_json`, `secrets_enc`, `verified_from_domains`, `created_at`, `updated_at`.

Reserved: `default` (the deployment's own tenant, env-configured) and `demo` (fake
data only; sending is forced off for it in code, and customer fixtures
(`data/customers.json`) resolve for `demo` and for no other tenant).

### Configuration

`TenantConfig`, frozen, from one function `config_for(tenant_id)`. Per tenant:

* behaviour: approval mode and TTL, approver emails, recipient allowlist, send switch,
  from address, business name, update URL, portal return URL, allowed link hosts,
  holdout share and seed, AI disclosure, touch cap and cooldown overrides,
  high-value threshold, LLM drafting on or off (a per-merchant data-processing choice),
  Gmail grace.
* secrets (encrypted): Stripe, Recharge and Chargebee webhook secrets, optional
  Stripe API key, optional Resend key, PII salt, Gmail OAuth credentials, IMAP
  credentials.

Platform-level (environment): public base URL, default cap and cooldown, the shared
Resend key, the Fernet key, bootstrap token.

**Import-time reads are removed, not worked around.** `PAYMENT_UPDATE_URL` is copied
by value into `loop.py`, `nodes.py` (and into a prompt) and `stripe_client.py`;
`mailer.py` and `api.py` read settings at import. Each becomes a value taken from the
config object at the call site. This is the largest mechanical part of M3.

### Secrets

Fernet under `PAYPILOT_TENANT_KEY`, the mechanism of the bank consent store
(`app/bank/consent.py`): no key, no store. Decrypted at the point of use, never
logged, never returned by any route. The PII salt is per tenant so hashed customer
ids do not correlate across merchants.

**The Stripe SDK key is passed on every call** (`api_key=` per request), never
assigned to the module. A process-global key in a threadpool is a cross-tenant
credential race.

## Data isolation

`Database` owns the connection, lock, schema and migrations. `Store` is a view bound
to one tenant at construction. `Platform` is a separate object for cross-tenant work
(list tenants, sweep, platform metrics), reachable only through `authorize()`.

Two narrow resolvers exist because the caller has no principal yet:
`Platform.approval_tenant(approval_id)` for the signed-link page and
`Platform.message_tenant(provider_message_id)` for Resend callbacks (the transport is
shared). Each returns a tenant id and nothing else, and the caller must then pass the
capability check (link token) or signature check before anything is read.

Schema, following `data-model-evolution-standard.md`:

* `tenant_id TEXT NOT NULL` on every table. Composite keys: `failures(tenant_id,
  invoice_id)`, `events(tenant_id, event_id)`. The partial unique index on pending
  approvals becomes `(tenant_id, invoice_id)`; left global it is an existence oracle.
* Joins carry the predicate in the ON clause: `messages m JOIN failures f ON
  f.invoice_id = m.invoice_id AND f.tenant_id = m.tenant_id`. A `WHERE tenant_id = ?`
  on one side only would let one tenant's messages match another's failure row.
* Every INSERT lists `tenant_id` explicitly. The structural test checks column lists
  for INSERTs, not only predicates for reads.
* Table names in any dynamic SQL come from a constant allowlist.
* The early-paid marker gets its own table `early_paid(tenant_id, invoice_id,
  seen_at)`. The current salted-key marker is regenerated per process, so it is
  already lost on every restart; this fixes a bug the migration would otherwise
  carry across.
* `audit_events` gains `tenant_id` and every `audit_security_event` caller supplies
  one. Audit rows are append-only and survive offboarding by design; they hold hashed
  subjects, not raw PII, and say so.

SQLite has no row-level security, so isolation is enforced in the one data layer and
proved four ways: a structural test over `store.py` (predicates, JOIN conditions,
INSERT column lists), behavioural tests with two tenants sharing the same invoice id
across every read, write, count, export, erase and purge, a mutation test that removes
the predicate and must go red, and a test that no tenant-path function accepts a
default store.

**Honest limit.** This is multi-tenant correct on one machine and one volume. Fly
volumes are per machine, so it is not horizontally scalable, and one SQLite writer
lock is shared by every tenant (noisy neighbour). The scale-out step is Postgres with
`FORCE ROW LEVEL SECURITY` behind the same `Store` interface (M7), as the decision
slice already does. Also shared and unaddressed here: the LLM spend budget, the
request metrics, and the IP rate limiter. The limiter key gains the tenant; the rest
are listed as follow-ups, not claimed solved.

## Migration

The existing database is rebuilt, never altered in place, and never lazily:

* **Explicit step, not `Store()`.** `python -m app.migrate_tenancy`, run by
  `scripts/start.sh` before `uvicorn`, on the machine that owns the volume. (DECISION
  2026-10-27: not the Fly `release_command`, because a release machine is not known
  to mount the volume, and a migration run against nothing would report success.)
  `/health` opens the ledger and answers 503 when it cannot. Opening a legacy-schema
  database without migrating it refuses with a clear error; `Store()` never migrates.
* **The target tenant is never guessed.** `PAYPILOT_LEGACY_TENANT` must be set when
  legacy rows exist, or the migration refuses. The public app sets it to `demo`.
  A dedicated merchant app sets it to `default`. A wrong guess would put a real
  merchant's open approvals in a tenant that cannot send, which is silent.
* **File backup first**, byte-verified, before any table is touched.
* **One transaction**, with `approvals.id` copied explicitly (it is already in links
  that have been sent) and `sqlite_sequence` carried so new ids continue the series.
* **The check measures the whole.** Not row counts alone, which are blind to a shifted
  column: a content hash per table over a canonical serialisation of every column
  except `tenant_id`, computed before and after and required equal, plus counts, plus
  the sum over tenants equal to the old total.
* Rows move to a tenant that can only send LESS, never more: fails safe.

## Identity and roles

A `Principal` is `(tenant_id or None, role, token_id)`. Tokens are `pp_` plus a short
id plus a secret. The table stores a SHA-256 of the secret with `created_at`,
`last_used_at`, `revoked_at`, `revoked_reason`. The caller sends the token and
nothing else.

**There is no anonymous principal and no static one.** `ADMIN_TOKEN` stops being a
principal. In multi mode it is a one-time bootstrap value: `scripts/tenants.py
bootstrap` uses it to mint the first real `platform_admin` token, after which it is
refused. In multi mode with neither a platform token nor a bootstrap value, every
platform route is 503. The fail-open behaviour for an unset token exists only in
`single` mode, where it is the demo's documented behaviour.

| Role | Scope | May |
|---|---|---|
| `platform_admin` | all tenants | create, suspend, offboard tenants; platform-only config; mint any token; sweep; platform metrics |
| `tenant_admin` | one tenant | tenant-editable config; rotate that tenant's own secrets (write-only); mint tokens for roles it contains; list approvals; report; GDPR export and erase |
| `tenant_approver` | one tenant | list and decide approvals; read recipient addresses |
| `tenant_viewer` | one tenant | report and approval list with recipient addresses masked |
| `tenant_service` | one tenant | the decision API (`/decide`, outcome, audit lookup) and nothing else |

Role containment, so "not above its own role" has meaning: a role may be granted only
by a principal whose permission set contains it. `tenant_admin` contains approver,
viewer and service; approver contains viewer; nothing mints `platform_admin` except
`platform_admin`.

**Platform-only fields, not tenant-editable:** the send switch, the recipient
allowlist (so a tenant cannot set `*`), the verified from-domain list, and tenant
status. The from address a tenant may set must be on its verified domains, because
Resend is shared and an unrestricted from address would let one tenant send as
another.

The signed approval link is a capability, not a role. It authorises one approval,
bound to the approval row and so to its tenant. `decide()` also checks tenant
status; there is no API route that decides approvals except through `decide()`.

**One authorisation function**, `authorize(principal, action, tenant_id)`, returns a
verdict and a reason. Every route calls it. Coverage tests: every role is granted at
least one action; every action is checked by at least one route.

**One identity, not two.** The decision slice's `client_id` IS the tenant id. In multi
mode `DECISION_API_TOKEN` is retired and `/decide` authenticates with a
`tenant_service` token. In single mode the old token map still works.

## Webhooks

`POST /t/{tenant_id}/webhooks/stripe` (and the Recharge and Chargebee equivalents),
signature checked against that tenant's secret.

* **One failure body.** Unknown tenant, offboarded tenant, bad signature, missing
  signature and unconfigured secret return the same status and the same body. The
  unknown-tenant path still performs the HMAC against a dummy secret so timing does
  not distinguish it.
* **Unsigned webhooks are allowed only for `demo`,** decided in code, not by a
  platform-wide flag. A new tenant whose secret is not set refuses.
* **Idempotency keys carry the tenant:** `pf`, `bt`, `stripe`, `recharge`,
  `chargebee`, all of them. Chargebee event ids are per site and can legitimately
  collide across tenants, and an un-keyed cache would hand one tenant another's cached
  response.
* The legacy `/webhooks/stripe` resolves to `default` and keeps its environment
  secret.

## Suspension

Suspension must not cause a paid customer to be dunned. So a suspended tenant:

* still has closing events applied (`invoice.paid`, `invoice.payment_succeeded`,
  `customer.subscription.deleted`), so the ledger stays true;
* has dunning events acknowledged, audited with the reason and dropped;
* has every pending approval expired at the moment of suspension;
* is refused by `decide()` and by the mailer on every send.

## Public demo routes

`/payment-failed`, `/payment-failed/batch`, `/config`, `/portfolio-impact` and
`/report/sample` belong to the `demo` tenant explicitly, are unavailable on any other,
and are the only paths that read `customers.json`. A tenant event carrying
`metadata.paypilot_customer_id=cust_001` must not inherit a demo customer's name or
address.

## Lifecycle

`scripts/tenants.py`: `bootstrap`, `create`, `show`, `suspend` (reason required),
`resume`, `token` (create or revoke), `config`, `offboard`. Creating a tenant prints
its webhook URL and its first admin token once; the token is not recoverable.
Offboarding exports what the tenant is owed, then erases its ledger rows and revokes
its tokens, irreversibly, with an explicit flag and the per-table counts printed.

## Mailbox modes under tenancy

Gmail and IMAP credentials are per-tenant secrets and the clients are cached by
tenant. The background sweep iterates tenants and reconciles each in that tenant's own
mode; a gmail-mode tenant on an email-mode deployment is reconciled.

## Known small leaks, recorded not fixed

* Sequential `approvals.id` shows an approver the rate at which approvals are created
  across all tenants. Not addressed in this pass.
* Per-tenant LLM budget, metrics and request limits.

## Not in this pass

Postgres with RLS (M7). OIDC for humans (the spine names it; `Principal` is the seam).
A tenant-facing UI. Per-tenant quotas.

## Milestones

| | Done when | State |
|---|---|---|
| M1 | design reviewed; every finding answered or recorded | v2 written, answers all 15 |
| M2 | `Database`/tenant `Store`, schema, explicit migration with whole-table hash, early-paid table, isolation tests incl. the mutation, ambient store removed in multi mode | code done 2026-10-27, not deployed |
| M3 | `TenantConfig`, encrypted secrets, import-time reads removed, per-call Stripe key, demo data bound to `demo`, mailbox creds per tenant | code done 2026-10-27; tenant settings read through TenantConfig; the webhook and admin reads deferred to M4/M5 are pinned by tests (docs/tenant-config.md) |
| M4 | principals, `authorize()`, token table, bootstrap, role containment, route protection, coverage tests | code done 2026-10-27, not deployed: `app/principals.py`, `app/access.py`, `app/admin_api.py`, `tests/test_m4_access.py` |
| M5 | per-tenant webhooks, uniform failure, tenant-keyed idempotency, suspension semantics | code done 2026-10-27, not deployed: `tests/test_m5_webhooks.py` |
| M6 | lifecycle CLI, SOP rewritten, per-tenant documentation generated | code done 2026-10-27, not deployed: `scripts/tenants.py`, `docs/merchant-onboarding.md`, `tests/test_m6_cli.py` |
| M7 | Postgres backend with forced RLS behind `Store` | NOT in this pass |
