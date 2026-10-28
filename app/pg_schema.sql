-- PayPilot ledger on PostgreSQL, with row-level security that holds even if the application is wrong.
--
-- STATUS: WRITTEN, NOT RUN. The sandbox this was written in cannot start a Postgres server, so no
-- statement below has been executed. scripts/pg_rls_check.py applies this file to a throwaway cluster
-- and tries to read across tenants as the tenant role; until that passes, treat every claim here as
-- unproved.
--
-- Three roles, none of them a superuser and none with BYPASSRLS:
--   paypilot_owner     owns the tables. FORCE ROW LEVEL SECURITY applies to it too.
--   paypilot_tenant    what a tenant-bound Store connects as. Sees only rows whose tenant_id equals
--                      the transaction-local setting paypilot.tenant_id. Cannot touch platform tables.
--   paypilot_platform  what Platform connects as: the tenants, api_tokens, tenancy_meta and
--                      tenant_config tables, and the only role with a policy that spans tenants.
--
-- LIMIT: this stops a forgotten tenant predicate or a bad query. It does not stop code that runs
-- arbitrary SQL as paypilot_tenant, which may call set_config itself (one shared role).
--
-- The application sets paypilot.tenant_id with set_config(..., true) inside the same transaction as
-- each statement. Unset, blank or another tenant's value matches no row, so a forgotten set returns
-- nothing and writes nothing. FAIL CLOSED.
--
-- Roles are created by the operator, with passwords, outside this file:
--   CREATE ROLE paypilot_owner LOGIN PASSWORD '...';  -- apply this file as this role
--   CREATE ROLE paypilot_tenant LOGIN PASSWORD '...';
--   CREATE ROLE paypilot_platform LOGIN PASSWORD '...';
--   GRANT paypilot_platform TO paypilot_owner;  -- ALTER ... OWNER TO needs membership
--   REVOKE CREATE ON SCHEMA public FROM PUBLIC;  -- clusters older than PG15 only
-- PG15+: the owner (and the two login roles) need rights on schema public first:
--   GRANT ALL ON SCHEMA public TO paypilot_owner;
--   GRANT USAGE ON SCHEMA public TO paypilot_tenant, paypilot_platform;
--   GRANT CREATE ON SCHEMA public TO paypilot_platform;   -- only for the apply; REVOKE it after
-- (ALTER FUNCTION ... OWNER TO needs the new owner to hold CREATE on the schema.)
-- This file is re-applied safely: every statement is IF NOT EXISTS or DROP-then-CREATE.

-- -- tenant tables ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS failures (
    tenant_id               TEXT NOT NULL,
    invoice_id              TEXT NOT NULL,
    customer_id             TEXT NOT NULL,
    amount_minor            BIGINT NOT NULL,
    currency                TEXT NOT NULL,
    attempt_count           INTEGER NOT NULL DEFAULT 1,
    failure_code            TEXT NOT NULL,
    state                   TEXT NOT NULL,
    holdout                 INTEGER NOT NULL DEFAULT 0,
    recovered_amount_minor  BIGINT,
    failed_at               TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    recovered_at            TEXT,
    stripe_customer_id      TEXT,
    subscription_id         TEXT,
    PRIMARY KEY (tenant_id, invoice_id)
);

CREATE TABLE IF NOT EXISTS events (
    tenant_id    TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    invoice_id   TEXT,
    received_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id                   BIGSERIAL PRIMARY KEY,
    tenant_id            TEXT NOT NULL,
    invoice_id           TEXT NOT NULL,
    channel              TEXT NOT NULL,
    status               TEXT NOT NULL,
    provider_message_id  TEXT,
    attempt              INTEGER NOT NULL DEFAULT 1,
    error                TEXT,
    created_at           TEXT NOT NULL,
    sent_at              TEXT
);

CREATE TABLE IF NOT EXISTS transitions (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    invoice_id  TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    invoice_id      TEXT NOT NULL,
    channel         TEXT NOT NULL,
    status          TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    body_sha256     TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT,
    error           TEXT,
    external_id     TEXT,
    gone_since      TEXT
);

CREATE TABLE IF NOT EXISTS early_paid (
    tenant_id  TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, invoice_id)
);

CREATE INDEX IF NOT EXISTS idx_failures_state ON failures(tenant_id, state);
CREATE INDEX IF NOT EXISTS idx_failures_customer ON failures(tenant_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_failures_stripe_customer ON failures(tenant_id, stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_failures_subscription ON failures(tenant_id, subscription_id);
CREATE INDEX IF NOT EXISTS idx_messages_invoice ON messages(tenant_id, invoice_id);
CREATE INDEX IF NOT EXISTS idx_messages_provider_id ON messages(provider_message_id);
CREATE INDEX IF NOT EXISTS idx_transitions_invoice ON transitions(tenant_id, invoice_id);
CREATE INDEX IF NOT EXISTS idx_approvals_invoice ON approvals(tenant_id, invoice_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_one_pending
    ON approvals(tenant_id, invoice_id) WHERE status = 'pending';

-- -- platform tables ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenancy_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_config (
    tenant_id      TEXT PRIMARY KEY,
    behaviour_json TEXT NOT NULL,
    secrets_enc    BYTEA,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    token_id       TEXT PRIMARY KEY,
    tenant_id      TEXT,
    role           TEXT NOT NULL,
    secret_hash    TEXT NOT NULL,
    label          TEXT,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT,
    revoked_at     TEXT,
    revoked_reason TEXT
);

CREATE TABLE IF NOT EXISTS tenants (
    id                    TEXT PRIMARY KEY,
    slug                  TEXT NOT NULL UNIQUE,
    name                  TEXT NOT NULL,
    status                TEXT NOT NULL,
    status_reason         TEXT,
    config_json           TEXT,
    secrets_enc           TEXT,
    verified_from_domains TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

-- -- row-level security on tenant tables --------------------------------------------------------
-- Two policies per table, each bound to one role with TO. The tenant policy compares tenant_id with
-- the transaction-local setting; nullif turns "unset" and "blank" into NULL, which equals nothing.
-- The platform policy is USING (true) but only for paypilot_platform, a role the tenant connection
-- never holds, so a tenant session cannot widen its own view by setting a variable.
-- FOR ALL on purpose: a later GRANT of UPDATE or DELETE must not land outside every policy.
-- DROP then CREATE because CREATE POLICY has no IF NOT EXISTS.

-- Which tenant is this session? Two ways, never mixed:
--   * a per-tenant login role named pp_t_<tenant> (M9): the answer is the role name itself, which a
--     session cannot change (SET ROLE needs membership, and these roles are members of nothing), so
--     injected SQL cannot move to another tenant;
--   * the shared paypilot_tenant role (M7): the answer is the transaction-local setting the
--     application makes, which the same session could also make, so this mode stops a forgotten
--     predicate and not injected SQL.
-- Any other role gets NULL, which equals nothing. NULLIF is syntax, like COALESCE, not a function in
-- pg_catalog, so it is written bare; it cannot be shadowed.
CREATE OR REPLACE FUNCTION pp_current_tenant() RETURNS TEXT
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, pg_temp
    AS $f$
        SELECT CASE
            WHEN pg_catalog.left(current_user, 5) OPERATOR(pg_catalog.=) 'pp_t_'
                THEN pg_catalog.substr(current_user::text, 6)
            WHEN current_user::text OPERATOR(pg_catalog.=) 'paypilot_tenant'
                THEN NULLIF(pg_catalog.current_setting('paypilot.tenant_id', true), '')
        END
    $f$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['failures','events','messages','transitions','approvals','early_paid']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant_isolation', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I TO PUBLIC '
            'USING (tenant_id = pp_current_tenant()) WITH CHECK (tenant_id = pp_current_tenant())',
            t || '_tenant_isolation', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_platform_all', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I TO paypilot_platform USING (true) WITH CHECK (true)',
            t || '_platform_all', t);
    END LOOP;
END $$;

-- -- grants -------------------------------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM paypilot_tenant;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    failures, events, messages, transitions, approvals, early_paid TO paypilot_tenant;
-- USAGE is what nextval needs; SELECT would expose platform-wide id volume via last_value.
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO paypilot_tenant;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO paypilot_platform;
-- UPDATE on sequences is for setval() after an M8 copy; the tenant role keeps USAGE, SELECT only.
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO paypilot_platform;

-- -- cross-tenant lookups ----------------------------------------------------------------------
-- The platform role answers "which tenant owns this approval / provider message id / has pending
-- approvals" with plain SELECTs on its own connection (its policy spans tenants). An earlier draft
-- had SECURITY DEFINER functions for this, granted to the tenant role, which nothing called and
-- which handed a compromised tenant session a map of every tenant. They are dropped, not kept.
DROP FUNCTION IF EXISTS approval_tenant(BIGINT);
DROP FUNCTION IF EXISTS message_tenant(TEXT);
DROP FUNCTION IF EXISTS tenants_with_pending_approvals();
