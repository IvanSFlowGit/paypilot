"""GDPR + SOC2-ready controls: salted hashing, allowlist logging, subject
access/erasure, retention purge, and the append-only queryable audit log.

Every test runs offline against an isolated on-disk DB (``tmp_path``) and a
throwaway roster, so nothing touches the real ledger or ``data/customers.json``.
The load-bearing assertions are the ones that PROVE deletion: after an erasure,
a direct row read AND a fresh export must both find nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app import audit as audit_module
from app import pii as pii_module
from app.audit import AuditEventLog
from app.store import (
    STATE_MESSAGED,
    STATE_RECOVERED,
    Store,
)
from scripts import gdpr_common, gdpr_erase, gdpr_export, retention_purge

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_roster(tmp_path, monkeypatch):
    """Point the roster path at a throwaway file for EVERY test here.

    ``resolve_targets``/``redact_customers_file`` fall back to the real
    ``data/customers.json`` when no path is passed. Without this, a ledger-only
    erase test (customers_path=None) would rewrite the operator's real roster.
    """
    roster = tmp_path / "roster-default.json"
    roster.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(gdpr_common, "CUSTOMERS_PATH", roster)

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "gdpr.db")
    yield s
    s.close()


def _seed_customer(store, customer_id="cust_001", invoice_id="in_001"):
    """A failed invoice that was messaged then recovered, with full child rows."""
    store.record_failure(
        invoice_id=invoice_id,
        customer_id=customer_id,
        amount_minor=149900,
        currency="usd",
        failure_code="card_expired",
        stripe_customer_id="cus_stripe_1",
    )
    store.mark_event_seen("evt_1", "invoice.payment_failed", invoice_id)
    store.record_message(invoice_id=invoice_id, status="sent", provider_message_id="pm_1")
    store.transition(invoice_id, STATE_MESSAGED, reason="dunning_email_sent")
    store.transition(invoice_id, STATE_RECOVERED, reason="invoice.paid",
                     recovered_amount_minor=149900)


def _roster(tmp_path, records):
    path = tmp_path / "customers.json"
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return path


_ACME = {
    "id": "cust_001",
    "name": "Acme Robotics",
    "email": "billing@acmerobotics.io",
    "plan": "Scale",
    "mrr": 1499.0,
    "currency": "usd",
    "payment_history": [{"date": "2026-06-15", "status": "failed", "failure_code": "card_expired"}],
}
_BRIGHT = {
    "id": "cust_002",
    "name": "Brightleaf Studios",
    "email": "accounts@brightleaf.design",
    "plan": "Pro",
}


# ---------------------------------------------------------------------------
# 1. Salted hash_pii
# ---------------------------------------------------------------------------

def test_hash_pii_is_salted_not_bare_sha256(monkeypatch):
    """The digest depends on the salt, so it is not a bare sha256 of the value."""
    import hashlib

    monkeypatch.setenv("PAYPILOT_PII_SALT", "salt-A")
    value = "billing@acmerobotics.io"
    bare = hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert pii_module.hash_pii(value) != bare, "hash must be salted, not bare sha256"
    assert len(pii_module.hash_pii(value)) == 64


def test_hash_pii_changes_with_salt(monkeypatch):
    value = "billing@acmerobotics.io"
    monkeypatch.setenv("PAYPILOT_PII_SALT", "salt-A")
    a = pii_module.hash_pii(value)
    monkeypatch.setenv("PAYPILOT_PII_SALT", "salt-B")
    b = pii_module.hash_pii(value)
    assert a != b, "a different salt must yield a different digest"


def test_hash_pii_stable_within_a_salt(monkeypatch):
    monkeypatch.setenv("PAYPILOT_PII_SALT", "salt-A")
    v = "cust_001"
    assert pii_module.hash_pii(v) == pii_module.hash_pii(v)


def test_hash_pii_unset_salt_warns_once_and_still_hashes(monkeypatch, caplog):
    """Unset salt: documented default is used and a one-time warning is emitted."""
    monkeypatch.delenv("PAYPILOT_PII_SALT", raising=False)
    monkeypatch.setattr(pii_module, "_salt_warned", False)
    import logging

    with caplog.at_level(logging.WARNING, logger="paypilot"):
        first = pii_module.hash_pii("cust_001")
        pii_module.hash_pii("cust_002")
    assert len(first) == 64
    warns = [r for r in caplog.records if "pii_salt_unset" in r.getMessage()]
    assert len(warns) == 1, "the unset-salt warning must fire exactly once, not per call"


# ---------------------------------------------------------------------------
# 2. Allowlist logging (guard what you EXTRACT)
# ---------------------------------------------------------------------------

def test_safe_log_fields_drops_unknown_and_keeps_allowlisted():
    out = pii_module.safe_log_fields(
        {"invoice_id": "in_1", "state": "recovered", "phone": "+15551234567", "secret": "x"}
    )
    assert out == {"invoice_id": "in_1", "state": "recovered"}, "unknown keys must be dropped"


def test_safe_log_fields_hashes_name_and_email(monkeypatch):
    monkeypatch.setenv("PAYPILOT_PII_SALT", "salt-A")
    out = pii_module.safe_log_fields(
        {"name": "Acme Robotics", "email": "billing@acmerobotics.io", "state": "failed"}
    )
    assert "name" not in out and "email" not in out, "raw PII must never pass through"
    assert out["name_sha256"] == pii_module.hash_pii("Acme Robotics")
    assert out["email_sha256"] == pii_module.hash_pii("billing@acmerobotics.io")
    assert out["state"] == "failed"


def test_safe_log_fields_skips_empty_pii():
    out = pii_module.safe_log_fields({"email": "", "name": None, "invoice_id": "in_1"})
    assert out == {"invoice_id": "in_1"}


# ---------------------------------------------------------------------------
# 3. Subject access export
# ---------------------------------------------------------------------------

def test_export_returns_all_ledger_rows(store):
    _seed_customer(store)
    result = gdpr_export.export_subject(customer_id="cust_001", store=store,
                                        customers_path=None)
    ledger = result["ledger"][0]
    assert [f["invoice_id"] for f in ledger["failures"]] == ["in_001"]
    assert len(ledger["messages"]) == 1
    assert len(ledger["transitions"]) == 2       # messaged, recovered
    assert len(ledger["events"]) == 1


def test_export_by_email_resolves_via_roster(store, tmp_path):
    _seed_customer(store)
    path = _roster(tmp_path, [_ACME, _BRIGHT])
    result = gdpr_export.export_subject(
        email="billing@acmerobotics.io", store=store, customers_path=path
    )
    assert result["resolved_customer_ids"] == ["cust_001"]
    assert result["roster_records"][0]["name"] == "Acme Robotics"
    assert result["ledger"][0]["failures"][0]["invoice_id"] == "in_001"


# ---------------------------------------------------------------------------
# 4. Erasure - the load-bearing proof that rows are GONE
# ---------------------------------------------------------------------------

def test_erase_removes_every_ledger_row_and_read_finds_nothing(store):
    """After erase, a DIRECT read of each table AND a fresh export find nothing."""
    _seed_customer(store)
    # Precondition: the data is really there.
    assert store.get_failure("in_001") is not None
    assert store.messages_for("in_001") and store.transitions_for("in_001")

    result = gdpr_erase.erase_subject(customer_id="cust_001", store=store,
                                      customers_path=None)

    # (a) counts report what was removed
    assert result["ledger_deleted"]["failures"] == 1
    assert result["ledger_deleted"]["messages"] == 1
    assert result["ledger_deleted"]["transitions"] == 2
    assert result["ledger_deleted"]["events"] == 1
    # (b) direct reads find nothing
    assert store.get_failure("in_001") is None
    assert store.messages_for("in_001") == []
    assert store.transitions_for("in_001") == []
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE invoice_id = ?", ("in_001",)
    ).fetchone()["n"] == 0
    # (c) a subsequent export holds nothing for the subject
    after = gdpr_export.export_subject(customer_id="cust_001", store=store)
    assert after["ledger"][0]["failures"] == []
    assert after["ledger"][0]["invoice_ids"] == []


def test_erase_matches_on_stripe_customer_id(store):
    """A request naming the Stripe id reaches rows keyed by the local id too."""
    _seed_customer(store)  # customer_id=cust_001, stripe_customer_id=cus_stripe_1
    result = gdpr_erase.erase_subject(customer_id="cus_stripe_1", store=store)
    assert result["ledger_deleted"]["failures"] == 1
    assert store.get_failure("in_001") is None


def test_erase_by_email_removes_roster_record(store, tmp_path):
    _seed_customer(store)
    path = _roster(tmp_path, [_ACME, _BRIGHT])
    result = gdpr_erase.erase_subject(
        email="billing@acmerobotics.io", store=store, customers_path=path
    )
    assert result["roster_removed"] == 1
    remaining = json.loads(path.read_text(encoding="utf-8"))
    assert [r["id"] for r in remaining] == ["cust_002"], "only the subject is removed"
    assert store.get_failure("in_001") is None


def test_erase_dry_run_changes_nothing(store, tmp_path):
    _seed_customer(store)
    path = _roster(tmp_path, [_ACME])
    result = gdpr_erase.erase_subject(
        customer_id="cust_001", store=store, customers_path=path, dry_run=True
    )
    assert result["dry_run"] is True
    assert result["ledger_would_delete"]["failures"] == 1
    # Nothing was actually deleted.
    assert store.get_failure("in_001") is not None
    assert json.loads(path.read_text(encoding="utf-8"))[0]["id"] == "cust_001"


def test_erase_leaves_other_customers_untouched(store):
    _seed_customer(store, customer_id="cust_001", invoice_id="in_001")
    _seed_customer(store, customer_id="cust_002", invoice_id="in_002")
    gdpr_erase.erase_subject(customer_id="cust_001", store=store)
    assert store.get_failure("in_001") is None
    assert store.get_failure("in_002") is not None, "erasure must be scoped to the subject"


# ---------------------------------------------------------------------------
# 5. Retention purge
# ---------------------------------------------------------------------------

def _age_row(store, invoice_id, iso):
    store._conn.execute(
        "UPDATE failures SET updated_at = ? WHERE invoice_id = ?", (iso, invoice_id)
    )
    store._conn.commit()


def test_retention_purge_deletes_old_closed_keeps_recent_and_open(store):
    now = datetime.now(UTC)
    old = (now - timedelta(days=400)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=30)).isoformat(timespec="seconds")

    # Old, closed -> purged.
    _seed_customer(store, customer_id="c_old", invoice_id="in_old")
    _age_row(store, "in_old", old)
    # Recent, closed -> kept (inside window).
    _seed_customer(store, customer_id="c_recent", invoice_id="in_recent")
    _age_row(store, "in_recent", recent)
    # Old but OPEN (failed) -> kept (never purge an open invoice).
    store.record_failure(invoice_id="in_open", customer_id="c_open",
                         amount_minor=5000, currency="usd", failure_code="card_declined")
    _age_row(store, "in_open", old)

    result = retention_purge.purge(months=12, now=now, store=store)
    assert result["deleted"]["failures"] == 1
    assert store.get_failure("in_old") is None, "old closed record must be purged"
    assert store.get_failure("in_recent") is not None, "recent closed record must survive"
    assert store.get_failure("in_open") is not None, "an open invoice is never purged"


def test_retention_purge_child_rows_go_with_the_invoice(store):
    now = datetime.now(UTC)
    _seed_customer(store, customer_id="c_old", invoice_id="in_old")
    _age_row(store, "in_old", (now - timedelta(days=400)).isoformat(timespec="seconds"))
    retention_purge.purge(months=12, now=now, store=store)
    assert store.messages_for("in_old") == []
    assert store.transitions_for("in_old") == []


def test_retention_window_from_env(monkeypatch):
    monkeypatch.setenv("PAYPILOT_RETENTION_MONTHS", "6")
    assert retention_purge.retention_months() == 6
    monkeypatch.delenv("PAYPILOT_RETENTION_MONTHS", raising=False)
    assert retention_purge.retention_months() == retention_purge.DEFAULT_RETENTION_MONTHS
    assert retention_purge.DEFAULT_RETENTION_MONTHS == 12


def test_retention_dry_run_changes_nothing(store):
    now = datetime.now(UTC)
    _seed_customer(store, customer_id="c_old", invoice_id="in_old")
    _age_row(store, "in_old", (now - timedelta(days=400)).isoformat(timespec="seconds"))
    result = retention_purge.purge(months=12, now=now, dry_run=True, store=store)
    assert result["dry_run"] is True
    assert result["failures_would_delete"] == 1
    assert store.get_failure("in_old") is not None, "dry run must not delete"


# ---------------------------------------------------------------------------
# 6. Append-only, queryable audit event log
# ---------------------------------------------------------------------------

def test_audit_log_records_and_queries(tmp_path):
    log = AuditEventLog(tmp_path / "audit.db")
    log.record("webhook_received", detail="in_1", severity="info", invoice_id="in_1")
    log.record("message_sent", detail="in_1", invoice_id="in_1", channel="email")
    log.record("webhook_received", detail="in_2", invoice_id="in_2")

    assert log.count() == 3
    assert log.count(event="webhook_received") == 2
    webhooks = log.query(event="webhook_received")
    assert [e["fields"]["invoice_id"] for e in webhooks] == ["in_1", "in_2"]
    log.close()


def test_audit_log_is_append_only_no_mutators():
    """Immutability at the code level: the class exposes no update or delete."""
    for banned in ("update", "delete", "erase", "purge", "remove", "clear"):
        assert not hasattr(AuditEventLog, banned), f"audit log must not expose {banned}()"


def test_audit_log_never_stores_raw_pii(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_PII_SALT", "salt-A")
    log = AuditEventLog(tmp_path / "audit.db")
    rec = log.record(
        "erasure_request",
        detail="gdpr erasure",
        name="Acme Robotics",
        email="billing@acmerobotics.io",
        customer_id="cust_001",
    )
    raw = tmp_path.joinpath("audit.db").read_bytes()
    assert b"Acme Robotics" not in raw
    assert b"billing@acmerobotics.io" not in raw
    assert rec["fields"]["email_sha256"] == pii_module.hash_pii("billing@acmerobotics.io")
    assert rec["fields"]["customer_id"] == "cust_001"  # non-PII business key kept
    log.close()


def test_audit_log_timestamps_are_ordered(tmp_path):
    log = AuditEventLog(tmp_path / "audit.db")
    a = log.record("config_change")
    b = log.record("config_change")
    assert a["ts"] <= b["ts"]
    rows = log.query(event="config_change")
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows), "ids are monotonic"
    log.close()


def test_env_gated_default_log_off_by_default(monkeypatch):
    monkeypatch.delenv("PAYPILOT_AUDIT_DB_PATH", raising=False)
    audit_module.reset_audit_log(None)
    assert audit_module.get_audit_log() is None, "no DB writes unless explicitly configured"


def test_security_events_land_in_the_queryable_log(tmp_path, monkeypatch):
    """When configured, audit_security_event ALSO appends to the durable log."""
    monkeypatch.setenv("PAYPILOT_AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    audit_module.reset_audit_log(None)
    try:
        audit_module.audit_security_event(
            event="webhook_signature_rejected",
            detail="POST /payment-failed rejected",
            severity="error",
        )
        log = audit_module.get_audit_log()
        rows = log.query(event="webhook_signature_rejected")
        assert len(rows) == 1
        assert rows[0]["severity"] == "error"
    finally:
        audit_module.reset_audit_log(None)


def test_erasure_request_is_audited(tmp_path, store, monkeypatch):
    """A real erasure records an erasure_request event carrying only a hash."""
    monkeypatch.setenv("PAYPILOT_AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setenv("PAYPILOT_PII_SALT", "salt-A")
    audit_module.reset_audit_log(None)
    try:
        _seed_customer(store)
        gdpr_erase.erase_subject(customer_id="cust_001", store=store)
        rows = audit_module.get_audit_log().query(event="erasure_request")
        assert len(rows) == 1
        assert pii_module.hash_pii("cust_001") in rows[0]["detail"]
        assert "cust_001" not in rows[0]["detail"].replace(pii_module.hash_pii("cust_001"), "")
    finally:
        audit_module.reset_audit_log(None)
