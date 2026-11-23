# Security review

What was checked, what was found, what was fixed, and what was not covered. Read
`docs/what-paypilot-touches.md` first for what the application can reach.

## How it was done

- **Independent read.** A reviewer with read-only access and no knowledge of the drafting
  was asked to break the new modules: the customer-reply reader, the payment-in-flight
  check, the accounting read guards and mappers, the public reply demo route and the bank
  transaction parser. It could not run code, so its findings were reproduced against the
  real code before any fix. Each of the 13 findings was reproduced or refuted first.
- **Secret scan.** `gitleaks` over the full git history. Two hits, both fake values inside
  tests that prove secrets are not leaked; allowlisted by path in `.gitleaks.toml`.
- **Mutation testing.** Every new guard was broken on purpose and the tests were required
  to go red. One guard, the `fullmatch` on the QuickBooks path, stayed green when broken
  because a second check (control characters) refuses the same input first. It is kept as
  depth.

## Findings and what was done

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | High | A credit of one cent carrying the invoice number held the chase | Only a credit at or above the invoice amount holds. A smaller one is `partial_credit`, which goes to a person |
| 2 | High | A longer or unrelated reference (`INV-1001-2`, `PO 1001`) held this invoice | Whole-token match across every character an id may contain; a short or all-digit id also needs the amount to match |
| 3 | Medium | An unverified "I have already paid" paused reminders with no person involved | Always routed to a person with the chase held |
| 4 | Medium | A bank line with no id got a positional id, so a reordered pending credit renewed its hold forever | Id derived from the line's content |
| 5, 6 | Medium, Low | Two patterns slow on hostile input (measured 261 ms and 134 ms) | Rewritten; now under 5 ms |
| 7 | Medium | `25,00 EUR` read as 2,500; `1.234,50 EUR` as 23,450; `USD 2,500` matched a EUR invoice | Strict thousands grouping; any ambiguous form or other currency reads as nothing and goes to a person |
| 8, 9, 10 | Low | QuickBooks path accepted a trailing newline; the query could carry `&`, `%`, `#`; a `//host` path passed | Refused, with tests |
| 11 | Low | Unicode line breaks and bidi controls passed the control-character filter | Refused in the library and at the API |
| 12 | Low | The public demo read an oversize body before limiting | Rate limit and a 16 KB cap now run before the body is read; a request with no length is refused |
| 13 | Low | Malformed bank JSON raised errors other than `ProviderError`, and `+-500.00` read as a credit | Strict amount format; any other shape raises `ProviderError` |

Also fixed during the audit: the page's policy allowed inline scripts, which weakens its
protection against script injection. The one inline script moved to `/static/paypilot.js`,
its inline handlers became a delegated listener, and the policy now allows scripts from
the same origin only. A test fails if any served page regains an inline script or handler.

## Where data goes

Read from the code on 2026-11-23, not from a vendor statement.

- **Default path: no model.** `get_llm()` returns the committed copy library unless
  `PAYPILOT_LLM_DRAFT=1` and an `OPENAI_API_KEY` are both set. The reply reader and the
  decision step never call a model.
- **Live drafting on:** the prompt is a wording task with names and addresses masked,
  sent through `ChatOpenAI` to OpenAI's API. No base URL or region is set in the code.
- **A key alone:** the retriever embeds the static playbook once and, per request, a query of
  the form `failure reason <code> dunning strategy for <plan> plan`, sent to OpenAI's
  embeddings API. The plan field passes the same personal-data gate as the prompt.
- **Not established:** which region OpenAI serves these requests from, and whether the
  public demo has a key set as a Fly secret. `fly.toml` says it runs in mock mode by default.
- **So there is no EU-only guarantee.** The app runs in the London region on Fly.io
  (`primary_region = "lhr"`). A deployment that must keep data in the EU leaves both
  switches off, which loses nothing on the default path.

## What was not covered

- **No external penetration test.**
- **Dependency advisories**: `pip-audit -r requirements.txt` found no known vulnerabilities on
  2026-11-23, run once by hand against PyPI's advisory data. That is a point-in-time result,
  not a guarantee. `.github/workflows/security.yml` repeats the check and runs `gitleaks`,
  but it has not run on GitHub yet.
- **The policy still allows inline styles**, because every page carries a `<style>` block
  and style attributes. Inline styles cannot run script, but the policy is weaker than it
  could be. The API documentation pages keep `unsafe-inline` for scripts because Swagger UI
  needs it.
- **The reviewer could not execute code**, so findings came from reading. Anything it did
  not read was not reviewed. It read the five modules named above and their direct
  dependencies, not the rest of the application.
- **No real-browser run under the new policy.** The script was loaded in a browser from a
  file URL and executed; the served policy was checked by test. The sandbox could not bind
  a port, so the page was not loaded from the running server.
- **The reader has only run on invented messages.** Its phrase lists are a starting set
  and a real customer will phrase things it does not catch. Unrecognised wording goes to a
  person, which is the safe direction, but recall is untested.
- **Accounting access** is read-only by the vendor's own scope for Xero and Sage, and by
  PayPilot's client guard only for QuickBooks. No live call has been made to any of them.
- **Rent and arrears law** varies by jurisdiction and tenancy. None of it is encoded.
