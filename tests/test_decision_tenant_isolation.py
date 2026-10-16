"""A decision recorded under one client must not be readable under another.

THIS IS THE ACCEPTANCE TEST FOR THE WHOLE CHANGE, and it is deliberately not "the column
exists" or "RLS is enabled". Both of those are proxies: a test asserting the column is
present passes while the filter is missing, and a test asserting RLS is on passes against
Postgres and says nothing about SQLite, which has no RLS at all. Assert the thing. Write as
one client, read as another, get nothing.

TWO LAYERS, AND THEY ARE DIFFERENT STRENGTHS, which is stated rather than implied.
SqliteDecisionAudit is tested BEHAVIOURALLY against a real database: rows go in and the
wrong client reads nothing back. PostgresDecisionAudit has no live database in this suite,
so it is tested STRUCTURALLY: the statement it would send must carry the filter and bind
the client id. A structural test cannot prove isolation, only that the clause is there,
and the two stores are asserted separately because a control present in one and absent in
the other is the silent-success family.
"""

import pytest

from app.decision import (
    DEFAULT_CLIENT_ID,
    decide,
    handle_audit_lookup,
    handle_decision_request,
    parse_input,
    resolve_client,
)
from app.decision_audit import PostgresDecisionAudit, SqliteDecisionAudit

A, B = "client-a", "client-b"


def _decision(invoice_id, client_id):
    d = decide(parse_input({"invoice_id": invoice_id, "failure_code": "card_expired"}))
    d["client_id"] = client_id
    return d


# ---------------------------------------------------------------- SQLite, behavioural

def test_sqlite_one_clients_decision_is_invisible_to_another():
    audit = SqliteDecisionAudit()
    audit.record(_decision("INV-1", A))

    mine = audit.for_invoice("INV-1", A)
    assert len(mine) == 1, "the owning client must see its own decision"

    theirs = audit.for_invoice("INV-1", B)
    assert theirs == [], (
        "client B read client A's decision for the same invoice id. The invoice id is "
        "chosen by the caller, so without the filter one client only has to guess another's"
    )


def test_sqlite_same_invoice_id_under_two_clients_stays_separate():
    """The invoice id is caller chosen, so a collision is not hypothetical."""
    audit = SqliteDecisionAudit()
    audit.record(_decision("SHARED-1", A))
    audit.record(_decision("SHARED-1", B))
    audit.record(_decision("SHARED-1", B))

    assert len(audit.for_invoice("SHARED-1", A)) == 1
    assert len(audit.for_invoice("SHARED-1", B)) == 2


def test_sqlite_lookup_through_the_handler_is_filtered_too():
    """The filter must be on the path the route actually uses, not only on the store."""
    audit = SqliteDecisionAudit()
    status, _ = handle_decision_request(
        {"invoice_id": "INV-2", "failure_code": "card_expired"}, audit, A)
    assert status == 200

    status, payload = handle_audit_lookup("INV-2", audit, A)
    assert status == 200 and len(payload["decisions"]) == 1

    status, payload = handle_audit_lookup("INV-2", audit, B)
    assert status == 404, (
        "a lookup by the wrong client must be indistinguishable from an invoice that does "
        "not exist, rather than an empty 200 that confirms it does")


def test_the_recorded_row_carries_the_client_id():
    """Without it on the row there is nothing to filter on, which was the whole gap."""
    audit = SqliteDecisionAudit()
    audit.record(_decision("INV-3", A))
    row = audit._conn.execute(
        "SELECT client_id FROM decision_audit WHERE invoice_id = ?", ("INV-3",)).fetchone()
    assert row[0] == A


# ---------------------------------------------------------------- Postgres, structural

class _SpyConn:
    """A connection that records every statement instead of reaching a database."""

    def __init__(self, calls):
        self.calls = calls

    def run(self, sql, **params):
        self.calls.append((sql, params))
        return []

    def close(self):
        pass


class _Spy(PostgresDecisionAudit):
    """Captures the SQL and the bound parameters instead of reaching a database.

    It substitutes the CONNECTION rather than overriding the run helper, so the
    real transaction wrapper executes: these tests therefore prove the
    client_id predicate AND that the tenant was declared to the policy in the
    same transaction. A spy that patched the helper instead would keep passing
    while the tenant scope was removed, which is the shape of a gate that has
    quietly stopped checking.
    """

    def __init__(self):
        self.calls = []
        self._env = {}
        self._conn = _SpyConn(self.calls)

    def _connect(self):
        raise AssertionError("the spy must never open a real connection")


def _only(spy, prefix):
    """The one captured statement starting with ``prefix``, with its params."""
    hits = [c for c in spy.calls if c[0].startswith(prefix)]
    assert len(hits) == 1, f"expected exactly one {prefix!r}, got {len(hits)}"
    return hits[0]


def _assert_tenant_declared(spy, client_id):
    """The policy variable was set, in the same transaction, before the statement.

    Order is asserted rather than mere presence: a set_config after the
    statement would read as compliant and protect nothing.
    """
    kinds = [c[0] for c in spy.calls]
    assert kinds[0] == "BEGIN", f"no transaction opened: {kinds}"
    setter = next(
        i for i, k in enumerate(kinds)
        if k.startswith("SELECT set_config('paypilot.client_id'")
    )
    stmt = next(
        i for i, k in enumerate(kinds)
        if k.startswith(("INSERT", "SELECT id,", "SELECT 1", "SELECT outcome", "SELECT COUNT"))
    )
    assert setter < stmt, f"tenant declared after the statement: {kinds}"
    assert spy.calls[setter][1].get("client_id") == client_id
    assert kinds[-1] == "COMMIT", f"transaction not committed: {kinds}"


def test_postgres_insert_carries_the_client_id():
    spy = _Spy()
    spy.record(_decision("INV-4", A))
    sql, params = _only(spy, "INSERT INTO decision_audit")
    assert "client_id" in sql, "the INSERT does not mention client_id"
    assert params.get("client_id") == A, "client_id is not bound on the INSERT"
    _assert_tenant_declared(spy, A)


def test_postgres_select_filters_on_the_client_id():
    spy = _Spy()
    spy.for_invoice("INV-4", A)
    sql, params = _only(spy, "SELECT id, rule_fired")
    assert "client_id" in sql, (
        "the SELECT has no client_id clause, so the Lambda path would return another "
        "client's rows while SQLite filtered correctly")
    assert params.get("client_id") == A
    _assert_tenant_declared(spy, A)


@pytest.mark.parametrize("store", ["sqlite", "postgres"])
def test_neither_store_takes_a_lookup_without_a_client(store):
    """A missing client id must refuse rather than default to seeing everything."""
    audit = SqliteDecisionAudit() if store == "sqlite" else _Spy()
    with pytest.raises(TypeError):
        audit.for_invoice("INV-5")


# ---------------------------------------------------------------------------
# resolve_client: where the client id comes from.
#
# These exist because the id decides which rows a caller can read, so a defect
# here is a cross-tenant read with every filter above still in place and still
# correct. The token is the only input: Option B, a client_id in the request
# body, is refused by design, because a caller declaring its own identity is a
# label and not authentication.
# ---------------------------------------------------------------------------

_MAP = '{"tok-acme": "acme", "tok-globex": "globex"}'


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_no_configured_token_is_a_503_and_never_a_client(configured):
    refusal, client_id = resolve_client("Bearer anything", configured)
    assert refusal is not None and refusal[0] == 503
    assert client_id is None, "a 503 must not hand back an id something could then filter on"


@pytest.mark.parametrize("header", [None, "", "tok-acme", "Basic tok-acme", "bearer tok-acme"])
def test_a_missing_or_wrong_scheme_header_is_401(header):
    refusal, client_id = resolve_client(header, _MAP)
    assert refusal is not None and refusal[0] == 401
    assert client_id is None


def test_a_plain_token_resolves_to_the_default_client():
    refusal, client_id = resolve_client("Bearer opaque-token", "opaque-token")
    assert refusal is None
    assert client_id == DEFAULT_CLIENT_ID


def test_a_wrong_plain_token_is_401():
    refusal, client_id = resolve_client("Bearer wrong", "opaque-token")
    assert refusal is not None and refusal[0] == 401
    assert client_id is None


def test_a_token_map_gives_each_token_its_own_client():
    assert resolve_client("Bearer tok-acme", _MAP) == (None, "acme")
    assert resolve_client("Bearer tok-globex", _MAP) == (None, "globex")


def test_an_unknown_token_against_a_map_is_401_and_not_the_first_entry():
    refusal, client_id = resolve_client("Bearer tok-nobody", _MAP)
    assert refusal is not None and refusal[0] == 401
    assert client_id is None, "an unknown token must not fall through to any mapped client"


def test_a_token_that_happens_to_parse_as_json_stays_an_opaque_token():
    """A numeric token parses as a JSON scalar. It is a token, not a map."""
    assert resolve_client("Bearer 12345", "12345") == (None, DEFAULT_CLIENT_ID)
    refusal, _ = resolve_client("Bearer 5", "12345")
    assert refusal is not None and refusal[0] == 401


@pytest.mark.parametrize("configured", [
    '{"tok-acme": ""}',          # blank client id
    '{"tok-acme": 7}',           # non-string client id
    '{}',                        # empty object
    '["tok-acme"]',              # a list, not a map
])
def test_a_malformed_map_is_treated_as_one_opaque_token_rather_than_refused(configured):
    """Refusing here would turn a token that merely looks like JSON into a 503.

    So the whole configured string becomes the token, which means the KEY alone
    does not authenticate. Both halves are asserted: a 503 would be a worse
    failure than a narrow one, and a key that authenticated would be a client
    id nobody configured.
    """
    assert resolve_client(f"Bearer {configured}", configured) == (None, DEFAULT_CLIENT_ID)
    refusal, client_id = resolve_client("Bearer tok-acme", configured)
    assert refusal is not None and refusal[0] == 401
    assert client_id is None


# --- the transaction wrapper itself -----------------------------------------
#
# The policy reads current_setting('paypilot.client_id'), so a statement that
# runs outside _as_client reads nothing and writes nothing. These assert that
# property per method rather than on one happy path, because the method added
# next is the one nobody will remember to wrap.


_SCOPED_METHODS = {
    "record": lambda s: s.record(_decision("INV-9", A)),
    "for_invoice": lambda s: s.for_invoice("INV-9", A),
    "has_decision": lambda s: s.has_decision("INV-9", A),
    "record_outcome": lambda s: s.record_outcome(
        {
            "invoice_id": "INV-9",
            "client_id": A,
            "outcome": "paid",
            "reported_at": "1970-01-01T00:00:00.000+00:00",
            "report_key": "k",
            "source": "test",
        }
    ),
    "outcome_for": lambda s: s.outcome_for("INV-9", A),
    "outcome_counts": lambda s: s.outcome_counts(A),
}


def test_every_tenant_scoped_method_declares_the_tenant_in_its_own_transaction():
    for name, call in _SCOPED_METHODS.items():
        spy = _Spy()
        call(spy)
        kinds = [c[0] for c in spy.calls]
        assert kinds[0] == "BEGIN", f"{name}: no transaction: {kinds}"
        assert kinds[-1] == "COMMIT", f"{name}: not committed: {kinds}"
        setters = [
            c for c in spy.calls
            if c[0].startswith("SELECT set_config('paypilot.client_id'")
        ]
        assert len(setters) == 1, f"{name}: tenant declared {len(setters)} times"
        assert setters[0][1].get("client_id") == A, f"{name}: wrong tenant"


def test_the_tenant_setting_is_transaction_local():
    """is_local = true, measured on the statement rather than on the docstring.

    A session-scoped setting would leak one tenant's id into the next
    invocation on the same warm container, which is worse than no policy: the
    next request would read another client's rows and the policy would report
    as enforced.
    """
    spy = _Spy()
    spy.for_invoice("INV-9", A)
    setter = _only(spy, "SELECT set_config('paypilot.client_id'")[0]
    assert setter.endswith(", true)"), setter


def test_a_failure_inside_the_transaction_rolls_back_and_re_raises():
    class _Boom(_SpyConn):
        def run(self, sql, **params):
            super().run(sql, **params)
            if sql.startswith("SELECT id, rule_fired"):
                raise RuntimeError("statement failed")
            return []

    spy = _Spy()
    spy._conn = _Boom(spy.calls)
    with pytest.raises(RuntimeError, match="statement failed"):
        spy.for_invoice("INV-9", A)
    kinds = [c[0] for c in spy.calls]
    assert "ROLLBACK" in kinds, f"no rollback: {kinds}"
    assert "COMMIT" not in kinds, f"committed a failed transaction: {kinds}"


def test_a_dead_connection_on_rollback_does_not_mask_the_original_error():
    """The statement's error is the finding; the rollback's is noise.

    A connection that has gone away fails the ROLLBACK too, and re-raising that
    would report a transport fault where the real cause was the statement.
    """
    class _DeadOnRollback(_SpyConn):
        def run(self, sql, **params):
            super().run(sql, **params)
            if sql.startswith("SELECT id, rule_fired"):
                raise RuntimeError("the real cause")
            if sql == "ROLLBACK":
                raise OSError("connection already gone")
            return []

    spy = _Spy()
    spy._conn = _DeadOnRollback(spy.calls)
    with pytest.raises(RuntimeError, match="the real cause"):
        spy.for_invoice("INV-9", A)
    assert spy._conn is None, "a connection that failed its rollback was kept"


def test_the_reconnect_retry_covers_the_begin():
    """A container frozen between invocations now fails on BEGIN, not on the
    statement, so the single retry _run_unscoped had must still be reached."""
    class _StaleOnce(_SpyConn):
        def __init__(self, calls):
            super().__init__(calls)
            self.failed = False

        def run(self, sql, **params):
            if sql == "BEGIN" and not self.failed:
                self.failed = True
                raise OSError("stale connection")
            return super().run(sql, **params)

    spy = _Spy()
    stale = _StaleOnce(spy.calls)
    fresh = _SpyConn(spy.calls)
    spy._conn = stale
    spy._connect = lambda: fresh
    rows = spy.for_invoice("INV-9", A)
    assert rows == []
    assert spy._conn is fresh, "did not reconnect after the stale BEGIN"
    assert [c[0] for c in spy.calls][0] == "BEGIN"


def test_no_tenant_scoped_method_reaches_the_unscoped_runner():
    """Structural, keyed on the SOURCE rather than on a run.

    A method that calls _run_unscoped passes every behavioural test above (it
    talks to the same connection and returns the same rows) and reads nothing
    once the policy is live. The only thing that catches it before deploy is
    reading which runner each method names.
    """
    import inspect

    from app import decision_audit

    src = inspect.getsource(decision_audit.PostgresDecisionAudit)
    for name in _SCOPED_METHODS:
        body = inspect.getsource(getattr(decision_audit.PostgresDecisionAudit, name))
        assert "_run_unscoped" not in body, f"{name} bypasses the tenant scope"
        assert "_as_client" in body, f"{name} does not open a tenant scope"
    # ensure_schema is the one legitimate unscoped caller: DDL has no policy.
    ddl = inspect.getsource(decision_audit.PostgresDecisionAudit.ensure_schema)
    assert "_run_unscoped" in ddl
    # self._run_unscoped( counts CALL SITES; a bare _run_unscoped( also matches
    # the def and would make this assertion pass on its own subject.
    assert src.count("self._run_unscoped(") == 1, "a second unscoped call site appeared"
