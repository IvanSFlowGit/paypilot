#!/bin/bash
# One command for the demo recording. No fragile paste, no env exports to get
# wrong. Run it from anywhere:  bash record-demo.sh
#
# It clears the demo ledger IN PLACE (not by deleting the file), then drives the
# real Stripe test-mode cycle and prints the dashboard before and after. Keeping
# the same file means a dashboard server already reading it sees this run live,
# instead of holding a stale copy of a deleted file. The email and all keys come
# from .env, which the app loads itself.
set -euo pipefail

cd "$(dirname "$0")"

DB="data/demo-loop.db"

# The demo customer needs an email. app/__init__ loads .env at import, so the
# script's own --email default already sees PAYPILOT_DEMO_EMAIL; this line only
# fails loud and early if .env is missing it, rather than deep inside Stripe.
EMAIL="$(awk -F= '/^PAYPILOT_DEMO_EMAIL=/{print $2}' .env | tr -d '"'\'' \r')"
if [ -z "$EMAIL" ]; then
  echo "PAYPILOT_DEMO_EMAIL is not set in .env" >&2
  exit 1
fi

# Empty the ledger without unlinking it, so a running dashboard server (which
# holds one long-lived connection) reflects this run instead of a deleted inode.
PAYPILOT_DB_PATH="$DB" .venv/bin/python - <<'PY'
import os
from app.store import Store
db = os.environ["PAYPILOT_DB_PATH"]
store = Store(db)
for table in ("transitions", "messages", "events", "failures"):
    try:
        store._conn.execute(f"DELETE FROM {table}")
    except Exception:
        pass
store._conn.commit()
store.close()
PY

clear

PAYPILOT_DB_PATH="$DB" .venv/bin/python scripts/demo_loop.py --email "$EMAIL"
