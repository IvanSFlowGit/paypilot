# Claims register

Every load-bearing claim this repository makes about a system it does not
control, with how it was established and when. A row saying we do not know is
worth more than silence, because silence reads as nothing-to-check.

Three states, never two. **MEASURED** means somebody ran the command in the
re-check column and read the result. **RELAYED** means a second-hand source
said it and nobody has opened the primary. **NEVER CHECKED** means exactly
that, and it is written down rather than left out.

The re-check column is the load-bearing half. A row whose re-check has never
been run is an open task wearing the clothes of a closed record.

| # | Claim | State | Established | Re-check |
|---|-------|-------|-------------|----------|
| 1 | There is no first-hand price for the configured default model (`gpt-4o-mini`), so `app/meter.py` ships an EMPTY price table and reports `unpriced_calls` rather than costing a call it cannot price. | MEASURED | 2026-10-16. `https://platform.openai.com/docs/pricing` fetched with curl following the 301: HTTP 200, 597,532 bytes, 36,458 characters of visible text after stripping script and tags. Control fired: the word pricing is present and twelve dollar figures were extracted. The served text carries transcription and tool rows (`gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, web search per 1k calls) and **zero** occurrences of `Text tokens`; the text-model row for the default model is not in what was served. The page is client-rendered, so the bytes arriving is not the row arriving. | `curl -sL https://platform.openai.com/docs/pricing`, strip tags, and grep for the model id within 180 characters of a `$`. If the row appears, add it to `PAYPILOT_MODEL_PRICES` **with its source and read date**, which the loader requires. |
| 2 | The budget ceiling cannot be certified while any call is unpriced. `CostMeter.verdict()` returns `unknown` rather than `within`, and the measured spend is labelled a floor. | MEASURED | 2026-10-16, by construction and by test. `tests/test_meter.py::test_an_unpriced_call_makes_the_verdict_unknown_and_the_total_a_floor`. Mutating `cost_of` to return `0.0` for an unpriced model reds three rows. | `.venv/bin/python -m pytest tests/test_meter.py -q` |
| 3 | The latency percentiles in `/metrics` describe the last `LATENCY_WINDOW` requests and `avg_latency_ms` describes every request since boot. They are two populations and the keys say which. | MEASURED | 2026-10-16. `tests/test_meter.py::test_the_window_is_bounded_and_forgets_the_oldest` and `::test_the_published_metrics_payload_keeps_its_old_key_and_gains_the_new_blocks`. | `.venv/bin/python -c "from app.api import metrics; print(metrics())"` |
| 4 | The inference spend total survives a restart because it is re-derived from the append-only `llm_call` audit rows, not read from a snapshot. | MEASURED | 2026-10-16. `tests/test_meter.py::test_totals_are_re_derived_from_the_durable_audit_rows`, including that a reload does not double the total and that a legacy row carrying no token fields is skipped rather than folded in as a zero-token call. Depends on `input_tokens`, `output_tokens` and `cost_usd` being in `LOG_SAFE_FIELDS`: the first version of the meter wrote all three and `safe_log_fields` silently dropped them, so the restore would have reported zero. | Mutation: remove those three keys from `app/pii.py` and the durability test must red. |
| 5 | Neither of the two decision-API outcome routes has been exercised against a deployed URL. In-process agreement between the FastAPI and Lambda targets is what has been measured. | NEVER CHECKED | 2026-10-16. The contract suite has a `live` target and it is collected only when `PAYPILOT_CONTRACT_BASE_URL` and `PAYPILOT_CONTRACT_TOKEN` are set. Neither was set in any run here. | `PAYPILOT_CONTRACT_BASE_URL=... PAYPILOT_CONTRACT_TOKEN=... .venv/bin/python -m pytest tests/test_decision_contract.py -k live` |
