"""The FAQ lives in one place and three surfaces must carry it.

MEASURED 2026-10-02, BEFORE scripts/render_faq.py existed: the visible accordion,
the FAQPage JSON-LD and llms.txt each held six questions by hand, and three of the
six answers disagreed between surfaces. Three hand-kept copies agreeing is luck,
and nothing could have reported the day it stopped.

What is asserted as SHARED is the question set, its wording and its ORDER. The
answer text is per surface on purpose: llms.txt is read by answer engines and
carries pointers a human does not need in an accordion, so a longer form there is
a feature. That divergence is declared in the data as answer_long rather than
being left to look like drift.

Also pinned here: the FAQ edit must not disturb the other JSON-LD blocks. The
first version of the renderer used a pattern anchored on the first script tag and
running forward to FAQPage, which cannot bound a nested structure and silently ate
the SoftwareApplication, Organization and WebSite blocks, taking the page from four
to one. The audit in scripts/seo_optimize.py would have failed the deploy on that,
but only after it shipped here.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PATH = ROOT / "scripts" / "render_faq.py"
_spec = importlib.util.spec_from_file_location("render_faq", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_LD = re.compile(r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', re.S)
REQUIRED_SCHEMA = {"SoftwareApplication", "Organization", "WebSite", "FAQPage"}


def _index() -> str:
    return mod.INDEX.read_text(encoding="utf-8")


def _blocks(html: str) -> dict[str, dict]:
    out = {}
    for block in _LD.finditer(html):
        parsed = json.loads(block.group(1))
        out[parsed.get("@type")] = parsed
    return out


def _jsonld_questions() -> list[tuple[str, str]]:
    faq = _blocks(_index())["FAQPage"]
    return [(q["name"], q["acceptedAnswer"]["text"]) for q in faq["mainEntity"]]


def _accordion_questions() -> list[tuple[str, str]]:
    html = _index()
    start = html.find('<div class="faq">')
    section = html[start : html.find("</section>", start)]
    return [
        (q.strip(), " ".join(a.split()))
        for q, a in re.findall(r"<summary>(.*?)</summary>\s*<p>(.*?)</p>", section, re.S)
    ]


def _llms_questions() -> list[str]:
    text = mod.LLMS.read_text(encoding="utf-8")
    start = text.find("## FAQ")
    nxt = text.find("\n## ", start + 1)
    section = text[start : len(text) if nxt == -1 else nxt]
    return [line[2:].strip() for line in section.splitlines() if line.startswith("- ")]


def test_control_every_surface_is_non_empty():
    """Without this the comparisons below pass on three empty lists."""
    assert len(mod.FAQ) >= 6
    assert len(_jsonld_questions()) == len(mod.FAQ)
    assert len(_accordion_questions()) == len(mod.FAQ)
    assert len(_llms_questions()) == len(mod.FAQ)


def test_committed_surfaces_match_the_source():
    assert _index() == mod.rendered_index(), (
        "app/static/index.html is stale: run python scripts/render_faq.py"
    )
    assert mod.LLMS.read_text(encoding="utf-8") == mod.rendered_llms(), (
        "app/static/llms.txt is stale: run python scripts/render_faq.py"
    )


def test_the_question_set_and_its_order_is_identical_on_all_three():
    expected = [entry["question"] for entry in mod.FAQ]
    assert [q for q, _ in _jsonld_questions()] == expected
    assert [q for q, _ in _accordion_questions()] == expected
    for question, line in zip(expected, _llms_questions(), strict=True):
        assert line.startswith(question), line[:90]


def test_the_accordion_and_the_jsonld_carry_the_same_answers():
    """These two are read by a human and a crawler on the same page, so a
    difference between them is drift rather than design. Q6 differed by one
    character before this file existed."""
    assert _jsonld_questions() == _accordion_questions()


def test_llms_uses_the_long_answer_where_one_is_declared():
    for entry, line in zip(mod.FAQ, _llms_questions(), strict=True):
        assert line == f"{entry['question']} {mod.long_answer(entry)}"


def test_the_other_schema_blocks_survive_the_faq_render():
    present = set(_blocks(_index()))
    assert REQUIRED_SCHEMA <= present, f"missing: {REQUIRED_SCHEMA - present}"
    assert _index().count("application/ld+json") == 4


def test_rendering_twice_changes_nothing():
    """A transformer that reads its own output must reach a fixpoint."""
    once = mod.rendered_index()
    mod.INDEX.write_text(once, encoding="utf-8")
    try:
        assert mod.rendered_index() == once
    finally:
        mod.INDEX.write_text(once, encoding="utf-8")


def test_the_new_entries_make_the_honest_claim_about_ach():
    """PayPilot publishes a decision table for ACH; it does not handle ACH. The
    overstatement is the one a reader would make on their own."""
    answers = {q: a for q, a in _jsonld_questions()}
    ach = next(a for q, a in answers.items() if "ACH returns as well as" in q)
    assert "not wired into the dunning agent" in ach
    assert "would overstate" in ach


def test_no_faq_answer_states_a_rate_limit_or_threshold():
    joined = " ".join(a for _, a in _jsonld_questions()).lower()
    joined += " ".join(_llms_questions()).lower()
    for banned in ("return rate", "retry limit", "threshold"):
        assert banned not in joined, banned
    assert not re.search(r"\d+\s*%", joined), "a percentage reached the FAQ"
