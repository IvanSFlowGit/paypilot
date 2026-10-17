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


def test_twelve_slides_between_the_markers():
    assert len(re.findall(r"^## Slide \d+\.", _slides(), re.M)) == 12


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
    table = _slide(body, "## Slide 10")
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


def test_the_burn_is_stated_rather_than_left_as_a_division():
    """EUR 750,000 over 24 months is the first sum an investor does."""
    body = _slides()
    ask = _slide(body, "## Slide 9")
    assert "EUR 750,000" in ask
    assert "24 months" in ask
    assert "31,000 a month" in ask, "the monthly burn is not on the ask slide"
