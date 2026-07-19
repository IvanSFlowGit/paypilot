# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Regenerate the dunning copy library at BUILD time. Draft-first, never live.

This is where inference is allowed to happen: once, by a person, with the
output reviewed before it reaches a customer. The runtime path calls no model
(see ``app/templates.py``), so this script is the only place the copy changes.

It deliberately **never overwrites** ``data/templates/dunning.json``. It writes
``dunning.draft.json`` beside it and stops. Publishing is a human action:

    python scripts/generate_templates.py
    diff data/templates/dunning.json data/templates/dunning.draft.json
    # read every changed line, then:
    mv data/templates/dunning.draft.json data/templates/dunning.json
    pytest    # the artifact gates in tests/test_zero_token.py must pass

An auto-publishing generator would put unreviewed text in front of paying
customers on a schedule, which is the exact failure this architecture exists
to prevent.

Requires OPENAI_API_KEY. Not run in CI, not run at deploy.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_PATH = REPO_ROOT / "data" / "templates" / "dunning.json"
DRAFT_PATH = REPO_ROOT / "data" / "templates" / "dunning.draft.json"

FAILURE_CODES = ("card_expired", "insufficient_funds", "generic_decline")

_RULES = """
Write dunning copy for a friendly SaaS billing team.

Hard requirements, all of them non-negotiable:
- Use the literal slots {name} and {plan}. Never invent a real name.
- Include NO URL or link of any kind. The single sanctioned link is attached
  at send time by the application.
- Warm and helpful, never blaming. Frame it as "let's fix this together".
- Reassure the customer their service stays on for now, and invite a reply.
- Plain text. End with the literal slot {business} on its own line as the
  sign-off. NEVER name PayPilot: the recipient is the client's customer and
  has never heard of the tool.
- Use a plain hyphen, never an em dash or en dash.
"""

_INTENT = {
    "card_expired": "The saved card expired. Retrying it will keep failing until replaced.",
    "insufficient_funds": "A funding shortfall, usually temporary. Space the retry out.",
    "generic_decline": "The issuer declined without a reason, often a temporary hold.",
}


def _client():
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        sys.exit(
            "OPENAI_API_KEY is required to regenerate templates.\n"
            "This is a build-time script; the runtime path performs no inference."
        )
    from langchain_openai import ChatOpenAI

    # ZERO-TOKEN CLASSIFICATION: BUILD-TIME. This module is never imported by
    # the application; it runs by hand, its output is committed, and the
    # runtime renders that committed artifact with no model involved.
    return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0.4)


def _ask(llm, instruction: str) -> str:
    return str(getattr(llm.invoke(instruction), "content", "")).strip()


def main() -> int:
    llm = _client()
    live = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
    draft = {"_meta": dict(live["_meta"]), "diagnosis": {}, "message": {}, "subject": {},
             "fallback": dict(live["fallback"])}

    for code in FAILURE_CODES:
        intent = _INTENT[code]
        draft["message"][code] = _ask(
            llm,
            f"{_RULES}\nWrite the dunning email body (3-5 sentences) for this "
            f"situation: {intent}",
        )
        draft["diagnosis"][code] = _ask(
            llm,
            f"{_RULES}\nWrite a 1-2 sentence internal diagnosis (not customer "
            f"facing, but same slots) for: {intent}",
        )
        draft["subject"][code] = _ask(
            llm,
            f"{_RULES}\nWrite ONLY a plain email subject line, under 60 "
            f"characters, no slots, for: {intent}",
        )

    DRAFT_PATH.write_text(
        json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {DRAFT_PATH}")
    print("NOT published. Diff it, read every changed line, then move it into place.")
    print(f"  diff {LIVE_PATH} {DRAFT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
