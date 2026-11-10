#!/bin/sh
# Fixed address for the Unit sandbox webhook: a NAMED Cloudflare tunnel on your own domain.
# Only /webhooks/unit is routed to localhost:8000; every other path on the host returns 404.
#   named_tunnel.sh setup [hostname]   one time: tunnel, DNS record, config (needs `cloudflared tunnel login` first)
#   named_tunnel.sh run                every session: keeps the Mac awake and runs the tunnel
# A DNS name is not a secret. The protection is the HMAC signature on the route plus the path limit above.
NAME="paypilot-unit"
DOMAIN="${UNIT_TUNNEL_DOMAIN:-streamflow.solutions}"
CFG="$HOME/.cloudflared/$NAME.yml"
HOSTFILE="$HOME/.paypilot-named-host"
case "$1" in
  setup)
    test -f "$HOME/.cloudflared/cert.pem" || { echo "FAILED: not logged in. Run: cloudflared tunnel login   (pick $DOMAIN in the browser), then rerun setup"; exit 2; }
    HOST="${2:-$(cat "$HOSTFILE" 2>/dev/null)}"
    [ -n "$HOST" ] || HOST="unit-$(openssl rand -hex 4).$DOMAIN"
    printf '%s' "$HOST" > "$HOSTFILE"
    ID=$(cloudflared tunnel list --name "$NAME" -o json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[0]["id"] if d else "")' 2>/dev/null)
    if [ -z "$ID" ]; then
      cloudflared tunnel create "$NAME" || { echo "FAILED: tunnel create"; exit 2; }
      ID=$(cloudflared tunnel list --name "$NAME" -o json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')
    fi
    test -n "$ID" || { echo "FAILED: could not read the tunnel id"; exit 2; }
    cloudflared tunnel route dns "$NAME" "$HOST" || echo "note: DNS route reported an error (fine if the record already exists)"
    cat > "$CFG" <<YML
tunnel: $ID
credentials-file: $HOME/.cloudflared/$ID.json
ingress:
  - hostname: $HOST
    path: ^/webhooks/unit\$
    service: http://localhost:8000
  - service: http_status:404
YML
    printf 'https://%s' "$HOST" > "$HOME/.paypilot-tunnel-url"
    echo "ready. Webhook URL for Unit: https://$HOST/webhooks/unit"
    echo "next: named_tunnel.sh run   (leave that window open)"
    ;;
  run)
    test -f "$CFG" || { echo "FAILED: no config. Run: named_tunnel.sh setup"; exit 2; }
    echo "Tunnel running. Webhook URL: $(cat "$HOME/.paypilot-tunnel-url")/webhooks/unit"
    exec caffeinate -i cloudflared tunnel --no-autoupdate --config "$CFG" run "$NAME"
    ;;
  *) echo "usage: named_tunnel.sh setup [hostname] | run"; exit 2 ;;
esac
