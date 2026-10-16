"""The OAuth `state` parameter: unguessable, bound to a client, expiring.

WHY THIS IS NOT A STRING THE CALLER PICKS. The consent callback arrives from
the bank, in a browser, with no bearer token: there is nothing else in the
request that says which client the flow belongs to. So the state IS the
credential on that hop, and a callback that accepts any state lets anybody
complete a consent flow and have the resulting tokens filed under a client
of their choosing.

WHY IT REUSES THE CONSENT KEY RATHER THAN ADDING A SECOND SECRET. A Fernet
token is already signed, already timestamped, and `decrypt(ttl=...)` already
refuses one that is too old. That is exactly the primitive this needs, so
there is no new environment variable, no clock arithmetic of my own, and no
second key to rotate. The consent store cannot run without that key anyway,
so a deployment that can store a consent can always issue a state.

THE ONE PROPERTY THIS DOES NOT HAVE, NAMED RATHER THAN IMPLIED: IT IS NOT
SINGLE USE. A replayed state verifies again until it expires. Single use
needs somewhere to record spent states, and the consent store has no
Postgres backend yet, so adding a second table there would be two untested
stores instead of one. The residual is narrow, because the provider's own
authorization code is single use and the token exchange is what actually
fails on a replay, and it is narrow rather than absent. It is written down
here, in the route's docstring, and on the human board, rather than being
left for somebody to discover by reading the code.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from app.bank.consent import ConsentError, _require_fernet

# Ten minutes. A consent flow is a person clicking through their bank's own
# screens, which is minutes rather than hours, and a longer window buys a
# replay more time for nothing.
STATE_TTL_SECONDS = 600

# The version is in the payload so a later format change is a refusal rather
# than a misread: an old token decrypts cleanly and would otherwise be
# interpreted under the new rules.
STATE_VERSION = 1


class StateError(ConsentError):
    """A state parameter was absent, malformed, expired or not ours."""


@dataclass(frozen=True)
class BankFlowState:
    client_id: str
    provider: str
    nonce: str


def issue_state(
    *, client_id: str, provider: str, fernet: Fernet | None = None
) -> str:
    """Mint a state for one client and one provider."""
    if not isinstance(client_id, str) or not client_id.strip():
        raise StateError("client_id must be a non-empty string")
    if not isinstance(provider, str) or not provider.strip():
        raise StateError("provider must be a non-empty string")
    f = fernet or _require_fernet()
    payload = {
        "v": STATE_VERSION,
        "client_id": client_id,
        "provider": provider,
        # 32 bytes of urandom. Two flows started in the same second by the
        # same client for the same provider must not produce one string.
        "nonce": secrets.token_urlsafe(32),
    }
    return f.encrypt(json.dumps(payload, sort_keys=True).encode("utf-8")).decode("ascii")


def read_state(
    state: object, *, fernet: Fernet | None = None, ttl: int = STATE_TTL_SECONDS
) -> BankFlowState:
    """Verify and unpack a state, or raise. Never returns a default.

    Every failure is one exception type with a reason, because a caller that
    could tell expired from forged from malformed would be telling whoever
    sent it the same thing.
    """
    if not isinstance(state, str) or not state:
        raise StateError("no state parameter")
    f = fernet or _require_fernet()
    try:
        raw = f.decrypt(state.encode("ascii", errors="strict"), ttl=ttl)
    except InvalidToken:
        raise StateError("state is expired, forged, or encrypted under another key") from None
    except (UnicodeEncodeError, ValueError, TypeError):
        raise StateError("state is not a well formed token") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise StateError("state payload is not JSON") from None
    if not isinstance(payload, dict):
        raise StateError("state payload is not an object")
    if payload.get("v") != STATE_VERSION:
        raise StateError(f"state version {payload.get('v')!r} is not {STATE_VERSION}")
    client_id = payload.get("client_id")
    provider = payload.get("provider")
    nonce = payload.get("nonce")
    for name, value in (("client_id", client_id), ("provider", provider), ("nonce", nonce)):
        if not isinstance(value, str) or not value:
            raise StateError(f"state payload has no usable {name}")
    return BankFlowState(client_id=client_id, provider=provider, nonce=nonce)
