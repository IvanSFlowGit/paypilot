"""Synthetic evals for follow-up copy: every failure code x every stage x awkward customers.

Two halves, and the second is what makes the first worth believing:

1. THE POPULATION. Every cell of the grid, rendered through the same function production uses when
   it falls back to committed copy, must meet every rule in ``cases.synthetic_dunning.check``.
2. THE CONTROLS. The checker is run on deliberately broken emails and must flag the specific rule
   each one breaks. A checker that has only ever returned "clean" has been run, not tested.
"""

from __future__ import annotations

import pytest
from cases.synthetic_dunning import (
    CUSTOMERS,
    NO_RETRY_CODES,
    TOUCH_CAPS,
    UNSEEN_CODE,
    cells,
    check,
    overlap,
    words,
)

from app import nodes, templates

CODES = templates.failure_codes()


@pytest.fixture(autouse=True)
def _run_as_demo_tenant(monkeypatch):
    monkeypatch.setenv("PAYPILOT_TENANT", "demo")


def _render(cell):
    event = {"failure_code": cell.code, "touch": cell.touch, "max_touches": cell.cap}
    customer = {"name": cell.name, "plan": cell.plan}
    stage = nodes.event_stage(event)
    return (
        nodes._safe_template_message(event, customer),
        templates.get("subject", cell.code, stage=stage),
        stage,
    )


def _first_touch_words(code: str, name: str = "Ada", plan: str = "Pro") -> int:
    """Words in the first-touch email for the SAME customer, so a long name cannot make a follow-up look long."""
    event = {"failure_code": code}
    if name == "Ada" and plan == "Pro":
        return words(templates.render("message", code, name=name, plan=plan, business="Acme"))
    return words(nodes._safe_template_message(event, {"name": name, "plan": plan}))


def test_grid_is_the_whole_population_and_covers_every_code():
    grid = cells(CODES)
    assert len(grid) == (len(CODES) + 1) * len(TOUCH_CAPS) * len(CUSTOMERS)
    assert {c.code for c in grid} == set(CODES) | {UNSEEN_CODE}
    assert len(CODES) == 7, "a new failure code was added: it needs follow-up copy and this grid will cover it"


def test_every_cell_meets_every_rule():
    failures: dict[str, list[str]] = {}
    for cell in cells(CODES):
        text, subject, stage = _render(cell)
        bad = check(text, subject, code=cell.code, stage=stage,
                    first_words=_first_touch_words(cell.code, cell.name, cell.plan))
        if bad:
            failures[cell.key] = bad
    assert not failures, f"{len(failures)} of {len(cells(CODES))} cells broke a rule; first: {next(iter(failures.items()))}"


def test_all_three_stages_are_reached_by_the_grid():
    stages = {_render(c)[2] for c in cells(CODES)}
    assert stages == {"first", "reminder", "final"}


def test_stages_are_different_emails_not_rewordings():
    for code in CODES + [UNSEEN_CODE]:
        t = {s: templates.render("message", code, stage=s, name="Ada", plan="Pro", business="Acme")
             for s in templates.STAGES}
        for a, b in (("first", "reminder"), ("first", "final"), ("reminder", "final")):
            assert overlap(t[a], t[b]) < 0.5, (code, a, b, round(overlap(t[a], t[b]), 2))


def test_no_retry_codes_stay_no_retry_at_every_stage():
    for code in NO_RETRY_CODES:
        for stage in templates.STAGES:
            text = templates.render("message", code, stage=stage, name="Ada", plan="Pro", business="Acme")
            assert not check(text, templates.get("subject", code, stage=stage),
                             code=code, stage=stage, first_words=_first_touch_words(code)), (code, stage)


# ---------------------------------------------------------------------------------------------
# Controls: break one thing, expect exactly that rule to fire. A passing checker proves nothing until it can fail.
# ---------------------------------------------------------------------------------------------

def _good(code: str, stage: str) -> tuple[str, str]:
    return (templates.render("message", code, stage=stage, name="Ada", plan="Pro", business="Acme"),
            templates.get("subject", code, stage=stage))


def _rules(code, stage, text, subject):
    return check(text, subject, code=code, stage=stage, first_words=_first_touch_words(code))


def test_the_unbroken_controls_are_clean():
    for stage in templates.STAGES:
        text, subject = _good("insufficient_funds", stage)
        assert _rules("insufficient_funds", stage, text, subject) == []


@pytest.mark.parametrize(
    "code,stage,mutate,rule",
    [
        ("card_expired", "final", lambda t, s: (t.replace("last reminder", "reminder"), s), "final_not_marked_last"),
        ("card_expired", "reminder", lambda t, s: (t + " This is the last reminder we'll send.", s), "false_last_reminder"),
        ("card_expired", "first", lambda t, s: (t, "Last reminder: " + s), "false_last_reminder"),
        ("issuer_do_not_retry", "reminder", lambda t, s: (t + " We'll retry in two days.", s), "promises_retry_that_will_not_run"),
        ("direct_debit_not_retried", "final", lambda t, s: (t + " We will try again tomorrow.", s), "promises_retry_that_will_not_run"),
        ("insufficient_funds", "final", lambda t, s: (t + " We'll automatically retry soon.", s), "final_promises_more_action"),
        ("generic_decline", "final", lambda t, s: (t + " Your service is still running for now.", s), "final_asserts_service_state"),
        ("card_expired", "reminder", lambda t, s: (t + " See https://example.test/pay", s), "url_in_copy"),
        ("card_expired", "reminder", lambda t, s: (t.replace("Hello", "Hi", 1), s), "greeting"),
        ("card_expired", "reminder", lambda t, s: (t.replace(",", " \u2014", 1), s), "dash"),
        ("card_expired", "reminder", lambda t, s: (t.replace("{plan}", "x") + " {plan}", s), "unfilled_placeholder"),
        ("card_expired", "reminder", lambda t, s: (t + " " + t, s), "follow_up_longer_than_first"),
        ("card_expired", "reminder", lambda t, s: (t, "Reminder: URGENT action needed!"), "subject_pressure_or_fake_thread"),
        ("card_expired", "reminder", lambda t, s: (t, "Reminder: " + "x" * 60), "subject_too_long"),
        ("card_expired", "reminder", lambda t, s: (t, s.replace("Reminder: ", "")), "subject_prefix"),
        ("card_expired", "reminder", lambda t, s: (t.replace("Please update", "Update").replace("please", ""), s), "no_call_to_action"),
        ("card_expired", "reminder", lambda t, s: (t + " Built with LangChain.", s), "build_mechanics"),
        ("card_expired", "reminder", lambda t, s: (t + " As an AI I hope this helps.", s), "ai_tell"),
    ],
)
def test_the_checker_flags_the_rule_that_was_broken(code, stage, mutate, rule):
    text, subject = _good(code, stage)
    bad_text, bad_subject = mutate(text, subject)
    assert rule in _rules(code, stage, bad_text, bad_subject), rule
