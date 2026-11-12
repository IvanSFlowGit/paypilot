#!/usr/bin/env bash
# Sandbox only. Fires Unit's "Return ACH repayment" simulation for one repayment id.
# Unit then delivers a payment.returned event to the webhook. That is the repayment
# path, NOT a returned originated debit, so it proves delivery, signature and parsing
# and does not prove the shape of a real returned debit.
# Source of the endpoint: https://www.unit.co/docs/api/repayments/simulations/ (read 2026-11-11).
# The repayment must be in Clearing or Sent status. If it is Pending run the transmit
# simulation first (same URL with /transmit, data.type transmitAchRepayment).
# The token comes from the environment and is never printed.
set -u
if [ "$#" -ne 1 ] || [ -z "${UNIT_API_TOKEN:-}" ]; then
  echo "usage: UNIT_API_TOKEN set in the environment, one argument: the repayment id" >&2
  exit 2
fi
body=$(printf '{"data":{"type":"returnAchRepayment","relationships":{"repayment":{"data":{"type":"achRepayment","id":"%s"}}}}}' "$1")
out=$(curl -sS -m 15 -w '\nHTTP %{http_code}\n' -X POST "https://api.s.unit.sh/sandbox/ach-repayments/return" \
  -H "Content-Type: application/vnd.api+json" \
  -H "Authorization: Bearer ${UNIT_API_TOKEN}" \
  -d "$body")
rc=$?
if [ "$rc" -ne 0 ]; then echo "CANNOT RUN: curl exit $rc, nothing was sent or the answer was not read" >&2; exit 2; fi
printf '%s\n' "$out" | python3 -c '
import sys, json
t = sys.stdin.read()
body, _, code = t.rpartition("\nHTTP ")
print("HTTP", code.strip())
try:
    d = json.loads(body)["data"]["attributes"]
    print("status:", d.get("status"), " reason:", d.get("reason"))
except Exception:
    print(body[:400])
'
