"""The architecture document's current-state claims, asserted against the code.

A diagram is written once, at the moment of most understanding, and the code
then moves underneath it. So every measured figure in section 1 of
`docs/target-architecture.md` is checked here in BOTH directions: the property
is measured from the live code, and the document is required to state the
measured value. If the code moves, the row reds. If somebody edits the figure
in the document, the row reds too.

That second direction is the point. A document that merely accompanies passing
tests still goes stale; one whose own numbers are the assertion cannot, and a
diagram that reads as a measurement has to be one.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile

import pytest

DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "target-architecture.md"


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def api_app(monkeypatch_session=None):
    os.environ.pop("OPENAI_API_KEY", None)
    import app.ingest as ingest_module
    from app.api import app as fastapi_app

    ingest_module._retriever = None
    return fastapi_app


def _states(doc: str, value: object) -> bool:
    return str(value) in doc


# --- the document exists in the shape that was asked for -------------------

def test_the_document_carries_all_ten_requested_sections(doc):
    """Ten sections were specified by name. A missing one is a missing answer."""
    required = [
        "## 1. Current state",
        "## 2. Problems with the current state",
        "## 3. Future state",
        "## 4. Service boundaries",
        "## 5. API contracts",
        "## 6. Data ownership",
        "## 7. Security and compliance controls",
        "## 8. Migration phases",
        "## 9. Cost and business benefits",
        "## 10. Risks and rollback",
    ]
    missing = [h for h in required if h not in doc]
    assert not missing, missing


def test_the_document_names_its_own_paired_test(doc):
    """A current-state claim with no named checker is prose wearing a number."""
    assert "tests/test_arch_doc_current_state.py" in doc


# --- rails -----------------------------------------------------------------

def test_the_registered_rails_are_what_the_document_says(doc):
    import app.rails as rails

    registered = tuple(rails.rails())
    assert registered == ("ach", "bacs", "sepa"), registered
    for name in registered:
        assert f"`{name}`" in doc or name in doc


def test_the_sepa_code_count_is_what_the_document_says(doc):
    import app.sepa_return_map as sepa

    import re

    assert len(sepa.CODES) == 25, len(sepa.CODES)
    assert sorted(sepa.SCOPES) == ["b2b", "both", "core"]
    # EVERY count in the document, not the first one found. The figure appears
    # twice, in the diagram and in the bullet beneath it, so an assertion that
    # the measured value is present somewhere is satisfied by whichever copy
    # was not edited.
    stated = re.findall(r"(\d+) codes", doc)
    assert stated, "the document states no code count"
    assert set(stated) == {str(len(sepa.CODES))}, stated


def test_bacs_ships_a_table_rather_than_the_refusal_the_plan_predicted(doc):
    """The plan said this module refuses at import. Measurement refuted that.

    The four readable sources agree exactly, so a table with provenance is the
    honest artefact and the document says so rather than repeating the plan.
    """
    import app.bacs_return_map as bacs

    assert sorted(bacs.KNOWN_REPORTS) == ["ADDACS", "ARUDD", "AUDDIS"]
    assert len(bacs.COLLIDING_CODES) == 4, len(bacs.COLLIDING_CODES)
    assert _states(doc, "4 symbols")
    assert bacs.PROVENANCE, "a relayed table must carry its provenance"


def test_the_rail_lookup_requires_the_report_type(doc):
    """A default report type is the silent wrong answer, so there is none."""
    import app.bacs_return_map as bacs

    with pytest.raises(TypeError):
        bacs.lookup_return_code("0")  # type: ignore[call-arg]
    assert "required argument" in doc


# --- the decision store ----------------------------------------------------

@pytest.fixture(scope="module")
def sqlite_columns() -> dict[str, list[str]]:
    from app.decision_audit import SqliteDecisionAudit

    path = os.path.join(tempfile.mkdtemp(), "arch.db")
    SqliteDecisionAudit(path)
    con = sqlite3.connect(path)
    try:
        names = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")]
        return {t: [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
                for t in names}
    finally:
        con.close()


def test_the_decision_audit_columns_are_what_the_document_says(doc, sqlite_columns):
    assert sqlite_columns["decision_audit"] == [
        "id", "invoice_id", "client_id", "rule_fired", "decided_at",
        "input", "decision",
    ], sqlite_columns["decision_audit"]
    for col in sqlite_columns["decision_audit"]:
        assert col in doc, col


def test_the_outcome_landed_as_its_own_table_not_as_three_columns(doc, sqlite_columns):
    """The plan said add three columns to decision_audit. It shipped as a table.

    The document states the shipped shape, because a diagram describing the
    plan rather than the code is the thing this test exists to prevent.
    """
    assert "decision_outcomes" in sqlite_columns
    assert sqlite_columns["decision_outcomes"] == [
        "id", "invoice_id", "client_id", "outcome", "reported_at",
        "report_key", "source",
    ], sqlite_columns["decision_outcomes"]
    assert "decision_outcomes" in doc
    assert "rather than as three columns" in doc


def test_the_outcome_is_three_valued_and_unknown_is_not_not_paid(doc):
    import app.decision_audit as da

    assert da.OUTCOME_PAID == "paid"
    assert da.OUTCOME_NOT_PAID == "not_paid"
    assert da.OUTCOME_UNKNOWN == "unknown"
    assert "nobody has told us" in doc


# --- the published surface -------------------------------------------------

def test_the_decide_contract_requires_two_fields_not_four(doc):
    """The validation function names four and defaults two of them.

    A field that appears in a validator is not thereby required, and the
    integration surface is the smaller number.
    """
    from app.decision import DecisionInputError, parse_input

    ok = parse_input({"invoice_id": "inv_1", "failure_code": "card_expired"})
    assert ok["attempt"] == 1
    assert ok["prior_failures"] == 0
    with pytest.raises(DecisionInputError):
        parse_input({"invoice_id": "inv_1"})
    with pytest.raises(DecisionInputError):
        parse_input({"failure_code": "card_expired"})
    assert "REQUIRES two of them" in doc


def test_the_published_and_hidden_path_counts_are_what_the_document_says(doc, api_app):
    published = set(api_app.openapi()["paths"])
    every = {
        r.path for r in api_app.routes
        if hasattr(r, "methods")
        and not r.path.startswith(("/openapi", "/docs", "/redoc"))
    }
    hidden = every - published
    assert len(published) == 16, sorted(published)
    assert len(hidden) == 13, sorted(hidden)
    assert _states(doc, "16 published paths") or _states(doc, "16 paths")
    assert _states(doc, "13 paths hidden") or _states(doc, "13 hidden")


def test_the_commercially_load_bearing_routes_are_published(api_app):
    """A route hidden from the schema does not exist commercially."""
    published = set(api_app.openapi()["paths"])
    for path in (
        "/decide",
        "/decisions/{invoice_id}/outcome",
        "/decisions/outcomes/summary",
        "/webhooks/chargebee",
        "/bank/consent/start",
    ):
        assert path in published, path


# --- the bank seam ---------------------------------------------------------

def test_no_method_on_the_bank_protocol_moves_money(doc):
    from app.bank.provider import BankProvider

    methods = sorted(m for m in dir(BankProvider) if not m.startswith("_"))
    assert methods == [
        "accounts", "authorize_url", "exchange_code", "parse_return_event",
        "refresh", "transactions", "verify_webhook",
    ], methods
    for banned in ("pay", "initiate", "transfer", "debit", "charge"):
        assert not any(banned in m for m in methods), banned
    assert "none of them moves money" in doc


# --- the known problems ----------------------------------------------------

def test_the_embedding_model_is_still_unpinned_where_the_document_says(doc):
    """Section 2 names this as a live problem. If it is fixed, this reds.

    A document asserting a defect that has been repaired sends somebody to fix
    a fixed thing, which is the understating direction nobody re-queries.
    """
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "app" / "ingest.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    hits = [i + 1 for i, line in enumerate(lines) if "OpenAIEmbeddings(" in line]
    assert hits, "OpenAIEmbeddings call not found; section 2 is stale"
    line_no = hits[0]
    assert "OpenAIEmbeddings()" in lines[line_no - 1], lines[line_no - 1]
    assert f"app/ingest.py:{line_no}" in doc, (line_no, "document names a stale line")


def test_the_rls_claim_is_still_recorded_as_never_run(doc):
    """The document may not upgrade a NOT RUN to a measurement by itself."""
    claims = (pathlib.Path(__file__).resolve().parents[1]
              / "docs" / "claims.md").read_text(encoding="utf-8")
    assert "has NEVER been executed against a Postgres server" in claims
    # The phrase in the MIGRATION ROW, not anywhere in the document: "NOT RUN"
    # also appears in section 2, so a bare containment check is satisfied by
    # the copy an edit did not touch.
    rows = [l for l in doc.splitlines() if l.startswith("| 5 |")]
    assert len(rows) == 1, rows
    assert "NOT RUN against Postgres" in rows[0], rows[0]


def test_the_schema_emits_enable_and_force_per_table(doc):
    schema = (pathlib.Path(__file__).resolve().parents[1]
              / "app" / "decision_schema.sql").read_text(encoding="utf-8").upper()
    assert schema.count("ENABLE ROW LEVEL SECURITY") == 2, schema.count(
        "ENABLE ROW LEVEL SECURITY")
    assert schema.count("FORCE ROW LEVEL SECURITY") == 2
    assert "FORCE ROW LEVEL SECURITY" in doc


def test_the_metrics_payload_carries_both_populations(doc, api_app):
    from app.api import metrics

    keys = set(metrics().keys())
    assert {"latency_recent", "avg_latency_ms", "inference"} <= keys, sorted(keys)
    assert "latency_recent" in doc
    assert "avg_latency_ms" in doc


def test_no_recovery_percentage_is_claimed_anywhere_in_the_document(doc):
    """No verified recovery figure exists, so the document may not carry one."""
    import re

    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:%|per cent)", doc):
        window = doc[max(0, match.start() - 90):match.end() + 90].lower()
        assert "recover" not in window, match.group(0)
    assert "No recovery percentage is claimed" in doc
