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

class _Spy(PostgresDecisionAudit):
    """Captures the SQL and the bound parameters instead of reaching a database."""

    def __init__(self):
        self.calls = []

    def _run(self, sql, **params):
        self.calls.append((sql, params))
        return []


def test_postgres_insert_carries_the_client_id():
    spy = _Spy()
    spy.record(_decision("INV-4", A))
    sql, params = spy.calls[-1]
    assert "client_id" in sql, "the INSERT does not mention client_id"
    assert params.get("client_id") == A, "client_id is not bound on the INSERT"


def test_postgres_select_filters_on_the_client_id():
    spy = _Spy()
    spy.for_invoice("INV-4", A)
    sql, params = spy.calls[-1]
    assert "client_id" in sql, (
        "the SELECT has no client_id clause, so the Lambda path would return another "
        "client's rows while SQLite filtered correctly")
    assert params.get("client_id") == A


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
