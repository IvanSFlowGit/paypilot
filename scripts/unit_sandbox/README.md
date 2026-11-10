# Unit sandbox test kit

Throwaway tooling for proving the `/webhooks/unit` receiver against Unit's sandbox from a laptop. Sandbox only:
the token is a local test secret, the tenant is `demo`, the audit store is in memory, and the tunnel address is temporary.

Order, one Terminal window each, keep them open:

1. `token.sh`  creates the webhook token once in the macOS keychain (service `paypilot-unit-webhook`) and copies it to the clipboard. Run again to re-copy the same token. It never prints it.
2. `tunnel.sh`  starts a free Cloudflare quick tunnel to port 8000, keeps the Mac awake, saves the address to `~/.paypilot-tunnel-url` and prints it.
3. `run.sh`  starts PayPilot on port 8000 with the keychain token, tenant `demo` and a fresh database file `~/.paypilot-unit-demo.db`.
4. `tunneltest.sh`  sends one unsigned request through the tunnel. Expect `HTTP 400`.
5. In Unit: Developer, Webhooks, Create. Url is the printed address plus `/webhooks/unit`, Token pasted from `token.sh`, At least once, All, Json. A webhook URL cannot be edited, so a new tunnel address means a new webhook.
6. `return_test.sh`  sends one SYNTHETIC signed `payment.returned` twice. Expect `decided: 1` then `duplicates: 1`.
7. In Unit, Applications, "Test Create Application API", Send. Real signed deliveries should appear as `POST /webhooks/unit ... 200` from Unit's address.

Limits measured 2026-11-10: Unit's sandbox has no simulation for an originated ACH return, and Unit's support chat said the `payment.returned` event
carries no reason code. The synthetic return proves the path, not Unit's real payload. Quick tunnels drop when the Mac sleeps or the window closes.
