# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""One home for the FAQ, rendered into the three surfaces that carry it.

THE QUESTION SET LIVED IN THREE HAND-MAINTAINED COPIES AND HAD ALREADY DRIFTED.
Measured 2026-10-02 before this script existed: the visible accordion, the FAQPage
JSON-LD and llms.txt each carried six questions, and three of the six answers
disagreed between surfaces. Nothing could have reported that, because agreement
between three hand-kept copies is luck rather than a mechanism.

WHAT IS SHARED AND WHAT IS DELIBERATELY NOT. The question set, its wording and its
ORDER are shared: a question added to one surface and not the others is the drift
this exists to stop. The ANSWER TEXT is per surface on purpose. llms.txt is read
by answer engines and carries a longer form with pointers a human reader does not
need in an accordion, so ``answer_long`` is a feature and not a copy that has gone
stale. Where it is absent the short answer is used for all three.

No comments are emitted into index.html: anything served to a browser is readable
by every visitor and scripts/lint_style.py fails the build on one. The two regions
are found STRUCTURALLY instead, by the FAQPage type inside the JSON-LD block and
by the ``faq`` class on the accordion, which is a positive marker that is ordinary
HTML rather than a comment.

    python scripts/render_faq.py

``tests/test_faq_surfaces.py`` re-renders and fails if any committed surface
differs, so a stale FAQ is a red build rather than something a visitor or a
crawler finds.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INDEX = ROOT / "app" / "static" / "index.html"
LLMS = ROOT / "app" / "static" / "llms.txt"

#: The FAQ. ``answer`` is used for the accordion and the JSON-LD; ``answer_long``
#: overrides it for llms.txt where an answer engine benefits from the pointer.
FAQ: tuple[dict[str, str], ...] = (
    {
        "question": "What is PayPilot?",
        "answer": (
            "PayPilot is an AI dunning agent that recovers failed subscription payments. "
            "It turns a failed-payment event into a grounded recovery action: a diagnosis "
            "of why the charge failed, a churn-risk score, a retry strategy, a scheduled "
            "retry, and a drafted dunning email."
        ),
    },
    {
        "question": "How does PayPilot decide the retry strategy?",
        "answer": (
            "The retry action and cadence come from a deterministic rules table keyed on "
            "the failure code, not the language model, so the policy is stable and "
            "unit-testable. When churn risk is high, the cadence tightens automatically."
        ),
    },
    {
        "question": "Does the live demo need an API key?",
        "answer": (
            "No. With no OpenAI key set, PayPilot runs a deterministic mock mode using "
            "grounded playbook templates and a lexical retriever, so the demo works "
            "offline, free, and with no signup. Adding an API key alone changes nothing: "
            "live drafting also needs PAYPILOT_LLM_DRAFT=1."
        ),
        "answer_long": (
            "No. With no OpenAI key set, PayPilot runs a deterministic mock mode using "
            "grounded playbook templates and a lexical retriever, so the demo works "
            "offline, free, and with no signup. Adding an API key alone changes nothing: "
            "live model drafting also requires PAYPILOT_LLM_DRAFT=1. The default path "
            "renders committed copy and performs no chat inference."
        ),
    },
    {
        "question": "Does PayPilot handle ACH returns as well as card declines?",
        "answer": (
            "The dunning agent handles card and subscription charge failures: its tables "
            "are card decline codes and Recharge error types. ACH is separate. PayPilot "
            "publishes a deterministic decision table for returned ACH debits at "
            "/ach-returns, where 17 return codes sort into 5 decisions, and that table is "
            "not wired into the dunning agent. Saying PayPilot handles ACH would overstate "
            "it; it publishes a decision table for ACH."
        ),
    },
    {
        "question": "What is the ACH return-code decision table?",
        "answer": (
            "Seventeen ACH return codes sorted into five decisions, because there are five "
            "answers to the one question that decides what happens next: what has to "
            "change before the debit can exist again. Nothing, the account details, their "
            "instruction, our own entry, or nothing ever will. Any code the table does not "
            "list falls through to the conservative row, where nothing automatic happens, "
            "and the answer says it fell through rather than matched."
        ),
        "answer_long": (
            "Seventeen ACH return codes sorted into five decisions, because there are five "
            "answers to the one question that decides what happens next: what has to "
            "change before the debit can exist again. Nothing, the account details, their "
            "instruction, our own entry, or nothing ever will. Any code the table does not "
            "list falls through to the conservative row, where nothing automatic happens, "
            "and the answer says it fell through rather than matched. R11 is the row worth "
            "the table: it arrives in the unauthorized family and is still correctable, so "
            "it is the one code where the family and the decision disagree. Published at "
            "https://paypilot.fly.dev/ach-returns and generated from app/ach_return_map.py."
        ),
    },
    {
        "question": "Can an agent call PayPilot over MCP?",
        "answer": (
            "Yes. The ACH decision table ships as an MCP server with three read-only tools: "
            "lookup_return_code, list_decisions and explain. The agent asks, the table "
            "decides, and the answer carries which rule fired, so no model sits in the "
            "decision path. Nothing writes and nothing reaches the network, declared per "
            "tool as read_only_hint and open_world_hint false."
        ),
        "answer_long": (
            "Yes. The ACH decision table ships as an MCP server with three read-only tools: "
            "lookup_return_code, list_decisions and explain. The agent asks, the table "
            "decides, and the answer carries which rule fired, so no model sits in the "
            "decision path. Nothing writes and nothing reaches the network, declared per "
            "tool as read_only_hint and open_world_hint false and asserted by the test "
            "suite. CI spawns the server over stdio and completes a real client handshake, "
            "because a passing unit suite is not evidence that a client can load it. Run it "
            "with pip install \"mcp>=2,<3\" then python -m app.mcp_server."
        ),
    },
    {
        "question": "What technology does PayPilot use?",
        "answer": (
            "A seven-node LangGraph state machine with retrieval-augmented generation (RAG) "
            "over a dunning playbook, served through a FastAPI endpoint. It is written in "
            "Python 3.11 and runs its full test and evaluation suite offline."
        ),
    },
    {
        "question": "How does PayPilot measure recovered revenue?",
        "answer": (
            "Every response quantifies the amount at risk, the recovery likelihood, the "
            "expected recovered value, and the annual revenue at risk if the customer "
            "churns. Recovery likelihood is an illustrative constant, not a measured rate."
        ),
        "answer_long": (
            "Every response quantifies the amount at risk, the recovery likelihood, the "
            "expected recovered value, and the annual revenue at risk if the customer "
            "churns. Recovery likelihood is an illustrative constant per failure code, not "
            "a measured rate; actual recovered revenue is measured separately in the "
            "recovery ledger and reported at /report."
        ),
    },
    {
        "question": "Does PayPilot send emails on its own, and does it work with Gmail or Proton Mail?",
        "answer": (
            "No. By default PayPilot sends nothing and a person approves every message. It writes "
            "the proposed message and either emails the approver a link, or puts it in your own "
            "Drafts folder in Gmail, or in any IMAP mailbox such as Proton Mail through Proton Mail "
            "Bridge, so you read it there and press Send yourself. In those modes it creates, reads "
            "and deletes only its own drafts. Delivery through Resend happens only when sending is "
            "switched on, a person approved that message, and the recipient is on an allowlist."
        ),
        "answer_long": (
            "No. By default PayPilot sends nothing and a person approves every message. It writes "
            "the proposed message and either emails the approver a link, or puts it in your own "
            "Drafts folder in Gmail, or in any IMAP mailbox such as Proton Mail through Proton Mail "
            "Bridge, so you read it there and press Send yourself. In those modes it creates, reads "
            "and deletes only its own drafts, and it never sends from your mailbox. To notice that "
            "you sent a draft it reads the Subject, To and its own marker of recent sent mail to "
            "that one customer; it does not read your inbox or message bodies. Delivery through "
            "Resend happens only when sending is switched on, a person approved that message, and "
            "the recipient is on an allowlist, and every message passes an output check for "
            "unexpected links, secrets and placeholders first. A test fails if the code gains a way "
            "to reach anything not listed in docs/what-paypilot-touches.md."
        ),
    },
    {
        "question": "Is PayPilot GDPR and SOC2-ready?",
        "answer": (
            "Yes, with controls evidenced in code: personal data is masked before the model "
            "and is not stored in the ledger, scripted and tested jobs export and erase a "
            "customer's data end to end, records auto-expire on a configurable retention "
            "window, an append-only audit log records every money and auth event, and "
            "webhooks and admin routes are authenticated. It is also EU AI Act limited-risk "
            "with a configurable AI-assistance disclosure on generated emails. SOC2-ready "
            "means the controls are built and evidenced, not that a certification audit has "
            "been run."
        ),
        "answer_long": (
            "Yes, with controls evidenced in code: PII is masked before the model and never "
            "stored in the ledger, scripted and tested jobs export and erase a customer's "
            "data end to end, records auto-expire on a configurable retention window, an "
            "append-only audit log records every money and auth event, and webhooks and "
            "admin routes are authenticated. It is also EU AI Act limited-risk with a "
            "configurable AI-assistance disclosure on generated emails. See docs/compliance. "
            "SOC2-ready means the controls are built and evidenced, not that a certification "
            "audit has been performed."
        ),
    },
    {
        "question": "Can several clients share one PayPilot deployment?",
        "answer": (
            "Yes, or each client can have their own. Separate deployments, each with its own "
            "database and keys, give the strongest separation and remain available. One "
            "deployment can also serve several clients: every ledger row carries a tenant, "
            "every query is scoped to it, each client has its own webhook address and signing "
            "secret (stored encrypted and never readable back), access is by per-client tokens "
            "with roles, and a suspended client is never mailed. That separation is enforced in "
            "the application and checked by tests. An optional Postgres backend adds database "
            "row-level security, checked against a real Postgres 15, and by default every client "
            "shares one database role there; a per-client database login mode exists whose escape "
            "attempts fail on a real Postgres 15 test cluster, but it has not carried production "
            "traffic. A client who needs the strongest separation is given their own deployment. No production run has yet served more than one client."
        ),
        "answer_long": (
            "Yes, or each client can have their own. Separate deployments, each with its own "
            "database and Stripe keys, give the strongest separation and remain available. One "
            "deployment can also serve several clients: every ledger row carries a tenant, every "
            "query is scoped to it, a process refuses a database owned by another tenant, each "
            "client has its own webhook address and signing secret (stored encrypted and never "
            "readable back), access is by per-client tokens with five roles and one permission "
            "function, a wrong-client caller gets a 404, and a suspended client is never mailed "
            "while its paid customers still have their payments recorded. Lifecycle commands "
            "create, suspend, resume and offboard a client. That separation is enforced in the "
            "application and checked by tests. An optional Postgres backend adds database "
            "row-level security, exercised against a real Postgres 15 but not on a "
            "production database. By default every client shares one database role there, which "
            "stops a forgotten query condition but not injected SQL. A mode that gives each client "
            "its own database login, so the database itself reads the client from who is connected, "
            "has been independently reviewed and its escape attempts fail on a real Postgres 15 test "
            "cluster, but it has not carried production traffic. No "
            "production run has yet served more than one client, so a client who needs the "
            "strongest separation is given their own deployment. See docs/multi-tenancy.md and docs/what-paypilot-touches.md."
        ),
    },
)


def long_answer(entry: dict[str, str]) -> str:
    return entry.get("answer_long", entry["answer"])


def render_jsonld() -> str:
    lines = [
        '<script type="application/ld+json">',
        "{",
        '  "@context": "https://schema.org",',
        '  "@type": "FAQPage",',
        '  "mainEntity": [',
    ]
    for index, entry in enumerate(FAQ):
        name = json.dumps(entry["question"])
        text = json.dumps(entry["answer"])
        comma = "" if index == len(FAQ) - 1 else ","
        lines += [
            "    {",
            '      "@type": "Question",',
            f'      "name": {name},',
            f'      "acceptedAnswer": {{ "@type": "Answer", "text": {text} }}',
            f"    }}{comma}",
        ]
    lines += ["  ]", "}", "</script>"]
    return "\n".join(lines)


def render_accordion() -> str:
    out = ['<div class="faq">']
    for index, entry in enumerate(FAQ):
        tag = "<details open>" if index == 0 else "<details>"
        out += [
            f"      {tag}",
            f"        <summary>{entry['question']}</summary>",
            f"        <p>{entry['answer']}</p>",
            "      </details>",
        ]
    out.append("    </div>")
    return "\n".join(out)


def render_llms_faq() -> str:
    lines = ["## FAQ", ""]
    for entry in FAQ:
        lines.append(f"- {entry['question']} {long_answer(entry)}")
    return "\n".join(lines) + "\n"


#: Each JSON-LD block on its own. A pattern that starts at the first script tag
#: and runs forward to FAQPage cannot bound a nested structure: it swallowed the
#: SoftwareApplication, Organization and WebSite blocks on the first run here,
#: taking the page from four to one. So every block is matched separately and
#: PARSED, and the one to replace is chosen by its parsed type rather than by
#: where the text happens to say FAQPage.
_LD_BLOCK = re.compile(
    r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', re.S
)


def _replace_jsonld(html: str) -> str:
    target = None
    for block in _LD_BLOCK.finditer(html):
        try:
            parsed = json.loads(block.group(1))
        except json.JSONDecodeError:
            continue
        if parsed.get("@type") == "FAQPage":
            if target is not None:
                raise SystemExit("index.html carries two FAQPage blocks; refusing to guess")
            target = block
    if target is None:
        raise SystemExit("could not find a parseable FAQPage JSON-LD block in index.html")
    return html[: target.start()] + render_jsonld() + html[target.end() :]


def _replace_accordion(html: str) -> str:
    start = html.find('<div class="faq">')
    if start == -1:
        raise SystemExit('could not find <div class="faq"> in index.html')
    end = html.find("</section>", start)
    if end == -1:
        raise SystemExit("the faq div is not inside a section")
    closing = html.rfind("</div>", start, end)
    if closing == -1:
        raise SystemExit("could not find the end of the faq div")
    return html[:start] + render_accordion() + html[closing + len("</div>") :]


def _replace_llms(text: str) -> str:
    start = text.find("## FAQ")
    if start == -1:
        raise SystemExit("could not find ## FAQ in llms.txt")
    nxt = text.find("\n## ", start + 1)
    end = len(text) if nxt == -1 else nxt + 1
    return text[:start] + render_llms_faq() + text[end:]


def rendered_index() -> str:
    return _replace_accordion(_replace_jsonld(INDEX.read_text(encoding="utf-8")))


def rendered_llms() -> str:
    return _replace_llms(LLMS.read_text(encoding="utf-8"))


def main() -> int:
    for path, text in ((INDEX, rendered_index()), (LLMS, rendered_llms())):
        before = path.read_text(encoding="utf-8")
        path.write_text(text, encoding="utf-8")
        state = "unchanged" if before == text else "updated"
        print(f"{state}: {path.relative_to(ROOT)} ({len(text)} chars)")
    print(f"{len(FAQ)} questions across 3 surfaces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
