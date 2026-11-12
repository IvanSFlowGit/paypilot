#!/bin/sh
# SYNTHETIC test only: posts a signed made-up payment.returned through the public tunnel, twice.
# Proves tunnel + signature + parse + decision + audit write + dedupe. Does NOT prove Unit's real return payload.
BASE=$(cat "$HOME/.paypilot-tunnel-url" 2>/dev/null)
test -n "$BASE" || { echo "FAILED: no tunnel address. Start ~/unitkit/named_tunnel.sh run first"; exit 2; }
URL="$BASE/webhooks/unit"
SECRET=$(security find-generic-password -s paypilot-unit-webhook -w 2>/dev/null)
test -n "$SECRET" || { echo "FAILED: no token in keychain. Run ~/unitkit/token.sh first"; exit 2; }
PID="synthetic-$(date +%s)"
BODY="{\"data\":[{\"id\":\"syn-ev-$PID\",\"type\":\"payment.returned\",\"attributes\":{\"createdAt\":\"2026-11-10T14:00:00.000Z\",\"amount\":2500,\"direction\":\"Debit\",\"reason\":\"InsufficientFunds\"},\"relationships\":{\"payment\":{\"data\":{\"type\":\"payment\",\"id\":\"$PID\"}}}}]}"
SIG=$(printf '%s' "$BODY" | openssl dgst -sha1 -hmac "$SECRET" -binary | base64)
for n in 1 2; do
  printf "send %s (same event) -> " "$n"
  curl -s -m 20 -o "$HOME/.paypilot-last-response" -w "[HTTP %{http_code}] " -X POST -H "Content-Type: application/json" -H "X-Unit-Signature: $SIG" --data-binary "$BODY" "$URL"
  head -c 300 "$HOME/.paypilot-last-response" | tr -d "\n" | cut -c1-300; echo
done
echo "Expect send 1: decided=1 duplicates=0, send 2: decided=0 duplicates=1. SYNTHETIC: not a real Unit event."
