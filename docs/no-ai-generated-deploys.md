# What is deployed

Nothing that reads as AI-generated is deployed, and nothing is deployed that a person has
not read. Both are checked by `scripts/check_ai_tells.py`, which runs in CI before the tests
and before the deploy step, and in `make deploy`.

## Gate 1: tells

A pattern scan of the public surface (the HTML pages, `llms.txt`, the stylesheet and the
script). It refuses:

- dashes, including the entities that render to one, and emoji;
- filler vocabulary (seamless, unlock, leverage, robust, and similar);
- negative-parallelism sentences ("it's not just X, it's Y"), pseudo-cleft headings and
  sales closers;
- the visual habits of generated pages: gradient text, gradients, glow shadows, frosted
  blur, pill badges and decorative animation.
- text colours under 4.5:1 contrast on the white pages, including a `var()` fallback colour.
  It reads literal hex values only and skips a rule that sets its own background.

It reads text nodes, alt text, `aria-label`, meta descriptions and the strings inside
JSON-LD, because answer engines read those as prose.

Pages built in Python are not files on disk, so the gate cannot scan them directly. The
recovery report (`app/report.py`) and the approval page are rendered inside the test suite
and written through the same scan, and a further test checks by computed contrast that the
report's background is light, its text dark and its muted text readable. The report once
kept its own dark palette inline and stayed dark after every static page was plain; the
gate had never looked at it.

A match is a candidate for a person to judge, not proof of authorship. It cannot tell
whether a sentence is true, plain or says anything.

## Gate 2: a person has read it

`docs/human-review.json` records the sha256 of every public file as a named person last
read it. A file that changed since, was never recorded, or no longer exists fails. After
reading the files, in your own terminal:

    python3 scripts/check_ai_tells.py --attest

It lists the files and asks for the word READ. It refuses to run without a terminal, so a
coding session or a CI job cannot attest for anyone. The deploy job stops before
`flyctl deploy` until the record matches.

## What it does not cover

- **Source code.** Nothing here can tell who wrote a function. Code is held by tests,
  mutation checks and an independent review (`docs/security.md`), not by authorship.
- **Documents outside the public surface**, such as the README and the docs folder.
- **Truth.** A gate that passes says the page has none of the listed habits and that a
  person read it. It does not say the claims are right.
