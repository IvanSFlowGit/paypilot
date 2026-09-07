#!/bin/bash
# Start the demo dashboard server. Run it once, after record-demo.sh has run at
# least once, and leave it up:  bash dashboard.sh
#
# Reads the same ledger record-demo.sh writes, so a browser on
# http://127.0.0.1:8010/report reflects each take live. ADMIN_TOKEN is left
# empty on purpose so a browser (which sends no bearer) can open /report.
set -euo pipefail

cd "$(dirname "$0")"

ADMIN_TOKEN= PAYPILOT_DB_PATH=data/demo-loop.db \
  .venv/bin/python -m uvicorn app.api:app --port 8010
