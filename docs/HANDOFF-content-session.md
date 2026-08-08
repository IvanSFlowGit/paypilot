# HANDOFF: publish the PayPilot load test

Written 2026-08-08. For a content session, not an engineering one. The
engineering is finished and measured; what is left is publication.

## Read this first

Nothing in this file is a to-do list you should trust on sight. Canon rule: a
handoff is the highest-risk artefact because it is written to be read later.
Before acting on any line below, check the current state of the thing it names.
Anything already done is marked EXECUTED with its date.

## What already exists

- **`docs/loadtest-2026-08-08.md`** EXECUTED 2026-08-08. The full technical
  write-up, 1923 words. Gated: zero non-ASCII, zero em or en dashes, no retired
  numbers. **This is the source of truth. Do not re-derive any number; quote it.**
- **`scripts/loadtest.py`** EXECUTED. The load driver, with a module docstring
  explaining the design decisions.
- **`tests/test_loadtest.py`** EXECUTED. 18 tests. Each integrity probe has a
  test proving it can fail.
- **Raw results**: `data/loadtest-run1.json`, `data/peel-A-disk-wal.json`,
  `data/peel-B-in-memory.json`, `data/fix-4workers.json`. Every figure in the
  write-up traces to one of these.
- Suite is at **745 passing**. Public count claims in README, the landing page
  and `llms.txt` were updated to match, because `tests/test_mock_and_security.py`
  machine-enforces them. If you add tests, that guard fails until you update all
  three surfaces.

## The job

Three deliverables, in this order.

**1. Publish the write-up so it has a URL.** It is currently a file in a private
repo, which is worth nothing to a stranger. The whole point of this artefact is
that someone who has never met Ivan can evaluate it cold. Decide where it lives
and ship it. Options in preference order: the PayPilot site itself (it already
serves static pages and has AEO infrastructure), a GitHub gist or public repo,
or the Streamflow blog. It needs a stable public URL by the end of the session.

**2. A LinkedIn post that points at it.** The post is the trailer, not the film.
Do not put the full argument in the post.

**3. At least one inline infographic** if this goes on the blog. Canon: every
blog or journal post ships with a self-contained SVG using real numbers from the
post. The throughput curve is the obvious one, and it is genuinely a good shape:
it rises, peaks at 4 concurrent, and falls off a cliff. Two lines on one chart,
one worker against four workers, tells the whole story without a caption.

## The angle, and why it is this one

Do not write "I load tested my API". Write:

> **I put my own AI agent under load and the observability held, but two of my
> own instruments were wrong before the system was.**

Two reasons that framing is the right one.

**It is the half nobody measures.** Every perf write-up answers "how fast". This
one also answers "could you still see what the agent did", which is the question
agent-governance companies exist to sell into. It makes the piece legible to a
specific audience rather than being one more benchmark post.

**The wrong turns are the credibility.** A percentile function that returned 6 as
the median of 1 to 10. An integrity probe that reported five lost audit trails,
all five of which were correct behaviour. Nobody rehearses their own mistakes,
which is exactly why they land. Do not edit them out to make the piece cleaner.
They are the piece.

## Claim ceiling. Do not exceed these.

The write-up is the ceiling. Nothing downstream may be more specific than it.

**Can say:**
- Throughput peaked at 414 rps at 4 concurrent and declined to 140 at 64.
- Removing the filesystem entirely changed nothing at the collapse point
  (136.6 rps on disk against 133.7 in memory at 64 concurrent).
- Four workers bought 39 percent, not 400 percent.
- The audit trail held at every concurrency on one worker and on four.
- The baseline reproduced across separate runs inside 3 percent.

**Cannot say:**
- That SQLite's writer lock is the remaining bottleneck. It is consistent with
  the evidence and it is **not proved**. The write-up says so; keep that hedge.
- Any test count in outward-facing copy. All PayPilot test counts are retired
  (`~/Documents/CVWORK/retired-numbers.md`). Approved wording is "with a test and
  evaluation suite in CI". The 745 figure is for the repo's own guard, not for
  copy.
- Anything about a model provider's latency. The model was mocked. The write-up
  states this; do not let an edit drop it.
- That this is production hardened, battle tested, or any similar phrase.

## Mechanical gates before anything ships

- Zero em dashes (U+2014) and zero en dashes (U+2013). Plain hyphens only. Check
  the **escaped** forms too: `json.dumps(ensure_ascii=True)` writes an em dash as
  `—`, so a grep of visible text passes while JSON-LD still carries it.
- Run `node check_message_text.js --claim` over the post text before sending. It
  covers non-outreach surfaces.
- Grep the final rendered artefact, not the draft. Canon: check the assembled
  artefact, not its parts.
- If it ships to the PayPilot site, verify in devtools that it rendered, and
  confirm the canonical URL returns 200 rather than redirecting.

## Voice

Billionaire style. Conviction about mechanism, precision about measurement.
Short declaratives. Mechanism over benefit. No selling in the body. Name the
limit. If a sentence could appear in any agency's blog, cut it.

Banned: revolutionary, game changing, seamless, unlock, leverage, robust,
cutting edge, effortless, supercharge.

## Where this is going, so you know what it is for

This artefact is load-bearing for four live threads, which is why it matters more
than it looks:

1. **Geordie AI.** Applied 2026-08-08, Joel Furniss messaged the same day. This
   is touch two, and touch two is meant to be an artefact rather than another
   message. Their product is agent visibility and governance; the observability
   half of this write-up is aimed straight at it.
2. **Stability AI, London.** Their Senior Research Engineer posting asks for a
   paper, model card or open-source link as part of the application. Ivan has
   none, which is the only reason that role was marked skip. A published
   technical write-up may change that. Re-check the posting after publishing.
3. **Mistral and Poolside.** Both approaches already reference "I load tested it
   this week and published what broke". Those messages are out. The link needs
   to exist.
4. **Every interview from here.** It closes the "how does this scale" question,
   which previously could only be answered by reasoning.

## What NOT to do

- Do not open the load driver or the test file to "improve" them. The engineering
  is done and gated.
- Do not re-run the load test to get fresher numbers. The numbers in the
  write-up were measured on 2026-08-08 and carry their commands. Re-running
  produces different numbers and orphans every quote already sent.
- Do not publish anything under a byline other than Ivan's.
- Do not auto-publish. Draft first, Ivan reviews, Ivan ships.

## Open, and genuinely open

- Where to host is not decided. That is a real decision, not a formality: the
  PayPilot site gets AEO benefit and ties the artefact to the product, a gist is
  faster and more credible to engineers, the blog reaches the Streamflow
  audience which is the wrong audience for this.
- The infographic has not been started.
- `app/static/index.html:208` fails the repo style gate with a pre-existing
  comment in a public asset. Not caused by this work. Fix it or leave it, but
  know the gate is currently red for that one line.
