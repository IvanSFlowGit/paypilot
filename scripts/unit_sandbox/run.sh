#!/bin/sh
# Runs PayPilot locally for the Unit sandbox webhook test. Reads the webhook token from the macOS keychain (see token.sh); it is never printed or stored in a file.
cd "$(dirname "$0")/../.." || { echo "FAILED: paypilot folder not found"; exit 2; }
test -x .venv/bin/uvicorn || { echo "FAILED: .venv/bin/uvicorn missing"; exit 2; }
UNIT_WEBHOOK_SECRET=$(security find-generic-password -s paypilot-unit-webhook -w 2>/dev/null)
test -n "$UNIT_WEBHOOK_SECRET" || { echo "FAILED: no token in keychain. Run scripts/unit_sandbox/token.sh first"; exit 2; }
export UNIT_WEBHOOK_SECRET
# Fresh file: the default DB is a pre-tenancy one PayPilot refuses to open, and we never migrate it.
export PAYPILOT_DB_PATH="$HOME/.paypilot-unit-demo.db"
# The demo page only serves its sample customers to the demo tenant.
export PAYPILOT_TENANT=demo
# Decision records survive restarts: a file instead of memory.
export PAYPILOT_DECISION_AUDIT_DB_PATH="$HOME/.paypilot-unit-decisions.db"
exec .venv/bin/uvicorn app.api:app --port 8000
