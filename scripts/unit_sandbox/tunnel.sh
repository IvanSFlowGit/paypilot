#!/bin/sh
# Starts the public tunnel to local PayPilot, keeps the Mac awake, saves and prints the webhook address.
rm -f "$HOME/.paypilot-tunnel-url"
echo "Starting tunnel. Leave this window open. The webhook address appears below in a few seconds."
caffeinate -i cloudflared tunnel --protocol http2 --no-autoupdate --url http://localhost:8000 2>&1 | while IFS= read -r line; do
  u=$(printf '%s' "$line" | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | head -1)
  if [ -n "$u" ] && [ ! -s "$HOME/.paypilot-tunnel-url" ]; then
    printf '%s' "$u" > "$HOME/.paypilot-tunnel-url"
    echo ""
    echo ">>>>>> WEBHOOK URL FOR UNIT: $u/webhooks/unit"
    echo ""
  fi
done
