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
-- The application sets paypilot.tenant_id with set_config(..., true) inside the same transaction as
-- each statement. Unset, blank or another tenant's value matches no row, so a forgotten set returns
-- nothing and writes nothing. FAIL CLOSED.
--
-- Roles are created by the operator, with passwords, outside this file:
--   CREATE ROLE paypilot_owner NOLOGIN;  CREATE ROLE paypilot_tenant LOGIN PASSWORD '...';
--   CREATE ROLE paypilot_platform LOGIN PASSWORD '...';
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

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['failures','events','messages','transitions','approvals','early_paid']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant_isolation', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I TO paypilot_tenant '
            'USING (tenant_id = nullif(current_setting(''paypilot.tenant_id'', true), '''')) '
            'WITH CHECK (tenant_id = nullif(current_setting(''paypilot.tenant_id'', true), ''''))',
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
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO paypilot_tenant;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO paypilot_platform;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO paypilot_platform;

-- -- cross-tenant lookups -----------------------------------------------------------------------
-- Three questions have to be answered before a tenant is known: which tenant owns this approval id,
-- which tenant sent the message a provider callback names, and which tenants have approvals waiting.
-- They are SECURITY DEFINER functions owned by paypilot_platform, so they see across tenants, return
-- ONLY a tenant id (never a row), and are the only cross-tenant read a tenant session can reach.
-- search_path is pinned so a tenant cannot shadow a table name.

CREATE OR REPLACE FUNCTION approval_tenant(p_id BIGINT) RETURNS TEXT
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $$ SELECT tenant_id FROM approvals WHERE id = p_id $$;

CREATE OR REPLACE FUNCTION message_tenant(p_provider_id TEXT) RETURNS TEXT
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $$ SELECT tenant_id FROM messages WHERE provider_message_id = p_provider_id
          ORDER BY id LIMIT 1 $$;

CREATE OR REPLACE FUNCTION tenants_with_pending_approvals() RETURNS SETOF TEXT
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $$ SELECT DISTINCT tenant_id FROM approvals WHERE status = 'pending' $$;

ALTER FUNCTION approval_tenant(BIGINT) OWNER TO paypilot_platform;
ALTER FUNCTION message_tenant(TEXT) OWNER TO paypilot_platform;
ALTER FUNCTION tenants_with_pending_approvals() OWNER TO paypilot_platform;
REVOKE ALL ON FUNCTION approval_tenant(BIGINT) FROM PUBLIC;
REVOKE ALL ON FUNCTION message_tenant(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenants_with_pending_approvals() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION approval_tenant(BIGINT) TO paypilot_tenant, paypilot_platform;
GRANT EXECUTE ON FUNCTION message_tenant(TEXT) TO paypilot_tenant, paypilot_platform;
GRANT EXECUTE ON FUNCTION tenants_with_pending_approvals() TO paypilot_tenant, paypilot_platform;
