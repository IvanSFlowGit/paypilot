"""Tenant isolation in the data layer (docs/multi-tenancy.md, M2).

Two tenants deliberately use the SAME invoice id, event id and approval
request, because a collision is the only case that can show a leak. Every
public Store method is exercised from both sides.

The structural tests read ``app/store.py`` as a syntax tree. A behavioural test
can pass on a query that happens to return the right rows; the structural test
asserts the predicate is written into every statement that touches a tenant
table, which is what keeps it true for the next query someone writes.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from app import store as store_module
from app.store import (
    AmbientTenantError,
    Database,
    Platform,
    Store,
    UnknownInvoice,
    early_paid_key,
    get_store,
    reset_store,
)

ROOT = Path(__file__).resolve().parent.parent
STORE_SOURCE = ROOT / "app" / "store.py"

TENANT_NAMES = ("failures", "events", "messages", "transitions", "approvals", "early_paid")
ALPHA, BRAVO = "tn_alpha", "tn_bravo"
FAR_FUTURE = "9999-01-01T00:00:00+00:00"
LONG_AGO = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """Two tenant views over one database file, sharing a lock as in production.

    Multi mode: one file holding two tenants is the case under test. A single-mode
    file has one owner, and binding a second tenant to it is refused by design."""
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    db = Database(tmp_path / "isolation.db")
    alpha = Store(db, ALPHA)
    bravo = Store(db, BRAVO)
    yield alpha, bravo
    db.close()


def _fail(store, invoice_id="in_1", customer="cus_1", stripe="cus_st_1", sub="sub_1", amount=1000):
    return store.record_failure(
        invoice_id=invoice_id,
        customer_id=customer,
        amount_minor=amount,
        currency="eur",
        failure_code="card_declined",
        stripe_customer_id=stripe,
        subscription_id=sub,
    )


def _approval(store, invoice_id="in_1", expires_at=FAR_FUTURE):
    return store.create_approval(
        invoice_id=invoice_id,
        channel="email",
        recipient="ana@example.test",
        subject="Your payment",
        body="Please update your card.",
        token_hash="hash-" + store.tenant_id,
        expires_at=expires_at,
    )


# --- the same identifiers, two tenants -----------------------------------


def test_the_same_invoice_id_is_two_rows(pair):
    alpha, bravo = pair
    _fail(alpha, amount=100)
    _fail(bravo, amount=200)
    assert alpha.get_failure("in_1")["amount_minor"] == 100
    assert bravo.get_failure("in_1")["amount_minor"] == 200
    assert [r["amount_minor"] for r in alpha.list_failures()] == [100]
    assert [r["amount_minor"] for r in bravo.list_failures()] == [200]
    assert [r["amount_minor"] for r in alpha.list_failures("failed")] == [100]
    assert bravo.list_failures("recovered") == []


def test_a_failure_recorded_in_one_tenant_is_absent_from_the_other(pair):
    alpha, bravo = pair
    _fail(alpha)
    assert bravo.get_failure("in_1") is None
    assert bravo.list_failures() == []


def test_a_repeat_failure_in_one_tenant_does_not_touch_the_other(pair):
    alpha, bravo = pair
    _fail(alpha, amount=100)
    _fail(bravo, amount=200)
    _fail(alpha, amount=999)  # a retry in alpha only
    assert alpha.get_failure("in_1")["attempt_count"] == 1
    assert bravo.get_failure("in_1")["amount_minor"] == 200


def test_open_failures_resolve_per_tenant(pair):
    alpha, bravo = pair
    _fail(alpha, stripe="cus_shared", sub="sub_shared")
    _fail(bravo, stripe="cus_shared", sub="sub_shared")
    assert [r["invoice_id"] for r in alpha.open_failures(stripe_customer_id="cus_shared")] == [
        "in_1"
    ]
    assert alpha.open_failures(subscription_id="sub_shared")[0]["tenant_id"] == ALPHA
    assert bravo.open_failures(subscription_id="sub_shared")[0]["tenant_id"] == BRAVO


def test_transition_in_one_tenant_leaves_the_other_alone(pair):
    alpha, bravo = pair
    _fail(alpha)
    _fail(bravo)
    assert alpha.transition("in_1", "messaged") is True
    assert alpha.get_failure("in_1")["state"] == "messaged"
    assert bravo.get_failure("in_1")["state"] == "failed"


def test_transition_refuses_an_invoice_the_tenant_never_recorded(pair):
    alpha, bravo = pair
    _fail(alpha)
    with pytest.raises(UnknownInvoice):
        bravo.transition("in_1", "messaged")


def test_transitions_and_ever_reached_are_per_tenant(pair):
    alpha, bravo = pair
    _fail(alpha)
    _fail(bravo)
    alpha.transition("in_1", "recovered", recovered_amount_minor=1000)
    assert [t["to_state"] for t in alpha.transitions_for("in_1")] == ["recovered"]
    assert bravo.transitions_for("in_1") == []
    assert alpha.count_ever_reached("recovered") == 1
    assert bravo.count_ever_reached("recovered") == 0


def test_backdate_touches_only_its_tenant(pair):
    alpha, bravo = pair
    _fail(alpha)
    original = _fail(bravo)["failed_at"]
    alpha.backdate("in_1", failed_at=LONG_AGO)
    assert alpha.get_failure("in_1")["failed_at"] == LONG_AGO
    assert bravo.get_failure("in_1")["failed_at"] == original


# --- messages and delivery --------------------------------------------------


def test_messages_are_per_tenant(pair):
    alpha, bravo = pair
    _fail(alpha)
    _fail(bravo)
    alpha.record_message(invoice_id="in_1", status="sent", provider_message_id="re_a")
    assert len(alpha.messages_for("in_1")) == 1
    assert bravo.messages_for("in_1") == []
    assert bravo.sent_message_count("in_1") == 0
    assert alpha.sent_message_count("in_1") == 1


def test_update_message_cannot_reach_another_tenants_row(pair):
    alpha, bravo = pair
    msg_id = alpha.record_message(invoice_id="in_1", status="queued")
    bravo.update_message(msg_id, status="bounced", error="from the wrong tenant")
    assert alpha.messages_for("in_1")[0]["status"] == "queued"
    alpha.update_message(msg_id, status="sent")
    assert alpha.messages_for("in_1")[0]["status"] == "sent"


def test_a_provider_id_is_resolved_only_inside_its_tenant(pair):
    alpha, bravo = pair
    alpha.record_message(invoice_id="in_1", status="sent", provider_message_id="re_42")
    assert alpha.find_message_by_provider_id("re_42")["invoice_id"] == "in_1"
    assert bravo.find_message_by_provider_id("re_42") is None


def test_last_send_at_does_not_cross_tenants(pair):
    alpha, bravo = pair
    _fail(alpha, customer="cus_a", stripe=None, sub=None)
    alpha.record_message(invoice_id="in_1", status="sent")
    assert alpha.last_send_at(invoice_id="in_1") is not None
    assert bravo.last_send_at(invoice_id="in_1") is None


def test_last_send_at_join_is_tenant_scoped_when_customer_ids_collide(pair):
    """Alpha's invoice in_1 was sent to. Bravo holds a failure for the SAME invoice
    id under the Stripe customer that alpha is asked about. Alpha must not see
    bravo's failure row through the join, so alpha's answer is None."""
    alpha, bravo = pair
    _fail(alpha, customer="cus_a", stripe="cus_a_st", sub=None)
    alpha.record_message(invoice_id="in_1", status="sent")
    _fail(bravo, customer="cus_9", stripe="cus_9", sub=None)
    assert alpha.last_send_at(stripe_customer_id="cus_9") is None
    assert bravo.last_send_at(stripe_customer_id="cus_9") is None


def test_sent_message_count_ignores_other_tenants_messages(pair):
    alpha, bravo = pair
    for _ in range(3):
        bravo.record_message(invoice_id="in_1", status="sent")
    assert alpha.sent_message_count("in_1") == 0


# --- approvals ----------------------------------------------------------------


def test_a_pending_approval_in_one_tenant_does_not_block_the_other(pair):
    alpha, bravo = pair
    first = _approval(alpha)
    assert first is not None
    assert _approval(bravo) is not None, "tenant bravo must be able to open its own request"
    assert _approval(alpha) is None, "alpha still has one pending for the invoice"


def test_the_refusal_looks_identical_for_a_fresh_invoice(pair):
    """create_approval answers the same whether another tenant holds a pending request
    for the invoice id or nobody does. Otherwise the answer is an existence oracle."""
    alpha, bravo = pair
    _approval(alpha, invoice_id="in_1")
    fresh = _approval(bravo, invoice_id="in_fresh")
    collided = _approval(bravo, invoice_id="in_1")
    assert fresh is not None and collided is not None
    assert isinstance(fresh, int) and isinstance(collided, int)


def test_get_approval_is_a_404_across_tenants(pair):
    alpha, bravo = pair
    approval_id = _approval(alpha)
    assert bravo.get_approval(approval_id) is None
    assert alpha.get_approval(approval_id)["invoice_id"] == "in_1"


def test_approvals_for_and_list_are_per_tenant(pair):
    alpha, bravo = pair
    alpha_id = _approval(alpha, invoice_id="in_1")
    bravo_id = _approval(bravo, invoice_id="in_1")
    _approval(alpha, invoice_id="in_2")
    assert [r["id"] for r in alpha.approvals_for("in_1")] == [alpha_id]
    assert [r["id"] for r in bravo.approvals_for("in_1")] == [bravo_id]
    assert len(alpha.list_approvals()) == 2
    assert [r["id"] for r in bravo.list_approvals()] == [bravo_id]
    assert [r["id"] for r in bravo.list_approvals(status="pending")] == [bravo_id]
    assert bravo.list_approvals(status="approved") == []


def test_claim_approval_refuses_another_tenants_request(pair):
    alpha, bravo = pair
    approval_id = _approval(alpha)
    assert bravo.claim_approval(approval_id, by="mallory") is False
    assert alpha.get_approval(approval_id)["status"] == "pending"
    assert alpha.claim_approval(approval_id, by="ana") is True
    assert alpha.claim_approval(approval_id, by="ana") is False, "one winner"


def test_resolve_approval_refuses_another_tenants_request(pair):
    alpha, bravo = pair
    approval_id = _approval(alpha)
    assert bravo.resolve_approval(approval_id, "failed", by="mallory") is False
    assert alpha.get_approval(approval_id)["status"] == "pending"
    assert alpha.resolve_approval(approval_id, "failed", by="ana") is True


def test_expire_approvals_sweeps_only_its_own_tenant(pair):
    alpha, bravo = pair
    alpha_id = _approval(alpha, invoice_id="in_a", expires_at=LONG_AGO)
    bravo_id = _approval(bravo, invoice_id="in_b", expires_at=LONG_AGO)
    expired = alpha.expire_approvals(now_iso="2026-01-01T00:00:00+00:00")
    assert [e["invoice_id"] for e in expired] == ["in_a"]
    assert alpha.get_approval(alpha_id)["status"] == "expired"
    assert bravo.get_approval(bravo_id)["status"] == "pending"


def test_set_external_id_and_gone_flag_do_not_reach_across(pair):
    alpha, bravo = pair
    approval_id = _approval(alpha)
    bravo.set_approval_external_id(approval_id, "draft-from-bravo")
    assert alpha.get_approval(approval_id)["external_id"] is None
    bravo.mark_approval_gone(approval_id, True)
    assert alpha.get_approval(approval_id)["gone_since"] is None
    alpha.mark_approval_gone(approval_id, True)
    assert alpha.get_approval(approval_id)["gone_since"] is not None


# --- events, idempotency, early payment ------------------------------------------


def test_the_same_event_id_is_seen_once_per_tenant(pair):
    alpha, bravo = pair
    assert alpha.mark_event_seen("evt_1", "invoice.paid") is True
    assert bravo.mark_event_seen("evt_1", "invoice.paid") is True, "a different tenant's event"
    assert alpha.mark_event_seen("evt_1", "invoice.paid") is False
    assert bravo.mark_event_seen("evt_1", "invoice.paid") is False


def test_forget_event_releases_only_its_tenants_claim(pair):
    alpha, bravo = pair
    alpha.mark_event_seen("evt_1", "invoice.paid")
    bravo.mark_event_seen("evt_1", "invoice.paid")
    bravo.forget_event("evt_1")
    assert alpha.mark_event_seen("evt_1", "invoice.paid") is False
    assert bravo.mark_event_seen("evt_1", "invoice.paid") is True


def test_early_paid_is_per_tenant_and_not_a_forgeable_event(pair):
    alpha, bravo = pair
    assert alpha.mark_event_seen(early_paid_key("in_1"), "invoice.paid.early", "in_1") is True
    assert alpha.was_paid_early("in_1") is True
    assert bravo.was_paid_early("in_1") is False
    # A webhook carrying a crafted id and an ordinary type must not mark anything.
    bravo.mark_event_seen("early-paid:in_2", "invoice.paid")
    assert bravo.was_paid_early("in_2") is False


def test_early_paid_survives_a_restart(tmp_path):
    path = tmp_path / "early.db"
    first = Store(path, ALPHA)
    first.mark_event_seen(early_paid_key("in_1"), "invoice.paid.early", "in_1")
    first.close()
    reopened = Store(path, ALPHA)
    assert reopened.was_paid_early("in_1") is True
    reopened.close()


def test_early_paid_with_no_invoice_is_inert(pair):
    alpha, _ = pair
    assert alpha.was_paid_early("") is False
    assert alpha.mark_early_paid("") is False


# --- GDPR, retention and the other cross-cutting paths --------------------------


def test_erasing_one_tenants_customer_leaves_the_others(pair):
    alpha, bravo = pair
    _fail(alpha, customer="cus_1")
    _fail(bravo, customer="cus_1")
    alpha.record_message(invoice_id="in_1", status="sent")
    bravo.record_message(invoice_id="in_1", status="sent")
    deleted = alpha.erase_customer("cus_1")
    assert deleted["failures"] == 1
    assert deleted["messages"] == 1
    assert alpha.get_failure("in_1") is None
    assert bravo.get_failure("in_1") is not None
    assert len(bravo.messages_for("in_1")) == 1


def test_export_customer_is_scoped_to_its_tenant(pair):
    alpha, bravo = pair
    _fail(alpha, customer="cus_1")
    _fail(bravo, customer="cus_1", amount=5)
    exported = alpha.export_customer("cus_1")
    assert [f["amount_minor"] for f in exported["failures"]] == [1000]
    assert exported["invoice_ids"] == ["in_1"]


def test_erasing_a_customer_unknown_to_a_tenant_deletes_nothing(pair):
    alpha, bravo = pair
    _fail(bravo, customer="cus_only_bravo")
    assert alpha.erase_customer("cus_only_bravo") == {
        "failures": 0, "messages": 0, "transitions": 0, "events": 0, "approvals": 0,
        "early_paid": 0,
    }
    assert bravo.get_failure("in_1") is not None


def test_purge_and_expiry_are_per_tenant(pair):
    alpha, bravo = pair
    for store in (alpha, bravo):
        _fail(store)
        store.transition("in_1", "recovered", recovered_amount_minor=1000)
    assert alpha.expired_invoice_ids(FAR_FUTURE) == ["in_1"]
    deleted = alpha.purge_expired(before_iso=FAR_FUTURE)
    assert deleted["failures"] == 1
    assert alpha.get_failure("in_1") is None
    assert bravo.get_failure("in_1")["state"] == "recovered"


def test_open_invoices_are_never_purged_for_either_tenant(pair):
    alpha, bravo = pair
    _fail(alpha)
    _fail(bravo)
    assert alpha.purge_expired(before_iso=FAR_FUTURE)["failures"] == 0
    assert bravo.get_failure("in_1") is not None


# --- the cross-tenant surface lives on Platform only ----------------------------


def test_platform_resolvers_return_a_tenant_id_and_nothing_else(pair):
    alpha, bravo = pair
    approval_id = _approval(bravo)
    alpha.record_message(invoice_id="in_1", status="sent", provider_message_id="re_a")
    db = Database(alpha._db.path)
    platform = Platform(db)
    try:
        assert platform.approval_tenant(approval_id) == BRAVO
        assert platform.approval_tenant(999_999) is None
        assert platform.message_tenant("re_a") == ALPHA
        assert platform.message_tenant("") is None
        assert platform.tenants_with_pending_approvals() == [BRAVO]
    finally:
        db.close()


def test_platform_registers_and_reads_tenant_status(tmp_path):
    db = Database(tmp_path / "platform.db")
    platform = Platform(db)
    try:
        platform.register_tenant(ALPHA, "alpha", "Alpha Ltd")
        assert platform.tenant_status(ALPHA) == "active"
        assert platform.tenant_status(BRAVO) is None
        with pytest.raises(sqlite3.IntegrityError):
            platform.register_tenant(ALPHA, "alpha-again", "Dup")
    finally:
        db.close()


def test_store_has_no_cross_tenant_methods():
    for name in ("approval_tenant", "message_tenant", "tenants_with_pending_approvals",
                 "register_tenant", "tenant_status"):
        assert not hasattr(Store, name), f"{name} must live on Platform, not Store"


# --- binding and the ambient seam ----------------------------------------------


def test_store_on_a_path_binds_the_default_tenant(tmp_path):
    store = Store(tmp_path / "default.db")
    try:
        assert store.tenant_id == "default"
    finally:
        store.close()


def test_store_refuses_a_malformed_tenant_id(tmp_path):
    db = Database(tmp_path / "bad.db")
    try:
        for bad in ("", "Bad Id", "tn-hyphen", "x" * 65):
            with pytest.raises(ValueError):
                Store(db, bad)
    finally:
        db.close()


def test_get_store_refuses_in_multi_mode(monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    reset_store(None)
    with pytest.raises(AmbientTenantError):
        get_store()


def test_get_store_is_the_default_tenant_in_single_mode(monkeypatch, isolated_store):
    monkeypatch.delenv("PAYPILOT_TENANCY", raising=False)
    assert get_store() is isolated_store
    assert get_store().tenant_id == "default"


def test_an_unknown_tenancy_mode_is_an_error(monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "shared")
    with pytest.raises(ValueError):
        store_module.tenancy_mode()


# --- structural: the predicate is in the text of every statement -----------------

# SQL keywords are matched in any case. A lowercase statement is still a statement:
# a scanner that only reads upper case passes every lowercase DELETE for free.
_DML = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
# `{}` is an interpolated table name. `\b` cannot follow `}` (both are non-word
# characters), so the placeholder needs its own lookahead; otherwise every dynamic
# statement, including the delete paths, is invisible to the scan.
_TOUCHES = re.compile(
    r"\b(FROM|INTO|UPDATE|JOIN)\s+(?:\{\}(?!\w)|(?:" + "|".join(TENANT_NAMES) + r")\b)",
    re.IGNORECASE,
)
_INSERT = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE)
_JOIN = re.compile(r"JOIN\s+(\w+)\s+\w+\s+ON\s+(.*?)(?=WHERE|$)", re.DOTALL | re.IGNORECASE)
_SUBQUERY_START = re.compile(r"\(\s*select\b", re.IGNORECASE)
_TENANT_FROM = re.compile(r"\bFROM\s+(" + "|".join(TENANT_NAMES) + r")\b", re.IGNORECASE)


def _flatten(node: ast.AST) -> str | None:
    """A string literal or f-string as SQL text, with each interpolation as {}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(part.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def _matching_close(text: str, start: int) -> int:
    """Index of the `)` that closes the `(` at ``start``, or ``len(text)`` if none."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _outer(text: str) -> str:
    """The statement with each top-level subquery replaced by `()`.

    Cutting at the first subquery was a bypass: a predicate-free tail after a balanced
    subquery, as in `... AND id IN (SELECT ... WHERE tenant_id = ?) OR 1 = 1`, was
    never seen. Here the whole tail stays, so that OR is still a top-level OR in the
    outer WHERE. Nested subqueries sit inside their parent's span and go with it; each
    subquery is checked on its own by the structural test."""
    pieces = []
    position = 0
    while True:
        found = _SUBQUERY_START.search(text, position)
        if found is None:
            break
        pieces.append(text[position : found.start()])
        pieces.append("()")
        position = min(_matching_close(text, found.start()) + 1, len(text))
    pieces.append(text[position:])
    return "".join(pieces)


def _subqueries(text: str) -> list[str]:
    """The body of each `(SELECT ...)` segment, nested ones included, balanced."""
    return [
        text[found.start() + 1 : _matching_close(text, found.start())]
        for found in _SUBQUERY_START.finditer(text)
    ]


def _subquery_offenders(text: str) -> list[str]:
    """Each subquery naming a tenant table whose OWN statement does not bind the tenant.

    The check uses `_outer(body)`: a nested child's predicate does not count for the
    parent, so a middle subquery with no predicate is caught even when the innermost
    one has one."""
    return [
        body
        for body in _subqueries(text)
        if _TENANT_FROM.search(body) and not _binds_tenant(_outer(body))
    ]


def _without_parentheses(text: str) -> str:
    """Replace every innermost parenthesised group until none remain."""
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\([^()]*\)", "()", text)
    return text


def _binds_tenant(text: str) -> bool:
    """`WHERE tenant_id = ?` must be the FIRST condition, followed by AND, and no OR may
    sit at the top level of the WHERE. Parenthesised groups are ignored for the OR
    check, so `AND (a OR b)` is still bound."""
    # The predicate is the first condition: followed by AND, or the only condition
    # (then the statement ends or ORDER / GROUP / LIMIT follows).
    found = re.search(
        r"\bWHERE\s+(?:\w+\.)?tenant_id\s*=\s*\?\s*(?:AND\b|ORDER\b|GROUP\b|LIMIT\b|$|\))",
        text,
        re.IGNORECASE,
    )
    if found is None:
        return False
    return re.search(r"\bOR\b", _without_parentheses(text[found.start():]), re.IGNORECASE) is None


def _has_predicate(text: str) -> bool:
    """True when the OUTER statement binds the tenant. A predicate that only appears
    inside a subquery does not filter the outer rows; each subquery is checked on its
    own by the structural test."""
    return _binds_tenant(_outer(text))


def test_the_predicate_scanner_rejects_the_known_bypasses():
    """Every way a predicate can appear without filtering the rows it is meant to
    filter. Without these negative cases the scanner could pass a leak."""
    # Only inside a subquery: the outer rows are not filtered.
    assert not _has_predicate(
        "SELECT * FROM failures WHERE id IN (SELECT invoice_id FROM failures WHERE tenant_id = ?)"
    )
    # Lowercase and spaced subquery: the same bypass, written differently.
    assert not _has_predicate(
        "SELECT * FROM failures WHERE id IN ( select invoice_id from failures where tenant_id = ? )"
    )
    # A column compared with itself is always true.
    assert not _has_predicate("SELECT * FROM failures WHERE tenant_id = tenant_id")
    # OR before the tenant condition: rows of any tenant can match.
    assert not _has_predicate(
        "SELECT * FROM failures WHERE invoice_id = ? OR state = ? AND tenant_id = ?"
    )
    # OR at the top level after the tenant condition: AND binds tighter, so this leaks.
    assert not _has_predicate(
        "SELECT * FROM failures WHERE tenant_id = ? AND state = ? OR invoice_id = ?"
    )
    # Legitimate forms stay accepted, including an OR inside parentheses.
    assert _has_predicate("SELECT * FROM failures WHERE tenant_id = ? AND state = ?")
    assert _has_predicate(
        "SELECT * FROM failures WHERE tenant_id = ? AND (customer_id = ? OR stripe_customer_id = ?)"
    )
    assert _has_predicate(
        "DELETE FROM early_paid WHERE tenant_id = ? AND seen_at < ? "
        "AND invoice_id NOT IN (SELECT invoice_id FROM failures WHERE tenant_id = ?)"
    )


def test_a_subquery_naming_a_tenant_table_must_bind_its_own_tenant():
    """The purge's NOT IN subquery is checked on its own, so removing its predicate
    is caught even though the outer DELETE still binds the tenant."""
    unbound = (
        "DELETE FROM early_paid WHERE tenant_id = ? AND seen_at < ? "
        "AND invoice_id NOT IN (SELECT invoice_id FROM failures WHERE state IN (?))"
    )
    assert _has_predicate(unbound), "the outer statement is bound"
    bodies = _subqueries(unbound)
    assert len(bodies) == 1
    assert _TENANT_FROM.search(bodies[0])
    assert not _binds_tenant(bodies[0]), "the subquery names failures with no tenant"


def test_an_or_after_a_balanced_subquery_is_seen():
    """The first version cut the statement at its first subquery, so an OR written
    after a balanced subquery was never inspected. It is a top-level OR."""
    assert not _has_predicate(
        "SELECT * FROM failures WHERE tenant_id = ? AND invoice_id IN "
        "(SELECT invoice_id FROM failures WHERE tenant_id = ?) OR 1 = 1"
    )
    assert _has_predicate(
        "SELECT * FROM failures WHERE tenant_id = ? AND invoice_id IN "
        "(SELECT invoice_id FROM failures WHERE tenant_id = ?)"
    )


def test_a_nested_child_predicate_does_not_bind_its_parent():
    """The innermost subquery has a predicate; the middle one does not. The middle
    one must still be flagged: a child's WHERE is not its parent's."""
    nested = (
        "DELETE FROM early_paid WHERE tenant_id = ? AND invoice_id IN "
        "(SELECT invoice_id FROM failures WHERE id IN "
        "(SELECT invoice_id FROM failures WHERE tenant_id = ?))"
    )
    assert _has_predicate(nested), "the outer DELETE is bound"
    offenders = _subquery_offenders(nested)
    assert len(offenders) == 1, offenders
    assert offenders[0].startswith("SELECT invoice_id FROM failures WHERE id IN ")


def test_the_scanner_reads_lowercase_statements():
    """A scanner that reads only upper case passes every lowercase statement for free."""
    source = (
        "def erase(self):\n"
        "    self._conn.execute('delete from failures where state = ?', (self.state,))\n"
    )
    assert _structural_offenders(source), "a lowercase unbound DELETE was not flagged"
    assert _INSERT.search("insert into failures (invoice_id) values (?)").group(2) == "invoice_id"
    assert _JOIN.search("join failures f on f.id = e.id where 1 = 1") is not None


def _statements(source: str | None = None):
    """Every string constant in the store source, as SQL text (`source` lets a mutant
    be scanned exactly as the real file is)."""
    tree = ast.parse(STORE_SOURCE.read_text(encoding="utf-8") if source is None else source)
    # Platform is the cross-tenant class: its sweeps are unscoped by design, so it
    # is excluded by name rather than by a pattern that could hide a Store leak.
    platform_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Platform":
            platform_nodes.update(id(n) for n in ast.walk(node))
    # An f-string's literal pieces are Constant nodes in their own right. Scanning
    # them as statements would check a fragment, not the statement it belongs to.
    pieces = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            pieces.update(id(part) for part in node.values)
    for node in ast.walk(tree):
        if id(node) in pieces or id(node) in platform_nodes:
            continue
        text = _flatten(node)
        if text is not None:
            yield node.lineno, text


def _tenant_statements(source: str | None = None):
    for lineno, text in _statements(source):
        if _DML.search(text) and _TOUCHES.search(text):
            yield lineno, text


def _structural_offenders(source: str | None = None) -> list:
    """Every tenant statement whose outer WHERE, or any subquery, lacks its predicate.
    INSERTs are checked by their column list, not here: they have no WHERE."""
    offenders = []
    for lineno, text in _tenant_statements(source):
        if re.match(r"\s*INSERT\b", text, re.IGNORECASE):
            continue
        if not _has_predicate(text):
            offenders.append((lineno, "outer", text[:90]))
        for body in _subquery_offenders(text):
            offenders.append((lineno, "subquery", body[:90]))
    return offenders


def test_the_structural_scan_finds_the_statements_it_is_meant_to_check():
    """A scanner that matches nothing passes every check below for free."""
    found = list(_tenant_statements())
    assert len(found) >= 30, f"only {len(found)} tenant statements found; the scan is blind"


def test_every_statement_touching_a_tenant_table_carries_the_predicate():
    offenders = _structural_offenders()
    assert offenders == [], f"statements with no tenant predicate: {offenders}"


def test_the_dynamic_table_statements_are_in_the_scan_and_bound():
    """`FROM {}` statements are the ones a scanner most easily loses: the placeholder
    is not a word, so a boundary after it never matches. The delete paths are two
    of them; both must be found, and both must bind the tenant."""
    dynamic = [text for _, text in _tenant_statements() if "DELETE FROM {}" in text]
    assert len(dynamic) >= 2, f"only {len(dynamic)} dynamic DELETEs found; the scan is blind"
    for text in dynamic:
        assert _has_predicate(text), text[:90]


#: One predicate removed from the real store source, scanned by the structural test.
#: Not in LEAK_MUTANTS: the behavioural probe does not reach the purge, and removing
#: this predicate also changes the parameter count, so it could not run there.
STRUCTURAL_MUTANTS = {
    "purge_expired.subquery": (
        'f"WHERE tenant_id = ? AND state IN ({open_ph}))",',
        'f"WHERE state IN ({open_ph}))",',
    ),
}


@pytest.mark.parametrize("mutant", sorted(STRUCTURAL_MUTANTS))
def test_a_structural_predicate_removed_is_flagged_by_the_scan(mutant):
    old, new = STRUCTURAL_MUTANTS[mutant]
    source = STORE_SOURCE.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"{mutant}: the mutation no longer matches exactly once"
    offenders = _structural_offenders(source.replace(old, new))
    assert any(kind == "subquery" and "state IN" in body for _, kind, body in offenders), (
        f"removing the {mutant} predicate was not flagged: {offenders}"
    )


def test_every_insert_into_a_tenant_table_names_tenant_id():
    checked, offenders = 0, []
    for lineno, text in _statements():
        for match in _INSERT.finditer(text):
            if match.group(1) in TENANT_NAMES:
                checked += 1
                if "tenant_id" not in match.group(2):
                    offenders.append((lineno, match.group(1)))
    assert checked >= 6, f"only {checked} tenant INSERTs found; the scan is blind"
    assert offenders == []


def test_every_join_between_tenant_tables_has_tenant_in_its_on_clause():
    checked, offenders = 0, []
    for lineno, text in _statements():
        for match in _JOIN.finditer(text):
            if match.group(1) in TENANT_NAMES:
                checked += 1
                # Both sides of the join must agree on the tenant, not just mention it.
                same_tenant = r"(?:\w+\.)?tenant_id\s*=\s*(?:\w+\.)?tenant_id"
                if not re.search(same_tenant, match.group(2)):
                    offenders.append((lineno, match.group(2).strip()[:70]))
    assert checked >= 1, "no tenant JOIN found; the scan is blind"
    assert offenders == []


def test_dynamic_table_names_are_drawn_from_the_allowlist():
    """Every {} placeholder after FROM, INTO or UPDATE must be the allowlist guard."""
    tree = ast.parse(STORE_SOURCE.read_text(encoding="utf-8"))
    guarded, unguarded = 0, []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for index, part in enumerate(node.values):
            if not isinstance(part, ast.FormattedValue):
                continue
            prior = node.values[index - 1] if index else None
            prior_text = prior.value if isinstance(prior, ast.Constant) else ""
            if not re.search(r"\b(FROM|INTO|UPDATE)\s+$", prior_text):
                continue
            value = part.value
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id == "_guard_table"):
                guarded += 1
            else:
                unguarded.append(node.lineno)
    assert guarded >= 2
    assert unguarded == []


def test_early_paid_markers_are_exported_erased_and_purged_per_tenant(pair):
    alpha, bravo = pair
    _fail(alpha, customer="cus_1")
    _fail(bravo, customer="cus_1")
    alpha.mark_early_paid("in_1")
    bravo.mark_early_paid("in_1")
    export = alpha.export_customer("cus_1")
    assert [r["tenant_id"] for r in export["early_paid"]] == [ALPHA]

    assert alpha.erase_customer("cus_1")["early_paid"] == 1
    assert alpha.was_paid_early("in_1") is False
    assert bravo.was_paid_early("in_1") is True, "erasing alpha must not touch bravo's marker"

    # Bravo: an open invoice with a marker (must survive), a closed one (purged with
    # its invoice), and an orphan marker with no failure row (purged by seen_at).
    _fail(bravo, invoice_id="in_open", customer="cus_open", stripe=None, sub=None)
    bravo.mark_early_paid("in_open")
    bravo.mark_early_paid("in_orphan")
    _fail(bravo, invoice_id="in_closed", customer="cus_closed", stripe=None, sub=None)
    bravo.transition("in_closed", "recovered", recovered_amount_minor=1000)
    bravo.mark_early_paid("in_closed")
    deleted = bravo.purge_expired(before_iso=FAR_FUTURE)
    assert deleted["early_paid"] == 2, deleted
    assert bravo.was_paid_early("in_open") is True, "an open invoice keeps its marker"
    assert bravo.was_paid_early("in_closed") is False
    assert bravo.was_paid_early("in_orphan") is False


# --- mutation-style: a leaky variant of the store must be caught ----------------

#: Each entry removes ONE tenant predicate (and its bound parameter) from the real
#: source. The text must occur exactly once, so the mutant is exactly that edit.
LEAK_MUTANTS = {
    "get_failure": (
        '"SELECT * FROM failures WHERE tenant_id = ? AND invoice_id = ?",\n'
        "            (self.tenant_id, invoice_id),",
        '"SELECT * FROM failures WHERE invoice_id = ?",\n            (invoice_id,),',
    ),
    "list_failures": (
        '"SELECT * FROM failures WHERE tenant_id = ? ORDER BY failed_at",\n'
        "                (self.tenant_id,),",
        '"SELECT * FROM failures ORDER BY failed_at",\n                (),',
    ),
    "_invoice_ids_for_customer": (
        '"SELECT invoice_id FROM failures WHERE tenant_id = ? "\n'
        '            "AND (customer_id = ? OR stripe_customer_id = ?)",\n'
        "            (self.tenant_id, customer_id, customer_id),",
        '"SELECT invoice_id FROM failures WHERE "\n'
        '            "(customer_id = ? OR stripe_customer_id = ?)",\n'
        "            (customer_id, customer_id),",
    ),
    "export_customer.early_paid": (
        '"SELECT * FROM early_paid WHERE tenant_id = ? AND invoice_id = ?",\n'
        "                    (self.tenant_id, inv),",
        '"SELECT * FROM early_paid WHERE invoice_id = ?",\n                    (inv,),',
    ),
    "was_paid_early": (
        '"SELECT 1 FROM early_paid WHERE tenant_id = ? AND invoice_id = ?",\n'
        "            (self.tenant_id, invoice_id),",
        '"SELECT 1 FROM early_paid WHERE invoice_id = ?",\n            (invoice_id,),',
    ),
    "transitions_for": (
        '"SELECT * FROM transitions WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",\n'
        "            (self.tenant_id, invoice_id),",
        '"SELECT * FROM transitions WHERE invoice_id = ? ORDER BY id",\n            (invoice_id,),',
    ),
    "messages_for": (
        '"SELECT * FROM messages WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",\n'
        "            (self.tenant_id, invoice_id),",
        '"SELECT * FROM messages WHERE invoice_id = ? ORDER BY id",\n            (invoice_id,),',
    ),
    "approvals_for": (
        '"SELECT * FROM approvals WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",\n'
        "            (self.tenant_id, invoice_id),",
        '"SELECT * FROM approvals WHERE invoice_id = ? ORDER BY id",\n            (invoice_id,),',
    ),
}

#: Which probe check each mutant must trip.
LEAK_CHECK = {
    "get_failure": "get_failure",
    "list_failures": "list_failures",
    "_invoice_ids_for_customer": "export_customer.invoice_ids",
    "export_customer.early_paid": "export_customer.early_paid",
    "was_paid_early": "was_paid_early",
    "transitions_for": "transitions_for",
    "messages_for": "messages_for",
    "approvals_for": "approvals_for",
}


def _leaking_reads(module, path) -> list[str]:
    """Reads on which bravo sees alpha's rows, using identifiers both tenants share.

    Alpha holds invoice in_1 and markers for in_1 and in_2. Bravo holds only in_2,
    under the same customer. An isolated store answers bravo with in_2 and nothing
    else on every read. Returns the names of the reads that do not.
    """
    db = module.Database(path)
    alpha = module.Store(db, ALPHA)
    bravo = module.Store(db, BRAVO)
    try:
        alpha.record_failure(invoice_id="in_1", customer_id="cus_1", amount_minor=100,
                             currency="eur", failure_code="x", stripe_customer_id="cus_1")
        bravo.record_failure(invoice_id="in_2", customer_id="cus_1", amount_minor=200,
                             currency="eur", failure_code="x", stripe_customer_id="cus_1")
        alpha.mark_early_paid("in_1")
        alpha.mark_early_paid("in_2")
        alpha.transition("in_1", "messaged")
        alpha.record_message(invoice_id="in_1", status="sent")
        alpha.create_approval(invoice_id="in_1", channel="email", recipient="r",
                              subject="s", body="b", token_hash="h", expires_at=FAR_FUTURE)
        export = bravo.export_customer("cus_1")
        checks = {
            "get_failure": bravo.get_failure("in_1") is None,
            "list_failures": [r["invoice_id"] for r in bravo.list_failures()] == ["in_2"],
            "export_customer.invoice_ids": export["invoice_ids"] == ["in_2"],
            "export_customer.early_paid": export["early_paid"] == [],
            "was_paid_early": bravo.was_paid_early("in_1") is False,
            "transitions_for": bravo.transitions_for("in_1") == [],
            "messages_for": bravo.messages_for("in_1") == [],
            "approvals_for": bravo.approvals_for("in_1") == [],
        }
        return [name for name, isolated in checks.items() if not isolated]
    finally:
        db.close()


def _load_mutant(tmp_path, index: int, source: str):
    import importlib.util

    path = tmp_path / f"store_mutant_{index}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"store_mutant_{index}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_real_store_passes_the_leak_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    assert _leaking_reads(store_module, tmp_path / "real.db") == []


@pytest.mark.parametrize("mutant", sorted(LEAK_MUTANTS))
def test_a_store_with_this_predicate_removed_leaks_and_is_caught(tmp_path, monkeypatch, mutant):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    old, new = LEAK_MUTANTS[mutant]
    source = STORE_SOURCE.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"{mutant}: the mutation no longer matches exactly once"
    module = _load_mutant(tmp_path, sorted(LEAK_MUTANTS).index(mutant), source.replace(old, new))
    leaks = _leaking_reads(module, tmp_path / "mutant.db")
    assert LEAK_CHECK[mutant] in leaks, f"removing the {mutant} predicate was not caught: {leaks}"


# --- fallbacks to the default store on tenant paths ------------------------------

#: `store or get_store()` on a path that already receives a store. The tenant paths in
#: app/loop.py and app/mailer.py now take their config from the store, and the set below
#: is what still falls back. In multi mode get_store() raises, so each entry is a latent
#: ambient-tenant read until it is migrated. The set must only shrink.
KNOWN_DEFERRED = frozenset({
    ("app/loop.py", "handle_payment_failed"),
    ("app/loop.py", "handle_recovery"),
    ("app/loop.py", "handle_churn"),
    ("app/mailer.py", "send_dunning_email"),
    ("app/mailer.py", "mark_bounced"),
    ("app/report.py", "build_report"),
    ("scripts/gdpr_erase.py", "erase_subject"),
    ("scripts/gdpr_export.py", "export_subject"),
    ("scripts/retention_purge.py", "purge"),
})


def _store_or_default_fallbacks() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    files = sorted((ROOT / "app").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                        and any(isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                                and v.func.id == "get_store" for v in node.values)):
                    found.add((path.relative_to(ROOT).as_posix(), fn.name))
    return found


def test_store_or_get_store_fallbacks_are_only_the_known_deferred_set():
    found = _store_or_default_fallbacks()
    assert found - KNOWN_DEFERRED == set(), (
        f"a new store-or-get_store fallback appeared: {sorted(found - KNOWN_DEFERRED)}"
    )
    assert KNOWN_DEFERRED - found == set(), (
        f"a deferred fallback is gone; remove it from KNOWN_DEFERRED: "
        f"{sorted(KNOWN_DEFERRED - found)}"
    )


def test_no_store_method_takes_a_tenant_argument():
    """The tenant is bound at construction. A per-call tenant argument is the leak."""
    tree = ast.parse(STORE_SOURCE.read_text(encoding="utf-8"))
    store_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Store")
    offenders = []
    for method in store_class.body:
        if not isinstance(method, ast.FunctionDef) or method.name == "__init__":
            continue
        names = [a.arg for a in method.args.args + method.args.kwonlyargs]
        if "tenant_id" in names or "tenant" in names:
            offenders.append(method.name)
    assert offenders == []


def test_the_sample_report_builds_in_multi_mode(monkeypatch):
    """The public sample dashboard names its own tenant, so it runs in multi mode,
    where an unnamed Store is refused. Before the fix it raised AmbientTenantError."""
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    from app.report import sample_report

    assert sample_report()["arms"]
