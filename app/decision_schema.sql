-- Postgres schema for the decision audit (the Lambda slice, on RDS).
-- Applied by app/lambda_handler.py on each cold start; every statement is
-- idempotent. Column names and order must match SQLITE_SCHEMA in
-- app/decision_audit.py - tests/test_decision.py asserts they do.
CREATE TABLE IF NOT EXISTS decision_audit (
    id          TEXT PRIMARY KEY,
    invoice_id  TEXT NOT NULL,
    rule_fired  TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    input       TEXT NOT NULL,
    decision    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_audit_invoice
    ON decision_audit (invoice_id, decided_at);
