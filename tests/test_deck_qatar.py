"""The Qatar deck's structure, and the honesty it must share with the investor deck.

`docs/deck-2026-11-qatar.md` is an adaptation of `docs/deck-2026-11-investor.md`
for the QDB application form. Two decks is the right answer, because QDB takes
equity tranched against milestones and the investor deck asks for a SAFE, so one
file serving both would have had two jobs.

TWO FILES IS ALSO THE RISK THIS MODULE EXISTS FOR. A copy made for distribution
keeps the old behaviour after the source is fixed, and the copy is the one the
reader gets. So this does NOT assert the two files match, which would forbid the
differences they exist to carry. It asserts the properties that must hold of
both, and it imports the shared constants from `test_deck` rather than restating
their values, so a ceiling changed in one place cannot drift between the decks.

The Qatar-specific assertions are the ones that would otherwise go unchecked:
that the SAFE is gone, that the Qatar slide says only what Ivan actually
confirmed, and that the two competitors added to the comparison are present with
their read date.
"""

from __future__ import annotations

import pathlib
import re

from test_deck import BEGIN, END, MAX_WORDS_PER_SLIDE

DECK = pathlib.Path(__file__).resolve().parents[1] / "docs" / "deck-2026-11-qatar.md"
SLIDE_COUNT = 16


def _slides() -> str:
    s = DECK.read_text(encoding="utf-8")
    assert BEGIN in s, f"missing {BEGIN}"
    assert END in s, f"missing {END}"
    assert s.index(BEGIN) < s.index(END), "markers are in the wrong order"
    return s[s.index(BEGIN) + len(BEGIN) : s.index(END)]


def _slide(body: str, heading: str) -> str:
    chunk = next((c for c in re.split(r"\n---\n", body) if heading in c), None)
    assert chunk is not None, f"{heading} not found"
    return chunk


# --------------------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------------------

def test_sixteen_slides_between_the_markers():
    assert len(re.findall(r"^## Slide \d+\.", _slides(), re.M)) == SLIDE_COUNT


def test_the_slide_numbers_run_without_a_gap():
    """A renumbering that drops or repeats a number is invisible to a count alone."""
    nums = [int(n) for n in re.findall(r"^## Slide (\d+)\.", _slides(), re.M)]
    assert nums == list(range(1, SLIDE_COUNT + 1)), nums


def test_no_internal_note_can_reach_a_slide():
    """The header explains our own drafting and names the three answers this deck
    rests on. A reviewer at QDB must never see any of it."""
    body = _slides()
    for leak in (
        "WHY THIS IS A SECOND FILE",
        "asked directly",
        "sales asset",
        "never rendered",
        "Source of record",
        "ReportLab",
        "spends the clock",
    ):
        assert leak not in body, f"internal note leaked into a slide: {leak!r}"


def test_no_slide_is_a_wall_of_text():
    """Same ceiling as the investor deck, imported rather than restated."""
    for chunk in re.split(r"\n---\n", _slides()):
        title = re.search(r"^## (Slide \d+)\.", chunk, re.M)
        if title is None:
            continue
        body = chunk.split("\n", 2)[-1]
        words = len(re.sub(r"[|#*]|-{2,}", " ", body).split())
        assert words <= MAX_WORDS_PER_SLIDE, f"{title.group(1)} has {words} words"


# --------------------------------------------------------------------------------------
# The honesty both decks share
# --------------------------------------------------------------------------------------

def test_no_recovery_percentage_is_claimed_anywhere():
    """Nothing has recovered a payment for a paying customer, so no rate may appear."""
    body = _slides()
    claims = re.findall(r"recover\w*\s+[0-9]+|[0-9]+\s*per cent of (?:failed|recover)", body)
    assert not claims, f"a recovery rate reached the deck: {claims}"


def test_no_test_count_appears_in_written_copy():
    """A retired CLASS of figure. The count has moved nine times; the fix is no digit."""
    body = _slides()
    counts = re.findall(r"[0-9],[0-9]{3} (?:automated )?(?:checks|tests)", body)
    assert not counts, f"a retired class of figure reached the deck: {counts}"
    assert "test and evaluation suite" in body, "the approved phrase is missing"


def test_the_standing_honesty_pair_is_both_halves_or_neither():
    """Deployed AND carrying no client traffic. Either half alone is a false claim."""
    stands = _slide(_slides(), "## Slide 15")
    assert "Zero customers. Zero pilots. Zero revenue." in stands
    assert "paypilot.fly.dev" in stands
    assert "No client traffic." in stands


def test_the_platforms_are_not_named_as_competitors():
    """ASSERT THE PROPERTY, NOT THE SENTENCE. This first read

        assert "Stripe and Recharge are not competitors" in _slides()

    copied verbatim from `test_deck.py`, and it went red when two words were cut
    from that line to bring slide 7 under the word ceiling. The deck was right and
    the assertion was testing the wording rather than the claim, which is the
    defect where an assertion written against the literal you expected tests your
    expectation. The claim is that the platforms appear as something PayPilot plugs
    into and never as rivals, and that survives a rewrite.
    """
    comp = _slide(_slides(), "## Slide 7")
    assert "Stripe and Recharge" in comp, "the platforms are not placed at all"
    assert "not competitors" in comp, "the platforms are not disclaimed as rivals"
    assert "plugs into" in comp, "the platforms are not named as integration targets"
    for row in comp.splitlines():
        assert not row.startswith("| Stripe"), "a platform became a table row"
        assert not row.startswith("| Recharge"), "a platform became a table row"


def test_no_slide_uses_the_internal_vocabulary():
    body = _slides().lower()
    for word in ("audit row", "row level", "holdout", "append only"):
        assert word not in body, f"{word!r} reached a slide"


def test_the_loss_slide_prints_a_figure_only_with_its_source():
    loss = _slide(_slides(), "## Slide 3")
    assert "Industry data, not PayPilot data" in loss
    assert re.search(r"Read 20\d\d-\d\d-\d\d", loss), "no read date on the loss slide"


# --------------------------------------------------------------------------------------
# Qatar specifics
# --------------------------------------------------------------------------------------

def test_the_safe_is_gone_because_qdb_takes_equity():
    """QDB's own FAQ: funding is tranched against mutually agreed milestones, and its
    F6S profile states it takes 5-50% equity. A SAFE on this deck would be wrong."""
    terms = _slide(_slides(), "## Slide 16")
    assert "SAFE" not in terms, "the investor deck's instrument survived into the Qatar deck"
    assert "QDB proposes" in terms, "the terms slide does not leave the stake to them"


def test_no_equity_percentage_is_offered():
    """They state 5-50%. We do not pre-concede a point inside their own range, and
    canon forbids answering a negotiable question with an unqualified number."""
    body = _slides()
    offered = re.findall(r"\b\d{1,2}\s*(?:%|per cent)\s+(?:equity|of the company|stake)", body)
    assert not offered, f"an equity figure was offered: {offered}"


def test_the_ask_is_the_plans_own_number_and_not_their_ceiling():
    """QDB's START ceiling is about EUR 1.01M. The plan needs 750,000. Asking for the
    ceiling would be a number with no plan behind it."""
    ask = _slide(_slides(), "## Slide 13")
    assert "EUR 750,000" in ask
    assert "24 months" in ask
    assert "31,000 a month" in ask, "the monthly burn is not on the ask slide"
    assert "1.01" not in ask and "1,100,000" not in ask, "the ask drifted to their ceiling"


def test_the_qatar_slide_claims_only_what_was_confirmed():
    """Ivan confirmed three things on 2026-10-24: set up operations, hire locally,
    full time for two years. A headcount, a date or a named hire would be invented."""
    q = _slide(_slides(), "## Slide 10")
    assert "set up operations in Qatar" in q
    assert "hire locally" in q
    assert "full time for the first two years" in q
    invented = re.findall(r"\b\d+\s+(?:engineers|hires|employees|people|staff)", q)
    assert not invented, f"a headcount nobody stated reached the Qatar slide: {invented}"


def test_the_milestones_are_counts_and_tranche_against_them():
    table = _slide(_slides(), "## Slide 14")
    rows = [r for r in table.splitlines() if re.match(r"^\| M\d+ \|", r)]
    assert len(rows) == 6, rows
    assert "tranches against" in table, "the milestone slide does not say what it is for"
    assert not re.findall(r"EUR [0-9]{3},[0-9]{3}", table), "a revenue target reached the table"


def test_the_competitor_table_carries_every_name_and_its_read_date():
    """Two were added on 2026-10-24 because a table showing only the axes we win is a
    sales asset. Both carry a read date and neither says 'does not do'."""
    comp = _slide(_slides(), "## Slide 7")
    for name in ("Churn Buster", "Butter Payments", "Gravy", "Revaly",
                 "Chargebee Revive", "FlyCode"):
        assert any(line.startswith(f"| {name}") for line in comp.splitlines()), \
            f"{name} missing from the competitor slide"
    assert re.search(r"2026-10-18", comp) and re.search(r"2026-10-04", comp), \
        "the competitor slide does not carry both read dates"
    assert "does not" not in comp.lower(), "a capability was asserted as absent"


def test_the_slide_that_states_where_paypilot_is_behind_exists():
    """Without this the comparison is a sales asset. It names the incumbent's scale
    and our own zero, both from the registers."""
    behind = _slide(_slides(), "## Slide 8")
    assert "2013" in behind and "15 integrations" in behind
    assert "no customers" in behind
