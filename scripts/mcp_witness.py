# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Drive the MCP server over stdio as a real client, and refuse on a bad answer.

The unit suite calls the tool functions in this process. That proves the logic
and says NOTHING about whether a client can spawn the server, complete the
handshake and get a parseable answer back; those are different failures with the
same green tick. This spawns ``python -m app.mcp_server`` as a subprocess, does
the initialize exchange, lists the tools and calls them.

It carries its controls in the run rather than firing them once at build time:

* A POSITIVE control, ``R11``, which must come back on row 4 in the unauthorized
  family. If the server answered nothing, or answered a default for everything,
  this is the case that notices.
* A NEGATIVE control, an unlisted code, which must come back on the conservative
  row with ``matched`` false. A server that matched everything would pass the
  positive control alone.
* A SHAPE control: the tool list must be exactly the three tools, each declared
  read only and closed world.

Exit 0 only when all three hold. Any other outcome exits 1 and says which
control failed, so this can gate a release rather than be read by eye.

    .venv/bin/python scripts/mcp_witness.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {"lookup_return_code", "list_decisions", "explain"}


def _payload(result) -> dict:
    """Pull the structured answer out of a tool result, whichever way it came."""
    if getattr(result, "structured_content", None):
        content = result.structured_content
        return content.get("result", content) if isinstance(content, dict) else content
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("the tool returned no readable content")


async def witness() -> list[str]:
    failures: list[str] = []
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(REPO_ROOT)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"handshake: {info.server_info.name} speaking {info.protocol_version}")

            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            print(f"tools: {sorted(names)}")
            if names != EXPECTED_TOOLS:
                failures.append(f"SHAPE: tools are {sorted(names)}")
            for tool in listed.tools:
                a = tool.annotations
                if not a or a.read_only_hint is not True or a.open_world_hint is not False:
                    failures.append(f"SHAPE: {tool.name} is not declared read only and closed world")

            positive = _payload(
                await session.call_tool("lookup_return_code", {"code": "R11"})
            )
            print(f"positive control R11: row {positive.get('decision_row')} "
                  f"matched={positive.get('matched')} "
                  f"change_first={positive.get('what_has_to_change_first')!r}")
            if positive.get("decision_row") != 4 or positive.get("matched") is not True:
                failures.append(f"POSITIVE: R11 came back as {positive}")

            explained = _payload(await session.call_tool("explain", {"code": "R11"}))
            print(f"positive control explain R11: family={explained.get('family')!r}")
            if explained.get("family") != "unauthorized":
                failures.append(f"POSITIVE: R11 family came back as {explained.get('family')!r}")

            negative = _payload(
                await session.call_tool("lookup_return_code", {"code": "R97"})
            )
            print(f"negative control R97: row {negative.get('decision_row')} "
                  f"matched={negative.get('matched')}")
            if negative.get("matched") is not False or negative.get("decision_row") == 1:
                failures.append(f"NEGATIVE: an unlisted code came back as {negative}")

            rows = _payload(await session.call_tool("list_decisions", {}))
            print(f"rows: {len(rows.get('rows', []))}, "
                  f"default row {rows.get('default_row')}")
            if len(rows.get("rows", [])) != 5:
                failures.append(f"SHAPE: list_decisions returned {len(rows.get('rows', []))} rows")

    return failures


def main() -> int:
    failures = asyncio.run(witness())
    if failures:
        print("\n" + "\n".join(failures))
        print(f"\n{len(failures)} control(s) failed.")
        return 1
    print("\nall controls passed: handshake, shape, positive, negative")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
