"""Hold the dunning sequence for every invoice whose customer has replied, read from a mailbox.

Read-only on the mailbox (same adapters and credentials as mailbox_facts_export.py). Run it on a
schedule shorter than your send cooldown. It writes only the reply hold, in PayPilot's own ledger.

    python scripts/watch_replies.py --provider gmail --ours billing@yourco.test --days 14

Exit 0 scan complete. Exit 2 the mailbox could not be read in full: NOTHING was concluded, and a
silent mailbox is never read as "no replies". Exit 1 bad arguments or missing credentials.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from mailbox_facts_export import _reader  # noqa: E402

from app import reply_watch  # noqa: E402
from app.store import get_store  # noqa: E402


def main(argv: list[str], out=sys.stdout, reader=None, store=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--provider", choices=("imap", "gmail", "graph"), default="imap")
    ap.add_argument("--folder", action="append", default=[])
    ap.add_argument("--ours", action="append", required=True)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true",
                    help="read the mailbox and report what WOULD be held; writes nothing. "
                         "Run this first against a real mailbox to prove credentials and matching.")
    ns = ap.parse_args(argv)
    try:
        read = reader or _reader(ns.provider, ns.folder or ["INBOX"], None)
        result = read(date.today() - timedelta(days=ns.days))
    except Exception as exc:  # noqa: BLE001 - a script for a person: say what failed, never a traceback
        print(f"could not read the mailbox: {type(exc).__name__}: {exc}", file=out)
        return 1
    if not result.complete:
        print(f"mailbox read incomplete (failed={list(result.folders_failed)}, truncated={result.truncated}); "
              "nothing concluded", file=out)
        if ns.provider == "imap" and reader is None:
            from app.mailbox_reader import imap_probe

            print("why: " + imap_probe(), file=out)
        return 2
    if store is None:
        store = get_store()
    if ns.dry_run:
        # Scan a throwaway copy of the decision, not the ledger: count candidates without writing.
        import copy

        class _Dry:
            def __getattr__(self, name):
                return getattr(store, name)

            def mark_customer_replied(self, *a, **k):
                return 0

        counts = reply_watch.scan(copy.copy(result.messages), _Dry(), set(ns.ours))
        print(f"DRY RUN, nothing written. read {len(result.messages)} messages. "
              "would hold: {held}, already held: {already}, ignored: {ignored}, "
              "unrelated mail: {unmatched}".format(**counts), file=out)
        return 0
    counts = reply_watch.scan(result.messages, store, set(ns.ours))
    print("replies held: {held}, already held: {already}, ignored: {ignored}, "
          "unrelated mail: {unmatched}".format(**counts), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
