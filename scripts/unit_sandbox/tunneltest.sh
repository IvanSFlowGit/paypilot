#!/bin/sh
# Sends one deliberately unsigned request through the public tunnel. Expect 400 and a rejected-signature line in the PayPilot window.
BASE=$(cat "$HOME/.paypilot-tunnel-url" 2>/dev/null)
test -n "$BASE" || { echo "FAILED: no tunnel address. Start ~/unitkit/named_tunnel.sh run first"; exit 2; }
HOST=${BASE#https://}
printf "address: %s\n" "$BASE"
CODE=$(curl -sS -m 15 -o /dev/null -w "%{http_code}" -X POST -d '{}' "$BASE/webhooks/unit" 2>"$HOME/.paypilot-curl-err"); RC=$?
echo "tunnel -> HTTP $CODE (curl exit $RC)"
if [ "$RC" -ne 0 ]; then
  echo "curl said: $(head -c 200 "$HOME/.paypilot-curl-err")"
  if host "$HOST" >/dev/null 2>&1; then echo "DNS: the address resolves, so the tunnel is not connected or not reachable"; else echo "DNS: your resolver cannot find the address yet. Wait 1 to 2 minutes and rerun."; fi
fi
case "$CODE" in
  400) echo "OK: tunnel reaches PayPilot and PayPilot refused the unsigned request, as designed." ;;
  502|503|504) echo "tunnel is up but PayPilot is not answering on port 8000. Start it in its own window with: ~/unitkit/run.sh" ;;
  530) echo "Cloudflare says the tunnel is not connected. Start it in its own window with: ~/unitkit/named_tunnel.sh run" ;;
esac
