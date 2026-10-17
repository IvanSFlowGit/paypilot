"""The three bank handlers, as pure functions returning (status, payload).

api.py wraps these in routes, exactly as it wraps app/decision.py, so the
handlers are testable with no client, no server and no network.

ON THE WORD READ-ONLY, because the plan said "routes, all reads" and that is
true of the BANK and not of our own disk. The callback writes a consent row
here. What none of these do is ask a bank to move money: the protocol has no
method for it, so there is nothing for a route to call.

WHO AUTHENTICATES WHAT, which differs per route and is the thing to get right:

  START is a client calling us. Bearer token, and the client id comes from
  the token and never from a query parameter, because a caller declaring its
  own identity is a label.

  CALLBACK is a browser arriving from the bank with no bearer token at all.
  THE STATE IS THE CREDENTIAL on that hop. See app/bank/state.py, including
  the replay property it does not have.

  WEBHOOK is the provider calling us. The adapter's own verify_webhook is the
  credential, and a body that does not verify is refused and audited rather
  than parsed, because parsing first and checking after means an unverified
  body has already reached the code that reads it.

THE GAP THIS FILE CANNOT CLOSE, STATED HERE AND ON THE HUMAN BOARD. A return
event carries an ACCOUNT id and nothing maps an account to a client, because
the consent row has no account column. So an event that verifies and parses
cannot be attributed, and the honest outcome is 202 with the reason in the
body plus an audit event, never a silent 200. A webhook that drops what it
cannot place is the one failure nobody ever sees, so the drop is made loud
instead of being designed away with a guess about whose account it is.
"""

from __future__ import annotations

# A consent id is ours rather than the provider's: a provider that reuses or
# omits its own identifier would otherwise decide our primary key.
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime

from app import rails
from app.audit import audit_security_event
from app.bank import provider as bp
from app.bank import state as bank_state
from app.bank.consent import ConsentError, ConsentKeyMissing


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        raise ConsentError("now must be timezone aware")
    return now.astimezone(UTC)


def _adapter(provider: object) -> object:
    """Resolve and construct one adapter, or raise bp.RegistryError.

    Construction is where an adapter states its own preconditions: the fake
    refuses unless it has been switched on, and a real one would refuse with
    no credentials. So an unavailable provider fails here, with its own
    reason, rather than being reported as an unknown name.
    """
    cls = bp.get(provider)
    return cls()


def handle_consent_start(
    *,
    provider: object,
    client_id: str | None,
    redirect_uri: object,
    scopes: tuple[str, ...] | None = None,
    fernet=None,
) -> tuple[int, dict]:
    """Mint a state and hand back the bank's own authorize URL."""
    if not client_id:
        return 401, {"error": "unauthorised", "detail": "bearer token required"}
    if not isinstance(redirect_uri, str) or not redirect_uri.strip():
        return 400, {"error": "invalid_request", "detail": "redirect_uri is required"}
    try:
        adapter = _adapter(provider)
    except bp.RegistryError as exc:
        return 404, {"error": "unknown_provider", "detail": str(exc)}
    except bp.ProviderError as exc:
        # The provider exists and is not available here. Different answer
        # from "no such provider", and the caller can act on the difference.
        return 503, {"error": "provider_unavailable", "detail": str(exc)}
    name = getattr(type(adapter), "PROVIDER_NAME", str(provider))
    try:
        token = bank_state.issue_state(
            client_id=client_id, provider=name, fernet=fernet
        )
    except ConsentKeyMissing as exc:
        return 503, {"error": "not_configured", "detail": str(exc)}
    use_scopes = scopes if scopes is not None else getattr(
        type(adapter), "SCOPES", ("accounts", "transactions")
    )
    try:
        url = adapter.authorize_url(
            state=token, redirect_uri=redirect_uri.strip(), scopes=tuple(use_scopes)
        )
    except bp.ProviderError as exc:
        return 400, {"error": "invalid_request", "detail": str(exc)}
    return 200, {
        "provider": name,
        "authorize_url": url,
        "state": token,
        "state_expires_in_seconds": bank_state.STATE_TTL_SECONDS,
    }


def handle_consent_callback(
    *,
    state: object,
    code: object,
    store,
    fernet=None,
    now: datetime | None = None,
) -> tuple[int, dict]:
    """Exchange the code the bank sent back and file the consent.

    THE STATE IS CHECKED BEFORE THE CODE IS TOUCHED. Exchanging first and
    verifying after would spend a real authorization code on a request we
    had not authenticated.
    """
    try:
        flow = bank_state.read_state(state, fernet=fernet)
    except ConsentKeyMissing as exc:
        return 503, {"error": "not_configured", "detail": str(exc)}
    except bank_state.StateError as exc:
        audit_security_event(
            event="bank_consent_state_rejected",
            detail=f"Consent callback refused: {exc}",
            severity="error",
        )
        # One answer for expired, forged and malformed. Telling them apart
        # tells whoever sent it which one it was.
        return 400, {"error": "invalid_state", "detail": "state is not valid"}
    if not isinstance(code, str) or not code.strip():
        return 400, {"error": "invalid_request", "detail": "code is required"}
    try:
        adapter = _adapter(flow.provider)
    except bp.RegistryError as exc:
        return 404, {"error": "unknown_provider", "detail": str(exc)}
    except bp.ProviderError as exc:
        return 503, {"error": "provider_unavailable", "detail": str(exc)}
    try:
        tokens = adapter.exchange_code(code=code.strip(), redirect_uri="")
    except bp.ProviderError as exc:
        return 502, {"error": "exchange_failed", "detail": str(exc)}
    consent_id = f"{flow.provider}-{secrets.token_urlsafe(16)}"
    moment = _now(now)
    try:
        store.put(
            consent_id=consent_id,
            client_id=flow.client_id,
            provider=flow.provider,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            access_expires_at=tokens.access_expires_at,
            consent_expires_at=tokens.consent_expires_at,
            scopes=tokens.scopes,
            now=moment,
        )
    except ConsentError as exc:
        return 409, {"error": "not_stored", "detail": str(exc)}
    stored = store.get(consent_id=consent_id, client_id=flow.client_id)
    if stored is None:
        # The write reported success and the read back found nothing. Assert
        # the effect, never the operation.
        return 500, {"error": "not_stored", "detail": "consent did not read back"}
    return 200, _consent_view(consent_id, stored, moment)


def _consent_view(consent_id: str, stored, moment: datetime) -> dict:
    """The one shape a consent is ever shown in, by callback and by read.

    No token, no expiry timestamps, no account ids. The caller started this
    flow and needs to know it worked, not what we are holding. One builder
    means the read route cannot drift into showing more than the callback.
    """
    status = stored.state(moment)
    return {
        "consent_id": consent_id,
        "provider": stored.provider,
        "state": status.state,
        "reason": status.reason,
        "needs_refresh": status.needs_refresh,
        "scopes": list(stored.scopes),
    }


def handle_consent_read(
    *,
    consent_id: object,
    client_id: str | None,
    store,
    now: datetime | None = None,
) -> tuple[int, dict]:
    """Read one consent back, scoped to the caller.

    store.get returns None for an absent id AND for another client's id, so
    the two are one answer here too. That is deliberate: a different status
    for each would let a caller probe which ids exist.
    """
    if not client_id:
        return 401, {"error": "unauthorised", "detail": "bearer token required"}
    if not isinstance(consent_id, str) or not consent_id.strip():
        return 400, {"error": "invalid_request", "detail": "consent_id is required"}
    stored = store.get(consent_id=consent_id.strip(), client_id=client_id)
    if stored is None:
        return 404, {"error": "not_found", "detail": "no such consent"}
    return 200, _consent_view(stored.consent_id, stored, _now(now))


def handle_bank_webhook(
    *,
    provider: object,
    body: bytes,
    headers: Mapping[str, str],
    now: datetime | None = None,
) -> tuple[int, dict]:
    """Verify, then parse, then say honestly what could not be placed."""
    try:
        adapter = _adapter(provider)
    except bp.RegistryError:
        # Do not echo the name back or confirm which providers exist.
        return 404, {"error": "unknown_provider", "detail": "no such provider"}
    except bp.ProviderError as exc:
        return 503, {"error": "provider_unavailable", "detail": str(exc)}
    name = getattr(type(adapter), "PROVIDER_NAME", str(provider))
    try:
        ok = adapter.verify_webhook(body=body, headers=headers)
    except bp.ProviderError as exc:
        return 502, {"error": "verify_failed", "detail": str(exc)}
    if not ok:
        audit_security_event(
            event="bank_webhook_signature_rejected",
            detail=f"Bank webhook signature failed for provider {name}",
            severity="error",
        )
        return 401, {"error": "bad_signature", "detail": "signature did not verify"}
    event = adapter.parse_return_event(body=body)
    if event is None:
        # The third answer: the feed spoke and it was not a return. A 200
        # saying so is not the same as a 200 that silently dropped one.
        return 200, {"provider": name, "handled": False, "reason": "no_return_code"}

    decision = None
    if event.code is None:
        # THE THIRD STATE. The feed said a debit came back and did not say
        # why, which is conformant: the reason field is optional where it
        # exists at all. Calling the rail table with no code would be asking
        # a question with no subject, so this abstains and names which kind
        # of absence it is, because "this ASPSP never exposes a reason" and
        # "this ASPSP exposes one and left it empty" are different facts and
        # only the second one means no reason was given.
        decision = {
            "rail": event.rail,
            "code": None,
            "abstains": True,
            "reason_state": event.reason_state,
            "abstain_reason": (
                f"the feed reported a return on rail {event.rail} and carried "
                f"no reason code ({event.reason_state}). The rail table is not "
                "consulted, because a lookup with no code returns an answer "
                "about nothing."
            ),
        }
        return _unattributed(name=name, event=event, decision=decision)

    try:
        d = rails.lookup(event.rail, event.code)
        decision = {
            "rail": event.rail,
            "code": event.code,
            "matched": getattr(d, "matched", None),
            "goes_again": getattr(d, "goes_again", None),
            "what_has_to_change_first": getattr(d, "what_has_to_change_first", None),
            "abstains": getattr(d, "abstains", None),
            "fallback_reason": getattr(d, "fallback_reason", None),
        }
    # NO except rails.RegistryError HERE, AND ITS ABSENCE IS DELIBERATE.
    # There was one, returning {"error": "unknown_rail"}. Measured against
    # the real registry with five inputs, rails.lookup raises RailError for
    # every one of them, including an unknown name, a non-string and an
    # empty string; RegistryError is raised only while VALIDATING the rail
    # modules at import. So the branch was unreachable, and it was worse
    # than dead: it described an outcome this system does not have, because
    # an unknown rail ABSTAINS rather than returning an error field, and a
    # reader would have believed there were two shapes of answer.
    #
    # WHAT ITS REMOVAL STOPS CATCHING, named rather than assumed: if the
    # registry is ever changed to raise RegistryError from get(), that now
    # propagates out of this handler instead of being swallowed into a
    # decision dict. That is the direction to fail in, because a registry
    # that cannot resolve its own rails is a deployment fault and not a
    # property of one feed row.
    except rails.RailError as exc:
        # The rail exists and needs context this feed does not carry, which
        # is an abstention rather than a failure.
        decision = {
            "rail": event.rail,
            "code": event.code,
            "abstains": True,
            "abstain_reason": str(exc),
        }

    return _unattributed(name=name, event=event, decision=decision, now=now)


def _unattributed(
    *,
    name: str,
    event: bp.ReturnEvent,
    decision: dict | None,
    now: datetime | None = None,
) -> tuple[int, dict]:
    """The one exit for a verified, parsed, unplaceable return event.

    ONE EXIT RATHER THAN TWO COPIES. Both the decided path and the
    no-reason-available path end here, so the audit event and the 202 cannot
    drift apart. Two clauses stating one fact get one deleted, never kept in
    step by whoever edits next.

    ATTRIBUTION IS NOT BUILT. The event names an account and no store maps an
    account to a client, so this is recorded loudly and returned as
    accepted-but-unplaced. A 200 here would be the silent drop.
    """
    audit_security_event(
        event="bank_return_event_unattributed",
        detail=(
            f"Verified return event from {name} could not be attributed to a "
            f"client: rail={event.rail} code={event.code} "
            f"reason_state={event.reason_state} "
            f"account={event.account_id} amount_minor={event.amount_minor}. "
            "No account-to-client mapping exists."
        ),
        severity="warning",
    )
    return 202, {
        "provider": name,
        "handled": False,
        "reason": "unattributed",
        "detail": "event verified and parsed; no account-to-client mapping exists",
        "decision": decision,
        "observed_at": _now(now).isoformat(),
    }
