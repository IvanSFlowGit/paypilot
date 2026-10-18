"""The investor deck's own structure, asserted so the leaks cannot come back.

`docs/deck-2026-11-investor.md` is the source of record for a deck shown to
investors. Three defects in it were found by reading it as the audience rather
than as the author, and all three are the kind that a dash scan and a spell
check pass happily:

* the file's internal header sat before the first horizontal rule, so a
  renderer splitting on rules alone would have made it slide one and opened an
  investor presentation with "GROW THE PLAN rather than shrink the raise",
  handing the reader the argument that the ask could have been a third of the
  size,
* two slides explained the PREVIOUS version of the deck to a reader who has
  never seen it, which is a slide talking about its own drafting,
* the milestone table once carried an ARR figure at month 18 and a customer
  count at month 24, which against the contract value on the pricing slide made
  month 18 promise more revenue than month 24.

Positive markers rather than a counted rule, because the absence of a marker is
a real signal where a miscounted rule is only a mistake.
"""

from __future__ import annotations

import pathlib
import re

DECK = pathlib.Path(__file__).resolve().parents[1] / "docs" / "deck-2026-11-investor.md"
BEGIN = "<!-- deck:slides-begin -->"
END = "<!-- deck:slides-end -->"


def _deck() -> str:
    return DECK.read_text(encoding="utf-8")


def _slides() -> str:
    """Only what gets rendered. Everything outside the markers is internal."""
    s = _deck()
    assert BEGIN in s, f"missing {BEGIN}"
    assert END in s, f"missing {END}"
    assert s.index(BEGIN) < s.index(END), "markers are in the wrong order"
    return s.split(BEGIN, 1)[1].split(END, 1)[0]


def _slide(body: str, heading: str) -> str:
    """One slide's text.

    Split on a rule ALONE ON ITS LINE. Splitting on the bare string "---"
    cuts at a markdown table's own |---|---| separator row, which silently
    truncated the milestone table to its header and redded this file on
    correct content the first time it ran.
    """
    after = body.split(heading, 1)[1]
    return after.split("\n---\n", 1)[0]


def test_the_rendered_region_is_marked_at_both_ends():
    _slides()


def test_fourteen_slides_between_the_markers():
    assert len(re.findall(r"^## Slide \d+\.", _slides(), re.M)) == 14


def test_no_internal_note_can_reach_a_slide():
    """The header explains our own drafting. An investor must never see it."""
    body = _slides()
    for leak in (
        "GROW THE PLAN",
        "shrink the raise",
        "ReportLab",
        "Source of record",
        "never rendered",
        "previous version",
        "previous milestone",
    ):
        assert leak not in body, f"internal note leaked into a slide: {leak!r}"


def test_the_milestone_table_states_counts_and_never_revenue():
    """A count is a milestone the company controls; ARR is two assumptions."""
    body = _slides()
    table = _slide(body, "## Slide 12")
    # "| M" also matches the header row "| Month | Milestone |". Anchor on the
    # month number, which is what a data row actually has.
    rows = [r for r in table.splitlines() if re.match(r"^\| M\d+ \|", r)]
    assert len(rows) == 6, rows
    # Sweep the ROWS, not the section. The prose under the table explains why
    # there is no ARR figure in it, so a check over the whole section is
    # satisfied by its own documentation and reds on correct content.
    cells = "\n".join(rows)
    assert "ARR" not in cells, "the milestone table is stating revenue again"
    # The EUR 5,000 pilot price is a milestone the company controls and is
    # allowed. A six figure EUR amount in a row is a revenue target.
    big = re.findall(r"EUR [0-9]{3},[0-9]{3}", cells)
    assert not big, f"revenue target back in the milestone table: {big}"


def test_the_platforms_are_not_named_as_competitors():
    body = _slides()
    assert "Stripe and Recharge are not competitors" in body
    assert "already recover failed payments" not in body


def test_the_four_real_competitors_are_named():
    body = _slides()
    for name in ("Butter Payments", "Gravy", "Churn Buster", "Revaly"):
        assert name in body, f"{name} missing from the competitor slide"


def test_no_recovery_percentage_is_claimed_anywhere():
    """Nothing has recovered a payment for a paying customer.

    A recovery figure with no control group behind it is unfalsifiable by
    construction, which is the deck's own argument on the pricing slide.
    """
    body = _slides()
    claims = re.findall(r"recover\w*\s+[0-9]+|[0-9]+\s*per cent of (?:failed|recover)", body)
    assert not claims, f"a recovery rate reached the deck: {claims}"


def test_no_test_count_appears_in_written_copy():
    """A retired CLASS of figure, not a stale number.

    retired-numbers.md retired every PayPilot suite count on 2026-08-08 and
    streamflowsnippets.md has carried "NO TEST COUNT IN WRITTEN COPY. The
    approved phrase is a test and evaluation suite in CI" since 2026-08-21.
    The reason that still bites is staleness rather than verifiability: this
    deck was written saying 1,555, and the figure moved to 1,559 and then
    1,567 inside the same session that wrote it.
    """
    body = _slides()
    counts = re.findall(r"[0-9],[0-9]{3} (?:automated )?(?:checks|tests)", body)
    assert not counts, f"a retired class of figure reached the deck: {counts}"
    assert "test and evaluation suite" in body, "the approved phrase is missing"


def test_the_burn_is_stated_rather_than_left_as_a_division():
    """EUR 750,000 over 24 months is the first sum an investor does."""
    body = _slides()
    ask = _slide(body, "## Slide 11")
    assert "EUR 750,000" in ask
    assert "24 months" in ask
    assert "31,000 a month" in ask, "the monthly burn is not on the ask slide"


# Feedback on the first version, 2026-10-18: the slides carried 150 to 230 words
# each and the ask slide was "way too much text". The longest slide after the cut
# is 62 words, so the ceiling is 70, set against the measured corpus rather than
# guessed. A slide that grows past it has stopped being a slide.
MAX_WORDS_PER_SLIDE = 70


def test_no_slide_is_a_wall_of_text():
    for chunk in re.split(r"\n---\n", _slides()):
        title = re.search(r"^## (Slide \d+)\.", chunk, re.M)
        if title is None:
            continue
        body = chunk.split("\n", 2)[-1]
        words = len(re.sub(r"[|#*]|-{2,}", " ", body).split())
        assert words <= MAX_WORDS_PER_SLIDE, f"{title.group(1)} has {words} words"



def test_the_loss_slide_prints_a_figure_only_with_its_source():
    """Feedback 2026-10-18: show what is lost. A number with no source is the
    failure this estate exists to stop, so the slide carries the source line."""
    loss = _slide(_slides(), "## Slide 3")
    assert re.search(r"\d+%", loss) and re.search(r"\d{3},\d{3}", loss), "the loss slide prints no percentage and dollar-scale figure"
    assert "Industry data, not PayPilot data" in loss
    assert re.search(r"Read 20\d\d-\d\d-\d\d", loss), "no read date on the loss slide"


def test_the_tagline_and_the_synthetic_label_are_present():
    body = _slides()
    assert "PayPilot reclaims lost subscription revenue." in body
    walk = _slide(body, "## Slide 5")
    assert "Synthetic example, not customer data." in walk


def test_no_slide_uses_the_internal_vocabulary():
    """Feedback 2026-10-18: normal English. These words belong in the notes."""
    body = _slides().lower()
    for word in ("audit row", "row level", "holdout", "append only"):
        assert word not in body, f"{word!r} reached a slide"


def test_the_competitor_table_states_what_it_read_and_when():
    """Feedback 2026-10-18: a side by side table. Every competitor cell is
    either a fact read from their own site or 'Not stated', never 'does not
    do', and the slide carries the read date."""
    comp = _slide(_slides(), "## Slide 7")
    assert re.search(r"read 20\d\d-\d\d-\d\d", comp), "no read date on the competitor slide"
    for name in ("Butter Payments", "Gravy", "Churn Buster", "Revaly"):
        row = next(line for line in comp.splitlines() if line.startswith(f"| {name}"))
        assert row.count("Not stated") == 2, f"{name}: both capability cells must say Not stated"
    assert "does not" not in comp.lower()
