# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""Multilingual dunning copy, and the four decisions worth a test each.

Every case here is written against a BEHAVIOUR rather than a literal, and the
fixtures build catalogues in tmp_path rather than shipping real translated copy,
because shipping Spanish copy is a human review step and not a test fixture.
"""
from __future__ import annotations

import json

import pytest

from app import templates


@pytest.fixture
def catalogues(monkeypatch, tmp_path):
    """An English catalogue plus a factory for translations of it."""
    english = {
        "_meta": {"artifact": "test"},
        "diagnosis": {"card_expired": "EN diag expired", "insufficient_funds": "EN diag funds"},
        "message": {"card_expired": "EN msg for {name}", "insufficient_funds": "EN msg funds"},
        "subject": {"card_expired": "EN subj expired", "insufficient_funds": "EN subj funds"},
        "fallback": {"diagnosis": "EN diag generic", "message": "EN msg generic",
                     "subject": "EN subj generic"},
    }
    (tmp_path / "dunning.json").write_text(json.dumps(english), encoding="utf-8")
    monkeypatch.setattr(templates, "TEMPLATES_DIR", tmp_path)
    monkeypatch.setattr(templates, "TEMPLATES_PATH", tmp_path / "dunning.json")
    templates.reload()

    def write(locale: str, doc: dict) -> None:
        (tmp_path / f"dunning.{locale}.json").write_text(json.dumps(doc), encoding="utf-8")
        templates.reload()

    yield write
    # CLEARED RATHER THAN RELOADED, and this cost 25 failures in the rest of the suite
    # before it was fixed. reload() READS FROM DISK, and at teardown monkeypatch has not
    # yet restored TEMPLATES_DIR, so reloading repopulated the cache from tmp_path and
    # then the real paths came back with the TEST catalogue still cached. Canon: a test
    # that mutates module state without restoring it poisons every test after it, and
    # the tell is that the file passes alone and fails in the suite. It did exactly that.
    templates._cache = {}
    templates._incomplete.clear()
    templates._served = None


def _complete(prefix: str) -> dict:
    return {
        "diagnosis": {"card_expired": f"{prefix} diag expired",
                      "insufficient_funds": f"{prefix} diag funds"},
        "message": {"card_expired": f"{prefix} msg for {{name}}",
                    "insufficient_funds": f"{prefix} msg funds"},
        "subject": {"card_expired": f"{prefix} subj expired",
                    "insufficient_funds": f"{prefix} subj funds"},
        "fallback": {"diagnosis": f"{prefix} diag generic",
                     "message": f"{prefix} msg generic",
                     "subject": f"{prefix} subj generic"},
    }


def test_english_is_unchanged_by_the_locale_machinery(catalogues):
    """The regression guard. Every existing caller passes no locale at all."""
    assert templates.get("message", "card_expired") == "EN msg for {name}"
    assert templates.render("message", "card_expired", name="Ada") == "EN msg for Ada"
    assert templates.available_locales() == ["en"]


def test_a_complete_translation_is_served(catalogues):
    catalogues("es", _complete("ES"))
    assert "es" in templates.available_locales()
    assert templates.render("message", "card_expired", locale="es", name="Ada") == "ES msg for Ada"


def test_a_HALF_translated_locale_is_refused_WHOLE_rather_than_merged(catalogues):
    """THE LOAD BEARING DECISION.

    A catalogue missing one code must not serve the codes it does have, because
    that mails a customer a Spanish subject over an English body, which reads as
    broken software rather than as a missing translation.
    """
    half = _complete("ES")
    del half["message"]["insufficient_funds"]
    catalogues("es", half)

    assert "es" not in templates.available_locales()
    why = templates.incomplete_locales()["es"]
    assert "insufficient_funds" in why and "message" in why
    # and the code it DID translate is still not served
    assert templates.render("message", "card_expired", locale="es", name="Ada") == "EN msg for Ada"
    assert templates.render("subject", "card_expired", locale="es") == "EN subj expired"


def test_the_fallback_is_REPORTED_rather_than_silent(catalogues):
    """A fallback nobody records is indistinguishable from a working translation."""
    text, used = templates.render_with_locale("message", "card_expired", "de", name="Ada")
    assert used == "en", "a locale with no catalogue falls back"
    assert text == "EN msg for Ada"

    catalogues("es", _complete("ES"))
    text, used = templates.render_with_locale("message", "card_expired", "es", name="Ada")
    assert used == "es" and text == "ES msg for Ada"


def test_a_regional_tag_falls_back_to_its_base_language(catalogues):
    catalogues("es", _complete("ES"))
    assert templates.resolve("es-MX") == "es"
    assert templates.resolve("es_MX") == "en", "an underscore is not a valid tag"


@pytest.mark.parametrize("bad", [None, "", "   ", "ZZZ", "english", "e", "es-mx",
                                 "../../etc/passwd", "en;DROP", 42])
def test_a_malformed_locale_resolves_to_english_without_raising(catalogues, bad):
    """The caller's job is to recover a payment, not to validate a language tag."""
    assert templates.resolve(bad) == "en"


def test_a_locale_cannot_be_used_to_read_a_file_outside_the_templates_dir(catalogues):
    """path_for is only ever reached through resolve, and resolve refuses a path."""
    assert templates.resolve("../secrets") == "en"
    assert templates.path_for("es").parent == templates.TEMPLATES_DIR


def test_an_unknown_failure_code_still_falls_back_inside_the_chosen_locale(catalogues):
    catalogues("es", _complete("ES"))
    assert templates.render("message", "a_code_stripe_never_showed_us",
                            locale="es") == "ES msg generic"


def test_no_locale_path_derives_a_language_from_the_currency(catalogues):
    """CURRENCY IS NOT A LANGUAGE and it is the only geographic field in the codebase,
    which is exactly why a future reader will reach for it. EUR spans twenty countries
    and USD is legal tender in Ecuador and Panama."""
    import inspect
    src = inspect.getsource(templates)
    body = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "currency" not in body.lower(), (
        "a locale resolved from currency is a guess about what somebody reads")


def test_the_english_hot_path_does_not_scan_for_locales(catalogues, monkeypatch):
    """available_locales() globs the directory and reads every catalogue, and this
    path runs once per payment failure. The first version of resolve() called it
    unconditionally, so English rendering did disk work on every event.

    Patches available_locales rather than Path.glob, because PosixPath.glob is read
    only and the first version of this test failed on that rather than on the code.
    """
    templates.render("message", "card_expired", name="warm")  # warm any cache
    calls = []
    real = templates.available_locales
    monkeypatch.setattr(templates, "available_locales",
                        lambda: (calls.append(1), real())[1])
    for _ in range(25):
        templates.render("message", "card_expired", name="Ada")
        templates.resolve(None)
        templates.resolve("en")
    assert calls == [], "English rendering must not scan the templates directory"


def test_a_non_english_locale_scans_ONCE_and_then_caches(catalogues, monkeypatch):
    """The other direction, so the short circuit above is not hiding a dead cache."""
    catalogues("es", _complete("ES"))
    templates.resolve("es")  # warm
    calls = []
    real = templates.available_locales
    monkeypatch.setattr(templates, "available_locales",
                        lambda: (calls.append(1), real())[1])
    for _ in range(25):
        templates.render("message", "card_expired", locale="es", name="Ada")
    assert calls == [], "a warmed non English locale must not rescan either"


def test_the_deterministic_paths_use_the_customer_locale(catalogues, monkeypatch):
    """The real multilingual path: these two hold the customer dict."""
    from app import nodes
    catalogues("es", _complete("ES"))
    cust = {"name": "Ada", "plan": "Pro", "locale": "es"}
    assert nodes._safe_template_message({"failure_code": "card_expired"}, cust) == "ES msg for Ada"
    assert nodes._safe_template_diagnosis({"failure_code": "card_expired"},
                                          cust) == "ES diag expired"
    assert nodes.customer_locale(cust) == "es"
    assert nodes.customer_locale({}) == "en", "no locale means English, stated not guessed"
    assert nodes.customer_locale(None) == "en"


def test_a_hostile_locale_on_the_customer_cannot_escape_the_templates_dir(catalogues):
    from app import nodes
    for bad in ("../../../etc/passwd", "en/../../secret", {"nope": 1}, ["es"]):
        assert nodes.customer_locale({"locale": bad}) == "en"


def test_the_prompt_driven_engine_is_english_and_that_limit_IS_THE_TEST(catalogues):
    """Documented in _TemplateEngine's docstring. Asserted so it cannot rot into a
    silent gap that somebody later reads as multilingual."""
    from app import nodes
    catalogues("es", _complete("ES"))
    out = nodes._TemplateEngine().invoke("dunning email body\nCustomer name: Ada\nPlan: Pro")
    assert out.startswith("EN "), (
        "the prompt driven engine has no customer dict, so it serves English. If this "
        "ever changes, the docstring and this test change with it.")
