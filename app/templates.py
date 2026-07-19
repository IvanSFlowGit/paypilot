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

import json
import threading
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_PATH = _REPO_ROOT / "data" / "templates" / "dunning.json"

_lock = threading.Lock()
_cache: dict | None = None


class TemplatesUnavailable(RuntimeError):
    """Raised when the committed copy library is missing or unreadable.

    Deliberately fatal rather than falling back to an inline string. The
    templates ARE the product's voice on this path; a deploy that lost them
    should fail loudly at first use, not quietly mail customers something else.
    """


def load() -> dict:
    """Load and cache the template document."""
    global _cache
    with _lock:
        if _cache is None:
            try:
                _cache = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise TemplatesUnavailable(
                    f"dunning templates unreadable at {TEMPLATES_PATH}"
                ) from exc
        return _cache


def reload() -> dict:
    """Drop the cache and re-read from disk (tests, and hot edits in dev)."""
    global _cache
    with _lock:
        _cache = None
    return load()


def _section(kind: str) -> dict:
    doc = load()
    if kind not in doc:
        raise TemplatesUnavailable(f"template section '{kind}' missing")
    return doc[kind]


def get(kind: str, failure_code: str) -> str:
    """Return the raw template for ``kind`` and ``failure_code``.

    ``kind`` is one of ``diagnosis`` / ``message`` / ``subject``. An unknown
    failure code falls back to the documented generic copy rather than raising,
    so a decline reason Stripe has not shown us before still produces a sane
    email instead of a 500.
    """
    section = _section(kind)
    if failure_code in section:
        return section[failure_code]
    return _section("fallback")[kind]


def render(kind: str, failure_code: str, **slots: str) -> str:
    """Fill a template's slots. Missing slots are left literal, never blanked."""
    template = get(kind, failure_code)
    try:
        return template.format(**slots)
    except KeyError:
        # A template referencing a slot the caller did not supply is a content
        # bug. Returning the unfilled text surfaces it in review and in tests,
        # where a silently blanked name would read as fine.
        return template


def failure_codes() -> list[str]:
    """Failure codes the library carries copy for."""
    return sorted(_section("message"))
