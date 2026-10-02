# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Render app/static/ach-returns.html from app/ach_return_map.py.

The page is GENERATED so it cannot drift from the table that decides, which is
the same reason docs/recharge-error-types.md is generated. A test re-renders it
and fails if the committed file differs, so a stale page is a red build rather
than something a visitor finds.

No comments are emitted into the HTML or the CSS. Anything served to a browser is
readable by every visitor, build rationale is internal, and scripts/lint_style.py
fails the build on a comment in a public asset.

    python scripts/render_ach_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import ach_return_map as table  # noqa: E402

OUT = ROOT / "app" / "static" / "ach-returns.html"
DOC = ROOT / "docs" / "ach-return-decisions.md"
URL = "https://paypilot.fly.dev/ach-returns"
REPO = "https://github.com/IvanSFlowGit/paypilot"

STYLE = (
    ":root { --bg:#0b1120; --panel:#131c31; --panel-2:#1a2540; --line:#25324f; "
    "--ink:#e7ecf6; --muted:#9fb0cc; --brand:#4f8cff; --brand-2:#38e1b0; "
    "--radius:14px; --sans:-apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,"
    "Helvetica,Arial,sans-serif; --mono:ui-monospace,SFMono-Regular,Menlo,monospace; }\n"
    "  * { box-sizing:border-box; } html,body { margin:0; padding:0; }\n"
    "  body { font-family:var(--sans); background:radial-gradient(1200px 600px at 70% -10%, "
    "#16213c 0%, var(--bg) 55%); color:var(--ink); line-height:1.6; "
    "-webkit-font-smoothing:antialiased; }\n"
    "  a { color:var(--brand); text-decoration:none; } a:hover { text-decoration:underline; }\n"
    "  .wrap { max-width:900px; margin:0 auto; padding:0 22px 64px; }\n"
    "  header.top { display:flex; align-items:center; justify-content:space-between; padding:20px 0; }\n"
    "  .logo { display:flex; align-items:center; gap:11px; font-weight:700; font-size:18px; }\n"
    "  .logo .mark { width:30px; height:30px; border-radius:9px; "
    "background:linear-gradient(135deg,var(--brand),var(--brand-2)); display:grid; "
    "place-items:center; color:#06122b; font-weight:800; }\n"
    "  .top nav { display:flex; gap:18px; font-size:14px; } .top nav a { color:var(--muted); }\n"
    "  h1 { font-size:clamp(28px,5vw,40px); margin:26px 0 10px; letter-spacing:-.5px; }\n"
    "  h1 .grad { background:linear-gradient(120deg,var(--brand),var(--brand-2)); "
    "-webkit-background-clip:text; background-clip:text; color:transparent; }\n"
    "  .lede { font-size:18px; color:var(--muted); max-width:680px; }\n"
    "  .note { display:inline-block; font-size:13px; color:var(--brand-2); "
    "background:rgba(56,225,176,.09); border:1px solid rgba(56,225,176,.25); "
    "padding:5px 12px; border-radius:999px; font-weight:600; }\n"
    "  .card { background:var(--panel); border:1px solid var(--line); "
    "border-radius:var(--radius); padding:24px; margin:22px 0; }\n"
    "  .card.accent { border-color:rgba(56,225,176,.35); "
    "background:linear-gradient(180deg, rgba(56,225,176,.07), var(--panel)); }\n"
    "  h2 { font-size:21px; margin:30px 0 8px; } h3 { font-size:16px; margin:20px 0 6px; }\n"
    "  ul { padding-left:20px; color:var(--muted); } li { margin:7px 0; }\n"
    "  table { width:100%; border-collapse:collapse; margin:16px 0; font-size:14.5px; }\n"
    "  th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); "
    "vertical-align:top; }\n"
    "  th { color:var(--ink); font-size:13px; text-transform:uppercase; letter-spacing:.4px; }\n"
    "  td { color:var(--muted); } td.row-n { color:var(--brand-2); font-weight:700; }\n"
    "  code { font-family:var(--mono); font-size:13px; background:var(--panel-2); "
    "border:1px solid var(--line); border-radius:5px; padding:1px 5px; color:var(--ink); }\n"
    "  pre { background:var(--panel-2); border:1px solid var(--line); border-radius:10px; "
    "padding:14px 16px; overflow-x:auto; } pre code { background:none; border:none; padding:0; }\n"
    "  .btn { display:inline-block; font-weight:600; font-size:15px; padding:12px 20px; "
    "border-radius:10px; background:linear-gradient(120deg,var(--brand),#3f7bff); "
    "color:#fff; margin-top:8px; }\n"
    "  .btn:hover { filter:brightness(1.08); text-decoration:none; }\n"
    "  ul.disclaims li { color:var(--muted); }\n  footer { border-top:1px solid var(--line); margin-top:36px; padding-top:20px; "
    "color:var(--muted); font-size:13.5px; }\n"
)


def _rows_html() -> str:
    out = []
    for row in table.list_decisions():
        codes = " ".join(f"<code>{c}</code>" for c in row.codes)
        out.append(
            f'      <tr><td class="row-n">{row.decision_row}</td>'
            f"<td>{row.goes_again.replace('_', ' ')}</td>"
            f"<td>{row.what_has_to_change_first}</td>"
            f"<td>{'yes' if row.person_required else 'no'}</td>"
            f"<td>{codes}</td></tr>"
        )
    fell = table.lookup_return_code("__unlisted__")
    out.append(
        f'      <tr><td class="row-n">{fell.decision_row}</td>'
        f"<td>{fell.goes_again.replace('_', ' ')}</td>"
        f"<td>{fell.what_has_to_change_first}</td>"
        f"<td>{'yes' if fell.person_required else 'no'}</td>"
        "<td>every other code, which never lands on row 1</td></tr>"
    )
    return "\n".join(out)


def render() -> str:
    listed = len(table.CODES)
    rows = len(table.list_decisions())
    family = ", ".join(f"<code>{c}</code>" for c in table.FAMILY_STATED)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>PayPilot - Returned ACH debits: what each reason maps to</title>
<meta name="description" content="{listed} ACH return codes sort into {rows} decisions, because there are {rows} answers to one question: what has to change before this debit can exist again. A deterministic table, callable by an agent over MCP." />
<link rel="canonical" href="{URL}" />
<meta name="robots" content="index,follow" />
<meta name="theme-color" content="#0b1120" />
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%234f8cff'/%3E%3Ctext x='16' y='22' font-family='Arial' font-size='18' font-weight='bold' fill='%23fff' text-anchor='middle'%3EP%3C/text%3E%3C/svg%3E" />
<style>
  {STYLE}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <a class="logo" href="/" style="color:var(--ink)"><span class="mark">P</span> PayPilot</a>
    <nav>
      <a href="/">Demo</a>
      <a href="/pricing">Pricing</a>
      <a href="/loadtest">Load test</a>
      <a href="{REPO}" target="_blank" rel="noopener">Source</a>
    </nav>
  </header>

  <span class="note">A decision table, not a reference table</span>
  <h1>Returned ACH debits: <span class="grad">what each reason maps to.</span></h1>
  <p class="lede">
    {listed} return codes sort into {rows} decisions, because there are {rows} answers to the
    one question that decides what happens next: what has to change before this
    debit can exist again. Nothing, the account details, their instruction, our
    own entry, or nothing ever will.
  </p>

  <div class="card">
    <table>
      <thead>
      <tr><th>Row</th><th>Goes again</th><th>Change first</th><th>Person first</th><th>Codes</th></tr>
      </thead>
      <tbody>
{_rows_html()}
      </tbody>
    </table>
  </div>

  <p>
    One row per code would be a reference document, and the registrar publishes
    that for free. One row per decision is a rule set, and that is the part a team
    cannot download.
  </p>

  <h2>The row worth the table</h2>
  <div class="card accent">
    <p style="margin:0">
      {family} arrives in the unauthorized family and is still correctable. It is
      the one code where the family and the decision disagree, and the one thing
      here that is hard to know.
    </p>
  </div>

  <h2>The default is visible as a default</h2>
  <p>
    Every code the table does not list lands on row {table.lookup_return_code("__unlisted__").decision_row},
    where nothing automatic happens, and the answer says it fell through rather
    than matched. It is never null and it is never row 1. A caller that cannot
    tell a match from a fallback has been handed a guess dressed as an answer.
  </p>

  <h2>Callable by an agent</h2>
  <p>
    The table ships as an MCP server with three read-only tools:
    <code>lookup_return_code</code>, <code>list_decisions</code> and
    <code>explain</code>. The agent asks, the table decides, and the answer
    carries which rule fired. No model is consulted, so the decision cannot be
    argued with by anything the agent read on the way in.
  </p>
<pre><code>pip install "mcp&gt;=2,&lt;3"
python -m app.mcp_server</code></pre>
  <p>
    Nothing writes, nothing calls a model, and nothing reaches the network. Each
    tool declares that as <code>read_only_hint</code> and
    <code>open_world_hint: false</code>, so a client reads it off the tool list
    rather than taking this page's word for it, and the test suite runs every tool
    again with the socket layer replaced by a function that raises.
  </p>
  <p>
    A green unit suite is not evidence that a client can load a server, so CI
    spawns it over stdio, completes the handshake, and carries a positive control
    ({family} must come back on row 4 in the unauthorized family) and a negative
    one (an unlisted code must come back on the conservative row, marked as a
    fallback) in every run.
  </p>

  <h2>What this page does not claim</h2>
  <ul class="disclaims">
    <li>No return code family is stated except {family}, whose family the source
      table itself records. The published rulebook is sold rather than free, so a
      family recalled from memory would be an unverified claim.</li>
    <li>No failure rate, no retry limit and no threshold appears anywhere here.
      This is a mapping from a code to a decision, and nothing on this page
      measures how often any of it happens.</li>
    <li>No customer data is involved. The table is a decision table over a public
      standard, built from work done by hand with a fintech credit lead.</li>
  </ul>

  <a class="btn" href="{REPO}" target="_blank" rel="noopener">Read the source</a>

  <footer>
    This page is generated from <code>app/ach_return_map.py</code> by
    <code>scripts/render_ach_page.py</code>, and a test fails the build if the two
    disagree. PayPilot is built as a working system rather than a client
    engagement: it is deployed and running, and it does not carry client traffic.
  </footer>
</div>
</body>
</html>
"""


def render_doc() -> str:
    """The same table as markdown, for a reader in the repository.

    A second renderer over ONE module rather than a second source of truth. The
    page and this doc cannot disagree, because neither holds the table.
    """
    rows = table.list_decisions()
    fell = table.lookup_return_code("__unlisted__")
    lines = [
        "# Returned ACH debits: what each reason maps to",
        "",
        "Generated by `scripts/render_ach_page.py` from `app/ach_return_map.py`. "
        "Do not edit by hand.",
        "",
        f"{len(table.CODES)} return codes in {len(rows)} decisions, because there are "
        f"{len(rows)} answers to the one question that decides what happens next: what "
        "has to change before this debit can exist again. Nothing, the account details, "
        "their instruction, our own entry, or nothing ever will.",
        "",
        "Published at <https://paypilot.fly.dev/ach-returns> and callable by an agent "
        "over MCP (`app/mcp_server.py`).",
        "",
        "| Row | Goes again | What has to change first | A person acts first | Codes |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        codes = ", ".join(f"`{c}`" for c in row.codes)
        lines.append(
            f"| {row.decision_row} | {row.goes_again.replace('_', ' ')} | "
            f"{row.what_has_to_change_first} | "
            f"{'yes' if row.person_required else 'no'} | {codes} |"
        )
    lines.append(
        f"| {fell.decision_row} | {fell.goes_again.replace('_', ' ')} | "
        f"{fell.what_has_to_change_first} | "
        f"{'yes' if fell.person_required else 'no'} | "
        "anything unlisted, which never lands on row 1 |"
    )
    family = ", ".join(f"`{c}`" for c in table.FAMILY_STATED)
    lines += [
        "",
        "## The row worth the table",
        "",
        f"{family} arrives in the unauthorized family and is still correctable, so it is "
        "the one code where the family and the decision disagree, and the one thing here "
        "that is hard to know.",
        "",
        "## What this table does not state",
        "",
        f"- No return code family except {family}'s, whose family the source table itself "
        "records. The published rulebook is sold rather than free, so no family is "
        "asserted from memory.",
        "- No failure rate, no retry limit and no threshold. This maps a code to a "
        "decision and measures nothing about how often any of it happens.",
        "- Whether a named person acts first is DERIVED from the decision row, because "
        "the source column for it is empty on every row. The import refuses to start if "
        "that column is ever populated, rather than disagreeing with its own source.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(render())} chars)")
    DOC.write_text(render_doc(), encoding="utf-8")
    print(f"wrote {DOC.relative_to(ROOT)} ({len(render_doc())} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
