"""One pricing model on every public surface.

On 2026-10-17 the investor deck said a platform fee plus a fee per recovered
payment while the homepage, /pricing and /terms said a share of recovered
revenue with no recovery meaning no fee. A visitor to the site and a reader of
the deck were being quoted two different businesses. The deck model was chosen
on 2026-10-18, so the old phrases are asserted ABSENT from every static page
and the new ones asserted present on /pricing.
"""

from __future__ import annotations

import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"

RETIRED = (
    "no recovery, no fee",
    "share of the revenue",
    "share of what",
    "pay only for what",
    "performance basis",
    "performance-based",
    "no seat fee",
)


def _text(name: str) -> str:
    # Newlines flattened first: a phrase that wraps across a source line is
    # invisible to a line based search, which reads as "absent" when it is not.
    return re.sub(r"\s+", " ", (STATIC / name).read_text(encoding="utf-8")).lower()


def test_the_retired_pricing_phrases_are_on_no_static_page():
    for page in sorted(p.name for p in STATIC.glob("*.html")) + ["llms.txt"]:
        body = _text(page)
        for phrase in RETIRED:
            assert phrase not in body, f"{page} still says {phrase!r}"


def test_the_pricing_page_states_the_deck_model():
    body = _text("pricing.html")
    assert "platform fee" in body
    assert "fee per recovered payment" in body
    assert "held-out" in body, "the attribution mechanism is the reason the second fee is defensible"


def test_terms_and_homepage_agree_with_pricing():
    for page in ("terms.html", "index.html"):
        assert "platform fee plus a fee per recovered payment" in _text(page), page
