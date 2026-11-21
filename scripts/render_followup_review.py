"""Print every follow-up email as a customer would read it, one page for a human to approve.

    python scripts/render_followup_review.py > review.txt

Every failure code at every stage (first, reminder, final), with the slots filled by a sample name
and plan, plus the subject and word count. Nothing here decides anything: a person reads it and
either approves data/templates/dunning.json or edits it. Exit 0 always, so it cannot be mistaken
for a gate; the gates are the tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import templates  # noqa: E402


def main(out=sys.stdout) -> int:
    codes = templates.failure_codes() + ["(unknown code, fallback)"]
    for code in codes:
        key = "never_seen_before" if code.startswith("(") else code
        print("=" * 78, file=out)
        print(code, file=out)
        for stage in templates.STAGES:
            subject = templates.get("subject", key, stage=stage)
            body = templates.render("message", key, stage=stage, name="Dana Smith", plan="Pro",
                                    business="Northwind")
            print(f"\n--- {stage.upper()} ({len(body.split())} words)\nSubject: {subject}\n\n{body}", file=out)
        print(file=out)
    print("Read each email as the customer. Approve, or edit data/templates/dunning.json and rerun.", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
