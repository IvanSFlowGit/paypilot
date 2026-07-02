"""Rubric-based quality scoring for generated text.

Two judges, one :class:`Rubric`:

* **heuristic** (default) - each :class:`Criterion` carries a ``check`` function
  that returns True/False from the text alone. Deterministic, offline, free, and
  stable in CI. This is what runs unless you opt into the live judge.
* **live** - sends the same criteria (their ``description``) to an
  OpenAI-compatible chat endpoint (Groq, Gemini's OpenAI-compat URL, or OpenAI)
  and asks for a JSON verdict per criterion. Opt in with ``EVAL_JUDGE=live``.

Design choices worth knowing:

* No third-party HTTP dependency - the live call uses :mod:`urllib.request`, so
  ``evalkit`` stays copy-pasteable into any repo with zero installs.
* The live judge NEVER runs by default. Nondeterministic model output has no
  place in a normal test run; you turn it on deliberately (locally or in a
  dedicated nightly job) via ``EVAL_JUDGE=live`` plus a key.
* Both judges return the same :class:`JudgeResult`, so a test asserts on
  ``result.passed`` / ``result.score`` without caring which judge ran.

Environment (live judge only):
    EVAL_JUDGE          "heuristic" (default) | "live"
    EVAL_JUDGE_KEY      api key; falls back to GROQ_API_KEY / OPENAI_API_KEY
    EVAL_JUDGE_BASE_URL default "https://api.groq.com/openai/v1"
    EVAL_JUDGE_MODEL    default "llama-3.3-70b-versatile"
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Criterion:
    """One thing the text must do, checkable two ways.

    Parameters
    ----------
    key:
        Short stable identifier, used in reasons and live-judge JSON.
    description:
        Plain-English requirement. This is the text the live judge reads, so
        write it as an instruction ("The email reassures the customer their
        service is still active").
    check:
        Deterministic predicate over the (text, context) pair for the heuristic
        judge. Return True when the criterion is satisfied.
    weight:
        Relative importance in the aggregate score. Defaults to 1.0.
    """

    key: str
    description: str
    check: Callable[[str, dict], bool]
    weight: float = 1.0


@dataclass
class Rubric:
    """A weighted set of criteria plus the pass threshold.

    ``threshold`` is the minimum weighted score (0..1) for :attr:`JudgeResult.passed`
    to be True. 0.8 means "at least 80% of the weighted criteria must hold".
    """

    name: str
    criteria: list[Criterion]
    threshold: float = 0.8


@dataclass
class JudgeResult:
    """Outcome of judging one text against one rubric."""

    score: float                       # weighted fraction satisfied, 0..1
    passed: bool                       # score >= rubric.threshold
    mode: str                          # "heuristic" | "live"
    reasons: list[str] = field(default_factory=list)   # one line per criterion
    per_criterion: dict[str, bool] = field(default_factory=dict)


def judge(text: str, rubric: Rubric, context: Optional[dict] = None) -> JudgeResult:
    """Judge ``text`` against ``rubric``; pick the judge from the environment."""
    context = context or {}
    if _live_enabled():
        try:
            return _judge_live(text, rubric, context)
        except Exception as exc:  # live judge must never break a run; degrade.
            result = _judge_heuristic(text, rubric, context)
            result.reasons.append(f"[live judge unavailable: {exc}; used heuristic]")
            return result
    return _judge_heuristic(text, rubric, context)


# ---------------------------------------------------------------------------
# Heuristic judge
# ---------------------------------------------------------------------------

def _judge_heuristic(text: str, rubric: Rubric, context: dict) -> JudgeResult:
    total = sum(c.weight for c in rubric.criteria) or 1.0
    got = 0.0
    reasons: list[str] = []
    per: dict[str, bool] = {}
    for c in rubric.criteria:
        try:
            ok = bool(c.check(text, context))
        except Exception as exc:
            ok = False
            reasons.append(f"{c.key}: check errored ({exc})")
        per[c.key] = ok
        if ok:
            got += c.weight
        else:
            reasons.append(f"{c.key}: {c.description}")
    score = got / total
    return JudgeResult(
        score=round(score, 4),
        passed=score >= rubric.threshold,
        mode="heuristic",
        reasons=reasons,
        per_criterion=per,
    )


# ---------------------------------------------------------------------------
# Live judge (OpenAI-compatible chat completions)
# ---------------------------------------------------------------------------

def _live_enabled() -> bool:
    return os.getenv("EVAL_JUDGE", "heuristic").strip().lower() == "live"


def _judge_key() -> str:
    for name in ("EVAL_JUDGE_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        v = os.getenv(name)
        if v:
            return v.strip()
    raise RuntimeError("no judge API key (set EVAL_JUDGE_KEY / GROQ_API_KEY / OPENAI_API_KEY)")


def _judge_live(text: str, rubric: Rubric, context: dict) -> JudgeResult:
    key = _judge_key()
    base = os.getenv("EVAL_JUDGE_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    model = os.getenv("EVAL_JUDGE_MODEL", "llama-3.3-70b-versatile")

    criteria_lines = "\n".join(f"- {c.key}: {c.description}" for c in rubric.criteria)
    system = (
        "You are a strict evaluation judge. Given a TEXT and a list of CRITERIA, "
        "decide for each criterion whether the text satisfies it. Respond with a "
        'single JSON object: {"results": [{"key": <key>, "pass": <bool>, '
        '"reason": <short string>}]}. No prose outside the JSON.'
    )
    user = (
        f"CONTEXT: {json.dumps(context, default=str)}\n\n"
        f"CRITERIA:\n{criteria_lines}\n\n"
        f"TEXT:\n{text}"
    )
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    parsed = json.loads(content)

    verdicts = {r["key"]: r for r in parsed.get("results", [])}
    total = sum(c.weight for c in rubric.criteria) or 1.0
    got = 0.0
    reasons: list[str] = []
    per: dict[str, bool] = {}
    for c in rubric.criteria:
        v = verdicts.get(c.key, {})
        ok = bool(v.get("pass", False))
        per[c.key] = ok
        if ok:
            got += c.weight
        else:
            reasons.append(f"{c.key}: {v.get('reason', c.description)}")
    score = got / total
    return JudgeResult(
        score=round(score, 4),
        passed=score >= rubric.threshold,
        mode="live",
        reasons=reasons,
        per_criterion=per,
    )
