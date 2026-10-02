"""app/static/ach-returns.html must be exactly what the code renders, and be served.

Mirrors tests/test_recharge_table_doc.py. The page is generated from the decision
table so it cannot drift; this is what makes a stale page a red build rather than
something a visitor finds. The route is exercised too, because a correct file that
nothing serves is a file.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import ach_return_map as table
from app.api import app

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_ach_page.py"
_spec = importlib.util.spec_from_file_location("render_ach_page", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_committed_page_matches_the_code():
    assert mod.OUT.read_text(encoding="utf-8") == mod.render(), (
        "app/static/ach-returns.html is stale: run python scripts/render_ach_page.py"
    )


def _tbody(page: str) -> str:
    return page[page.index("<tbody>") : page.index("</tbody>")]


def _prose(page: str) -> str:
    """Body copy only: the stylesheet is stripped, because CSS legitimately
    carries things like 70% and 100% that a naive sweep reads as a rate, and so
    is the list that NAMES what the page refuses to claim, which would otherwise
    make the gate fire on the documentation of its own rule."""
    body = page[page.index("</style>") :]
    start = body.find('<ul class="disclaims">')
    if start != -1:
        body = body[:start] + body[body.index("</ul>", start) :]
    return body


def test_every_listed_code_appears_exactly_once_in_the_table():
    body = _tbody(mod.render())
    for code in table.CODES:
        assert body.count(f"<code>{code}</code>") == 1, code


def test_control_the_tbody_slice_actually_holds_the_table():
    body = _tbody(mod.render())
    assert body.count("<tr>") == len(table.list_decisions()) + 1, (
        "five rows plus the fall-through row; without this the test above could "
        "be scanning an empty slice"
    )


def test_the_page_carries_no_comment_a_visitor_could_read():
    page = mod.render()
    assert "<!--" not in page
    assert "/*" not in page


def test_the_page_states_no_family_the_table_does_not():
    page = mod.render()
    assert list(table.FAMILY_STATED) == ["R11"]
    assert "unauthorized" in page
    for other in ("administrative", "insufficient", "no account"):
        assert f"{other} family" not in page.lower()


def test_the_page_makes_no_rate_limit_or_threshold_CLAIM():
    """A standing ban on the thread this table came from, asserted rather than
    remembered. Scoped to body prose: the page is allowed to SAY it carries no
    rate, which is what the disclaims list does."""
    prose = _prose(mod.render()).lower()
    for banned in ("return rate", "failure rate", "retry limit", "threshold"):
        assert banned not in prose, banned
    numeric = re.findall(r"\d+\s*%|\d+\s*(?:day|retr|attempt)\w*", prose, re.I)
    assert not numeric, f"a number-bearing rate or limit claim: {numeric}"


def test_control_the_prose_slice_is_not_empty():
    prose = _prose(mod.render())
    assert "decision table" in prose.lower()
    assert "<style>" not in prose
    assert "disclaims" not in prose


def test_the_page_keeps_both_halves_of_the_deployment_claim():
    page = mod.render()
    assert "deployed and running" in page
    assert "does not carry client traffic" in page


def test_the_route_serves_it():
    client = TestClient(app)
    response = client.get("/ach-returns")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "what each reason maps to" in response.text


def test_control_the_renderer_produces_a_real_page():
    page = mod.render()
    assert len(page) > 4000, "the assertions above would be checking nothing"
    assert page.startswith("<!DOCTYPE html>")


# --- the markdown doc, second renderer over the same module -----------------


def test_committed_doc_matches_the_code():
    assert mod.DOC.read_text(encoding="utf-8") == mod.render_doc(), (
        "docs/ach-return-decisions.md is stale: run python scripts/render_ach_page.py"
    )


def test_the_doc_and_the_page_cannot_disagree_about_the_rows():
    """Neither holds the table, so this asserts both read the same one."""
    doc = mod.render_doc()
    for row in table.list_decisions():
        codes = ", ".join(f"`{c}`" for c in row.codes)
        assert codes in doc, row.decision_row
        assert row.what_has_to_change_first in doc


def test_every_listed_code_appears_exactly_once_in_the_doc_table():
    doc = mod.render_doc()
    body = doc[doc.index("|---|") : doc.index("## The row worth")]
    for code in table.CODES:
        assert body.count(f"`{code}`") == 1, code


def test_the_doc_names_no_rate_limit_or_threshold_claim():
    doc = mod.render_doc().lower()
    numeric = re.findall(r"\d+\s*%|\d+\s*(?:day|retr|attempt)\w*", doc)
    assert not numeric, numeric
    assert "no failure rate, no retry limit and no threshold" in doc


def test_the_doc_states_the_derivation_rather_than_hiding_it():
    doc = mod.render_doc()
    assert "DERIVED from the decision row" in doc
    assert "refuses to start" in doc


def test_control_the_doc_is_a_real_document():
    doc = mod.render_doc()
    assert len(doc) > 1000
    assert doc.startswith("# Returned ACH debits")
