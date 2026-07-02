# Evaluation harness

Custom eval harness for PayPilot's agent output. Three layers, one runner
(pytest). Runs offline, free, and deterministic by default - no API key, no
network - because it exercises PayPilot's built-in mock mode.

## Layout

```
evals/
  evalkit/          # portable, project-agnostic core (copy this into any repo)
    judge.py        # rubric scoring: heuristic (default) or live LLM judge
    guardrails.py   # deterministic golden-rule checks (no em-dash, etc.)
    goldenset.py    # regression snapshots of deterministic output
  cases/
    dunning_cases.py  # PayPilot-specific events, rubrics, guards, snapshot dir
    snapshots/        # committed regression baselines
  conftest.py       # forces mock mode (clears OPENAI_API_KEY)
  test_quality.py   # judge message + diagnosis against rubrics
  test_guardrails.py# golden-rule checks on generated text
  test_regression.py# snapshot the risk/strategy/impact decision fields
```

## Run

```bash
.venv/bin/python -m pytest evals -q          # all evals (offline, free)
EVAL_UPDATE_SNAPSHOTS=1 pytest evals          # re-baseline after intended changes
EVAL_JUDGE=live GROQ_API_KEY=... pytest evals # score quality with a real LLM judge
```

The quality judge defaults to a deterministic heuristic so CI stays green and
free. Opt into the live judge (Groq / Gemini OpenAI-compat / OpenAI) only in a
dedicated job; set `EVAL_JUDGE_BASE_URL` / `EVAL_JUDGE_MODEL` to switch provider.

## What each layer catches

- **Quality** - the LLM `message` and `diagnosis` stay on-rubric (greets by name,
  names the plan, reassures, one CTA, warm sign-off; diagnosis is grounded +
  actionable).
- **Guardrails** - no em-dash/en-dash, no unfilled `{placeholders}`, sane length,
  no build-mechanic disclosure in customer copy, no AI tells.
- **Regression** - the deterministic decision fields (risk / strategy / impact)
  never drift silently; a broken node fails the snapshot.

## Reuse in another project

Copy `evals/evalkit/` verbatim. Add a project `cases/` module defining your
inputs, `Rubric`s, and guardrail lists, then thin test files that run your
pipeline and call `judge()` / `check_guardrails()` / `assert_snapshot()`.
`evalkit` has zero third-party dependencies.
