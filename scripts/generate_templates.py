# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
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

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_PATH = REPO_ROOT / "data" / "templates" / "dunning.json"
DRAFT_PATH = REPO_ROOT / "data" / "templates" / "dunning.draft.json"


def draft_path(locale: str | None) -> Path:
    """Where a draft for this locale is written. English keeps the original name."""
    if not locale or locale == "en":
        return DRAFT_PATH
    return REPO_ROOT / "data" / "templates" / f"dunning.{locale}.draft.json"


# A TRANSLATION IS MADE FROM THE ENGLISH, NEVER REGENERATED FROM THE INTENT.
# Regenerating per language gives each language its own run of the model and they
# drift apart in MEANING, so the Spanish customer is told something the English one
# is not and nobody notices because nobody on the team reads both. One source of
# truth, translated, keeps the languages saying the same thing.
#
# THE SLOTS ARE THE PART THAT BREAKS. {name}, {plan} and {business} are filled by
# str.format at runtime, so a model that helpfully translates {name} to {nombre}
# produces a KeyError, and templates.render returns the unfilled text rather than
# raising, which means a customer reads a literal brace. So the instruction forbids
# touching them and the script ASSERTS the slot set is identical afterwards.
_TRANSLATE = (
    "Translate the text below into {language}. Rules, all of them absolute:\n"
    "1. Leave every {{curly brace placeholder}} EXACTLY as it is, same spelling, "
    "untranslated. They are code.\n"
    "2. Do not improve, shorten or soften the message. Translate what is there.\n"
    "3. Keep the register: plain, calm, no exclamation marks, no sales language.\n"
    "4. Never use an em dash or an en dash. A plain hyphen only.\n"
    "5. Return ONLY the translation, no preamble and no quotes around it.\n\n"
    "Text:\n{text}"
)

LANGUAGES = {"es": "Spanish (Spain)", "fr": "French (France)", "de": "German (Germany)",
             "pt": "Portuguese (Portugal)", "it": "Italian", "nl": "Dutch",
             "uk": "Ukrainian", "ru": "Russian", "pl": "Polish"}


def slots_of(text: str) -> set:
    return set(re.findall(r"\{([a-z_]+)\}", text or ""))

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


def translate(locale: str) -> int:
    """Translate the LIVE English catalogue into one locale. Draft only, never published.

    Refuses rather than guesses on an unknown locale, because the language name goes
    into the instruction and inventing one produces a translation into something
    nobody asked for.
    """
    if locale not in LANGUAGES:
        sys.exit(f"unknown locale {locale!r}. Known: {', '.join(sorted(LANGUAGES))}.\n"
                 "Add it to LANGUAGES with the language written out in full, because the "
                 "name is what goes into the instruction.")
    language = LANGUAGES[locale]
    live = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
    llm = _client()

    out = {"_meta": {"artifact": f"dunning copy library, {language}",
                     "translated_from": "dunning.json",
                     "why": "Translated from the English at build time, reviewed by a "
                            "human, committed. The runtime calls no model.",
                     "REVIEW": "NOT REVIEWED. A speaker of this language must read every "
                               "line before this is published."}}
    bad = []
    for kind in ("diagnosis", "message", "subject", "fallback"):
        out[kind] = {}
        for key, english in live[kind].items():
            got = _ask(llm, _TRANSLATE.format(language=language, text=english))
            if slots_of(got) != slots_of(english):
                bad.append(f"{kind}.{key}: slots {sorted(slots_of(english))} became "
                           f"{sorted(slots_of(got))}")
            out[kind][key] = got
            print(f"  {kind}.{key}")

    path = draft_path(locale)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {path}")

    if bad:
        print("\nSLOT MISMATCH, so this draft would break rendering if published:")
        for b in bad:
            print(f"  {b}")
        print("Fix those lines by hand before anything else.")
        return 1

    print("Every placeholder survived the translation, checked rather than assumed.")
    print("NOT published, and available_locales() still will not serve it. To publish:")
    print(f"  # have a {language} speaker read every line, then")
    print(f"  mv {path} {str(path).replace('.draft.json', '.json')}")
    print("  pytest    # the locale gates must pass")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--locale", help="translate the live English catalogue into this "
                                     "locale instead of regenerating English")
    a = ap.parse_args()

    if a.locale:
        return translate(a.locale)

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
