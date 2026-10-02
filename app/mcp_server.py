# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""An MCP server over the ACH return-code decision table.

An agent asks, the table decides, and the answer carries which rule fired. The
model scores, suggests and drafts; governed logic decides. This is that made
callable: :mod:`app.ach_return_map` holds the rules and this file is a transport
in front of it, so the decision cannot be argued with by anything the agent read
on the way in.

THREE TOOLS AND NO MORE. ``lookup_return_code`` answers one code.
``list_decisions`` makes the five rows legible rather than only queryable, which
is what an agent needs before it can reason about the table at all. ``explain``
says which row fired and why, because a decision that cannot be reconstructed
from an input and a rule is the thing the architecture refuses.

NOTHING WRITES, NOTHING CALLS A MODEL, NOTHING REACHES THE NETWORK. That is
declared per tool as ``read_only_hint`` and ``open_world_hint=False`` rather than
said in a comment, so a client can read it off the tool list and a test can
assert it. A server with no mutations and no egress is one anybody can install
without reading the source, which is the point of publishing it.

The import of :mod:`app.ach_return_map` is deliberately at module top: the table
is parsed and validated when this file loads, so a malformed table refuses at
start up rather than on the first call.

Run it::

    python -m app.mcp_server

Install it in a client by pointing that command at this repository; the README
carries the stanza.
"""

from __future__ import annotations

from dataclasses import asdict

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app import ach_return_map as table

SERVER_NAME = "paypilot-ach-returns"

#: Read only, not destructive, same answer every time, and no world outside this
#: process. All four are true of every tool here and all four are checked by
#: ``tests/test_mcp_server.py``.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

server = MCPServer(
    name=SERVER_NAME,
    instructions=(
        "A deterministic decision table over ACH return codes. Ask it what has "
        "to change before a returned debit can exist again. It answers from a "
        "fixed table, never from a model, and an unlisted code falls through to "
        "the conservative row rather than being guessed at. When the answer "
        "carries matched=false, nothing in the table matched the code."
    ),
)


@server.tool(annotations=READ_ONLY)
def lookup_return_code(code: str) -> dict:
    """Decide one ACH return code.

    Returns the code, its decision row, whether the entry goes again, what has
    to change first, the wording for the customer, and whether a named person
    acts before anything automatic.

    An unknown code is NOT an error and is NOT null. It lands on the
    conservative row and comes back with ``matched`` false and a
    ``fallback_reason`` saying it fell through rather than matched.
    """
    return asdict(table.lookup_return_code(code))


@server.tool(annotations=READ_ONLY)
def list_decisions() -> dict:
    """List the five decision rows and the codes that land on each.

    Seventeen codes, five answers. The row is the decision; the code is a cell
    inside one. ``default_row`` is where anything unlisted lands, and it is
    never row 1.
    """
    return {
        "rows": [asdict(row) for row in table.list_decisions()],
        "codes_listed": list(table.CODES),
        "default_row": table.lookup_return_code("__unlisted__").decision_row,
        "person_required_rows": sorted(table.PERSON_REQUIRED_ROWS),
    }


@server.tool(annotations=READ_ONLY)
def explain(code: str) -> dict:
    """Say which row fired for a code and why.

    ``family`` is populated only where the source table itself states one. The
    published rulebook is sold rather than free, so a family is never asserted
    from memory here; ``family_note`` says which of the two you are looking at.
    """
    return asdict(table.explain(code))


def main() -> None:
    """Serve over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
