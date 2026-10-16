-- Postgres schema for the decision audit (the Lambda slice, on RDS).
-- Applied by app/decision_bootstrap.py, as the master role, on each Terraform
-- apply; every statement is idempotent. Column names and order must match SQLITE_SCHEMA in
-- app/decision_audit.py - tests/test_decision.py asserts they do.
CREATE TABLE IF NOT EXISTS decision_audit (
    id          TEXT PRIMARY KEY,
    invoice_id  TEXT NOT NULL,
    client_id   TEXT NOT NULL DEFAULT '',
    rule_fired  TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    input       TEXT NOT NULL,
    decision    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_audit_invoice
    ON decision_audit (invoice_id, decided_at);

-- CREATE TABLE IF NOT EXISTS DOES NOT ADD A COLUMN to a table that already exists, so a
-- deployed database would keep running without client_id while a fresh one got it and the
-- suite stayed green. Postgres supports an idempotent ADD COLUMN, so the bootstrap carries
-- the migration rather than a human remembering it. SQLite has no IF NOT EXISTS here and is
-- migrated in SqliteDecisionAudit.__init__ instead.
ALTER TABLE decision_audit ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';

-- The outcome of a decision, appended rather than written back onto the decision.
-- decision_audit has no update method on either backend and that is a guarantee, so
-- closing the loop adds a table instead of a column. Absence of a row means unknown:
-- there is no stored third value, because a row meaning "nobody told us" is
-- indistinguishable from one meaning "we were told it did not pay".
CREATE TABLE IF NOT EXISTS decision_outcomes (
    id          TEXT PRIMARY KEY,
    invoice_id  TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    report_key  TEXT NOT NULL,
    source      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_invoice
    ON decision_outcomes (invoice_id, client_id, reported_at);

-- UNIQUE on (client_id, report_key) and never on report_key alone. The key comes from
-- the caller, so a global unique index would let one client discover another's keys by
-- watching which inserts were ignored. It is also what ON CONFLICT targets.
CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_outcomes_report_key
    ON decision_outcomes (client_id, report_key);

-- Row level security on both tenant tables. DEFENCE IN DEPTH BEHIND THE
-- WHERE client_id PREDICATE THAT ALREADY WORKS, never instead of it: every read
-- on both stores still carries a mandatory client_id filter, and the test that
-- proves this layer works is the one that DROPS that predicate and still cannot
-- read another tenant's row.
--
-- ENABLE alone would already bind paypilot_app, which is not the table owner and
-- is the role the public decision function logs in as, so it is the actual
-- exposure. FORCE is here because an owner bypasses a policy by default, which
-- would make this control a property of which role happened to connect rather
-- than a property of the table. Verbatim from the PostgreSQL 15 documentation on
-- row security policies, read 2026-10-16:
--   "Table owners normally bypass row security as well, though a table owner can
--    choose to be subject to row security with ALTER TABLE ... FORCE ROW LEVEL
--    SECURITY"
--   "Superusers and roles with the BYPASSRLS attribute always bypass the row
--    security system when accessing a table."
--   "If no policy exists for the table, a default-deny policy is used, meaning
--    that no rows are visible or can be modified."
-- So FORCE closes the owner hole and NOTHING here closes the superuser or
-- BYPASSRLS hole. Whether the RDS master role carries BYPASSRLS is unverified
-- and is recorded as such in docs/claims.md rather than claimed away.
--
-- THE COST OF FORCE, WRITTEN WHERE SOMEBODY WILL HIT IT: the master role now
-- reads ZERO ROWS from these tables unless it sets paypilot.client_id first. A
-- backfill run as master would report success having touched nothing, which is
-- the complete-looking derivation from an empty input. DDL is unaffected, which
-- is why the bootstrap still applies this file on every terraform apply.
--
-- The setting is read with missing_ok = true, so an UNSET variable yields NULL;
-- nullif maps the empty string to NULL as well, which is what makes the legacy
-- client_id DEFAULT '' unreachable rather than shared. client_id = NULL is NULL
-- rather than true, so a connection that has not declared a tenant reads nothing
-- and writes nothing. FAIL CLOSED, and it is why the application sets the
-- variable inside the same transaction as the statement.
--
-- FOR ALL rather than per-command on purpose: the app role holds SELECT and
-- INSERT today, and a future GRANT of UPDATE or DELETE would otherwise land
-- outside any policy. DROP then CREATE because CREATE POLICY has no
-- IF NOT EXISTS, and this file is re-applied on every apply.
ALTER TABLE decision_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS decision_audit_client_isolation ON decision_audit;
CREATE POLICY decision_audit_client_isolation ON decision_audit
    USING (client_id = nullif(current_setting('paypilot.client_id', true), ''))
    WITH CHECK (client_id = nullif(current_setting('paypilot.client_id', true), ''));

ALTER TABLE decision_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_outcomes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS decision_outcomes_client_isolation ON decision_outcomes;
CREATE POLICY decision_outcomes_client_isolation ON decision_outcomes
    USING (client_id = nullif(current_setting('paypilot.client_id', true), ''))
    WITH CHECK (client_id = nullif(current_setting('paypilot.client_id', true), ''));
