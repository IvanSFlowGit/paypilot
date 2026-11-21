# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The committed dunning copy library.

Zero-token architecture: a dunning email for a given failure code is the same
class of output on every invoice, so generating it with a model at runtime buys
nothing and costs on every event. The copy is generated once, reviewed by a
human, committed as ``data/templates/dunning.json``, and filled deterministically
at runtime. No model is called on this path.

That makes the copy reviewable the way code is reviewable: it diffs, it can be
tested, and a change to what a customer reads is a pull request rather than an
invisible shift in model behaviour.

The live model stays available for genuinely novel cases behind
``PAYPILOT_LLM_DRAFT=1`` (see :func:`app.nodes.llm_drafting_enabled`), which is
off by default.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = _REPO_ROOT / "data" / "templates"
TEMPLATES_PATH = TEMPLATES_DIR / "dunning.json"

DEFAULT_LOCALE = "en"

# MULTILINGUAL, AND THE TWO DECISIONS THAT MATTER ARE BOTH ABOUT WHAT HAPPENS WHEN A
# TRANSLATION IS MISSING.
#
# ONE, A LOCALE IS ALL OR NOTHING. A half translated catalogue is NOT merged key by
# key with English, because that sends one customer an email whose subject is Spanish
# and whose body is English, which reads as broken software rather than as a missing
# translation. A locale that does not cover every kind and every failure code is
# refused AS A LOCALE and English is served whole. Completeness is checked once at
# load and cached with the document.
#
# TWO, THE FALLBACK IS REPORTED RATHER THAN SILENT. Falling back to English is the
# right direction here and the opposite of how the missing-file case is handled above:
# TemplatesUnavailable is fatal because without ANY copy there is nothing to send,
# while a missing SPANISH catalogue still leaves a working English email, and an
# English dunning email recovers money where no email recovers none. So it falls back.
# But a silent fallback is the defect this estate records most often, because a locale
# nobody added looks identical to a locale working fine. Every resolution returns the
# locale it actually used, the audit event carries both, and a climbing fallback count
# is then the measurement that says which language to translate next.
#
# WHAT MUST NEVER BE DONE HERE: deriving a language from the currency. It is the only
# geographic field this codebase has, which is exactly why a future reader will reach
# for it. EUR spans twenty countries, USD is legal tender in Ecuador and Panama, and
# GBP is not a language. Currency is a unit of money and says nothing about what the
# cardholder reads.
_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")

# FOLLOW-UP STAGES. A dunning email is not one email: the second and third touch to the same
# invoice must not repeat the first. Until this existed every touch rendered the first-touch
# copy byte for byte (measured: 8 of 8 failure codes), so a customer who ignored email one
# received the identical email twice more.
#
# THE STAGE COMES FROM THE TOUCH NUMBER AND THE TENANT'S OWN CAP, NEVER FROM THE CODE.
#   first    touch 1, or any touch we cannot count. This is the original, human-reviewed copy
#            and it is served unchanged, so a caller that supplies nothing sees no difference.
#   reminder touch 2 up to the one before the cap.
#   final    the touch that reaches the cap, when the cap is 2 or more.
# "Final" is the only copy allowed to say this is the last message, and it is only true
# because max_touches says so. A tenant who raises the cap to 5 gets a reminder at touch 3
# and 4 rather than a false "last reminder" that is then followed by two more.
STAGES = ("first", "reminder", "final")
DEFAULT_STAGE = "first"


def stage_for(touch: object, max_touches: object = None) -> str:
    """The copy stage for email number ``touch`` under a cap of ``max_touches``.

    Total and quiet: anything that is not a positive int means FIRST, because the first-touch
    copy is the one that is safe to send to anybody at any point (it asserts nothing about
    earlier emails). A bool is not a count.
    """
    def _count(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None
        return value

    n = _count(touch)
    if n is None or n == 1:
        return DEFAULT_STAGE
    cap = _count(max_touches)
    if cap is not None and cap >= 2 and n >= cap:
        return "final"
    return "reminder"


def _stage_kind(kind: str, stage: str | None) -> str:
    """The section name for ``kind`` at ``stage``: ``message`` -> ``message_reminder``."""
    if not stage or stage == DEFAULT_STAGE or stage not in STAGES:
        return kind
    return f"{kind}_{stage}"

_lock = threading.Lock()
_cache: dict[str, dict] = {}
_incomplete: dict[str, str] = {}
_served: list[str] | None = None


class TemplatesUnavailable(RuntimeError):
    """Raised when the committed copy library is missing or unreadable.

    Deliberately fatal rather than falling back to an inline string. The
    templates ARE the product's voice on this path; a deploy that lost them
    should fail loudly at first use, not quietly mail customers something else.
    """


def path_for(locale: str) -> Path:
    """The catalogue file for a locale. English keeps the original filename."""
    if locale == DEFAULT_LOCALE:
        return TEMPLATES_PATH
    return TEMPLATES_DIR / f"dunning.{locale}.json"


def _incompleteness(doc: dict) -> str | None:
    """Why this document may not serve a locale, or None when it is complete.

    Checked against the ENGLISH catalogue rather than a hardcoded list, so adding a
    failure code to English makes every translation incomplete until it is translated,
    which is the direction that fails safe.
    """
    try:
        english = _raw(DEFAULT_LOCALE)
    except TemplatesUnavailable:
        return None
    for kind in ("diagnosis", "message", "subject"):
        if kind not in doc:
            return f"section {kind!r} missing"
        missing = sorted(set(english.get(kind, {})) - set(doc[kind]))
        if missing:
            return f"{kind}: {len(missing)} code(s) untranslated, first is {missing[0]!r}"
    if "fallback" not in doc:
        return "section 'fallback' missing"
    return None


def _raw(locale: str) -> dict:
    """Read and cache one locale's document with no fallback logic.

    TOLERATES ``_cache`` BEING SET TO None rather than requiring a dict, because
    tests/test_zero_token.py clears it that way and that test's assertion, that a
    deploy which lost the copy library fails loudly, is worth more than the type of
    the thing it pokes. Making the code tolerant beats editing the test: the test
    caught a real regression and its intent is right.
    """
    global _cache
    with _lock:
        if not isinstance(_cache, dict):
            _cache = {}
        if locale not in _cache:
            p = path_for(locale)
            try:
                _cache[locale] = json.loads(p.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise TemplatesUnavailable(
                    f"dunning templates unreadable at {p}"
                ) from exc
        return _cache[locale]


def available_locales() -> list[str]:
    """Locales with a catalogue on disk that is COMPLETE enough to serve.

    A locale whose file exists but is half translated is deliberately absent from
    this list, and :func:`incomplete_locales` says why.
    """
    out, bad = [], {}
    for p in sorted(TEMPLATES_DIR.glob("dunning*.json")):
        name = p.name
        if name.endswith(".draft.json"):
            continue
        loc = DEFAULT_LOCALE if name == "dunning.json" else name[len("dunning."):-len(".json")]
        if not _LOCALE_RE.match(loc):
            continue
        try:
            doc = _raw(loc)
        except TemplatesUnavailable:
            continue
        if loc == DEFAULT_LOCALE:
            out.append(loc)
            continue
        why = _incompleteness(doc)
        if why:
            bad[loc] = why
        else:
            out.append(loc)
    with _lock:
        _incomplete.clear()
        _incomplete.update(bad)
    return out


def incomplete_locales() -> dict[str, str]:
    """Locales that have a file and are not served, mapped to the reason."""
    available_locales()
    return dict(_incomplete)


def _served_cached() -> list[str]:
    """available_locales(), computed once per process until reload() clears it.

    Without this, a non English locale re-globbed the directory and re-read every
    catalogue on every single render.
    """
    global _served
    if _served is None:
        _served = available_locales()
    return _served


def resolve(locale: str | None) -> str:
    """The locale that will actually be used for ``locale``.

    Never raises on a bad input: an unknown, malformed or missing locale resolves to
    English, because the caller's job is to recover a payment rather than to validate
    a language tag. The CALLER is told which locale was used so the substitution is
    visible.
    """
    # SHORT CIRCUIT BEFORE ANY FILESYSTEM WORK. available_locales() globs the
    # directory and reads every catalogue, and this function runs once per payment
    # failure, so the first version of it put disk I/O on the hot path for the
    # overwhelmingly common English case. Nothing above this line touches the disk.
    if not locale:
        return DEFAULT_LOCALE
    loc = str(locale).strip()
    if loc == DEFAULT_LOCALE:
        return DEFAULT_LOCALE
    if not _LOCALE_RE.match(loc):
        return DEFAULT_LOCALE
    served = _served_cached()
    if loc in served:
        return loc
    base = loc.split("-")[0]
    if base in served:
        return base
    return DEFAULT_LOCALE


def load(locale: str | None = DEFAULT_LOCALE) -> dict:
    """Load and cache the template document for a locale, falling back to English."""
    return _raw(resolve(locale))


def reload(locale: str | None = None) -> dict:
    """Drop the cache and re-read from disk (tests, and hot edits in dev)."""
    global _cache, _served
    with _lock:
        _served = None
        if not isinstance(_cache, dict):
            _cache = {}
        if locale is None:
            _cache.clear()
            _incomplete.clear()
        else:
            _cache.pop(locale, None)
    return load(locale or DEFAULT_LOCALE)


def snapshot(locale: str | None = DEFAULT_LOCALE) -> dict:
    """A deep copy of the template document, safe for a caller to mutate.

    :func:`load` returns the live cache for speed on the hot path. Anything
    that hands the document to other code should use this: one caller mutating
    the shared dict would rewrite customer-facing copy process-wide.
    """
    return copy.deepcopy(load(locale))


def _section(kind: str, locale: str | None = DEFAULT_LOCALE) -> dict:
    doc = load(locale)
    if kind not in doc:
        raise TemplatesUnavailable(f"template section '{kind}' missing")
    return doc[kind]


def get(kind: str, failure_code: str, locale: str | None = DEFAULT_LOCALE,
        stage: str | None = None) -> str:
    """Return the raw template for ``kind`` and ``failure_code``.

    ``kind`` is one of ``diagnosis`` / ``message`` / ``subject``. An unknown
    failure code falls back to the documented generic copy rather than raising,
    so a decline reason Stripe has not shown us before still produces a sane
    email instead of a 500.

    ``stage`` selects follow-up copy (see :data:`STAGES`). A locale that carries no copy for
    a stage serves its own FIRST-touch copy for that code instead: a follow-up that reads as
    a first email is a worse email, and a locale refused whole because it lacks follow-ups
    would be a worse regression still. The stage sections are therefore optional in
    :func:`_incompleteness`, and :func:`stages_served` says which locales have them.
    """
    used = resolve(locale)
    staged = _stage_kind(kind, stage)
    if staged != kind and stages_served(used).get(stage, False):
        doc = load(used)
        section = doc.get(staged)
        if isinstance(section, dict):
            if failure_code in section:
                return section[failure_code]
            fb = doc.get("fallback", {}).get(staged)
            if isinstance(fb, str):
                return fb
    section = _section(kind, used)
    if failure_code in section:
        return section[failure_code]
    return _section("fallback", used)[kind]


def stages_served(locale: str | None = DEFAULT_LOCALE) -> dict[str, bool]:
    """Which follow-up stages this locale has COMPLETE copy for (message and subject).

    Complete means every failure code the first-touch section carries, plus the fallback. A
    partial stage is reported False, because serving half a stage would mix follow-up and
    first-touch copy for one customer's sequence by code.
    """
    doc = _raw(resolve(locale))
    out: dict[str, bool] = {}
    for stage in STAGES:
        if stage == DEFAULT_STAGE:
            out[stage] = True
            continue
        ok = True
        for kind in ("message", "subject"):
            section = doc.get(_stage_kind(kind, stage))
            base = doc.get(kind, {})
            if not isinstance(section, dict) or set(base) - set(section):
                ok = False
            fb = doc.get("fallback", {}).get(_stage_kind(kind, stage))
            if not isinstance(fb, str):
                ok = False
        out[stage] = ok
    return out


def render_with_locale(kind: str, failure_code: str,
                       locale: str | None = DEFAULT_LOCALE,
                       stage: str | None = None,
                       **slots: str) -> tuple[str, str]:
    """Render, and say which locale was actually used.

    The audit path uses this rather than :func:`render`, because a fallback that is
    not recorded is indistinguishable from a translation that is working.
    """
    used = resolve(locale)
    return render(kind, failure_code, locale=used, stage=stage, **slots), used


def render(kind: str, failure_code: str, locale: str | None = DEFAULT_LOCALE,
           stage: str | None = None, **slots: str) -> str:
    """Fill a template's slots. Missing slots are left literal, never blanked."""
    template = get(kind, failure_code, locale, stage)
    try:
        return template.format(**slots)
    except KeyError:
        # A template referencing a slot the caller did not supply is a content
        # bug. Returning the unfilled text surfaces it in review and in tests,
        # where a silently blanked name would read as fine.
        return template


def failure_codes(locale: str | None = DEFAULT_LOCALE) -> list[str]:
    """Failure codes the library carries copy for."""
    return sorted(_section("message", locale))
