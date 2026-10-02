"""The MCP server in front of the decision table.

Two layers, because they fail differently with the same green tick. The tool
functions are called directly here, and ``scripts/mcp_witness.py`` drives the
server over stdio through a real client handshake. A unit suite passing is not
evidence that a client can load the server.

Every tool is asserted read only and closed world rather than described as such
in a comment, and the whole tool surface is exercised with the network removed.

The suite skips rather than fails where the ``mcp`` package is absent, so the
decision core stays testable on a machine that has never installed it. The skip
is loud: it names the package.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest

mcp_server = pytest.importorskip(
    "app.mcp_server", reason="the mcp package is not installed in this environment"
)

from app import ach_return_map as table  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_NAMES = {"lookup_return_code", "list_decisions", "explain"}


def _tools():
    return asyncio.run(mcp_server.server.list_tools())


def test_the_server_exposes_exactly_three_tools():
    assert {t.name for t in _tools()} == TOOL_NAMES


def test_every_tool_is_declared_read_only_and_closed_world():
    """The no-mutation, no-egress claim is machine readable, so a client can
    read it off the tool list instead of taking the README's word."""
    for tool in _tools():
        annotations = tool.annotations
        assert annotations is not None, tool.name
        assert annotations.read_only_hint is True, tool.name
        assert annotations.destructive_hint is False, tool.name
        assert annotations.idempotent_hint is True, tool.name
        assert annotations.open_world_hint is False, tool.name


def test_every_tool_describes_itself():
    for tool in _tools():
        assert tool.description and len(tool.description) > 40, tool.name


def test_lookup_matches_the_decision_core():
    for code in table.CODES:
        assert mcp_server.lookup_return_code(code) == {
            **vars(table.lookup_return_code(code))
        }


def test_lookup_of_an_unknown_code_carries_matched_false():
    answer = mcp_server.lookup_return_code("R99")
    assert answer["matched"] is False
    assert answer["decision_row"] != 1
    assert "fell through" in answer["fallback_reason"]


def test_lookup_normalises_case_and_whitespace():
    assert mcp_server.lookup_return_code(" r01 ") == mcp_server.lookup_return_code("R01")


def test_list_decisions_reports_five_rows_and_the_default():
    answer = mcp_server.list_decisions()
    assert len(answer["rows"]) == 5
    assert sorted(answer["codes_listed"]) == sorted(table.CODES)
    assert answer["default_row"] != 1
    assert answer["person_required_rows"] == sorted(table.PERSON_REQUIRED_ROWS)


def test_explain_r11_carries_the_family_through_the_server():
    answer = mcp_server.explain("R11")
    assert answer["family"] == "unauthorized"
    assert answer["decision_row"] == 4
    assert "our own entry" in answer["why"]


def test_explain_reports_no_family_where_the_table_states_none():
    answer = mcp_server.explain("R05")
    assert answer["family"] is None
    assert "not stated in this table" in answer["family_note"]


def test_every_tool_answers_with_the_network_removed(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a tool opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert mcp_server.lookup_return_code("R01")["decision_row"] == 1
    assert len(mcp_server.list_decisions()["rows"]) == 5
    assert mcp_server.explain("R11")["family"] == "unauthorized"


def test_no_tool_can_change_the_table():
    """Nothing writes. Called twice either side of every tool, the table is the
    same object with the same contents."""
    before = {c: dict(r) for c, r in table._TABLE.items()}
    for code in ("R01", "R11", "R99"):
        mcp_server.lookup_return_code(code)
        mcp_server.explain(code)
    mcp_server.list_decisions()
    assert {c: dict(r) for c, r in table._TABLE.items()} == before


# --- the witness reader -----------------------------------------------------
#
# Measured 2026-10-02 against mcp 2.2.0: a tool result comes back with
# ``structured_content`` set to None and the answer in a text block, so the
# witness reads it through its fallback and the structured branch never runs.
# An untested branch in a release gate is the gate's weakest part, so both are
# driven here with stand-ins rather than left to whichever shape the SDK happens
# to send today.


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, structured=None, content=()):
        self.structured_content = structured
        self.content = list(content)


def _witness_payload(result):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from mcp_witness import _payload

    return _payload(result)


def test_witness_reads_a_text_block_answer():
    """The shape mcp 2.2.0 actually sends."""
    assert _witness_payload(_Result(content=[_Block('{"decision_row": 4}')])) == {
        "decision_row": 4
    }


def test_witness_reads_a_structured_answer_wrapped_in_result():
    assert _witness_payload(_Result(structured={"result": {"decision_row": 4}})) == {
        "decision_row": 4
    }


def test_witness_reads_a_bare_structured_answer():
    assert _witness_payload(_Result(structured={"decision_row": 4})) == {"decision_row": 4}


def test_witness_refuses_a_result_it_cannot_read():
    with pytest.raises(AssertionError, match="no readable content"):
        _witness_payload(_Result(content=[_Block("")]))
