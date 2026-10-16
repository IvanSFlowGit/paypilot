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
