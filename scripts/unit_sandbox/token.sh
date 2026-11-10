#!/bin/sh
# Creates the sandbox webhook token once, keeps it in your macOS keychain, and copies it to the clipboard.
# Run again any time to copy the SAME token again. Never prints the token.
SVC="paypilot-unit-webhook"
if ! security find-generic-password -s "$SVC" -w >/dev/null 2>&1; then
  T=$(openssl rand -hex 24) || exit 2
  security add-generic-password -a "$USER" -s "$SVC" -w "$T" || { echo "FAILED: keychain write"; exit 2; }
  echo "created a new token in your keychain"
fi
security find-generic-password -s "$SVC" -w | tr -d '\n' | pbcopy && echo "token is on your clipboard (not shown). Paste it into Unit's Token field."
