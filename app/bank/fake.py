"""A complete offline bank provider, so the whole path runs with no account.

Consent, refresh, read and decide all work against this adapter with no
network, no credentials and no aggregator contract. Phases 1 to 6 of the bank
work need nothing from a person because of this file.

WHAT MAKES IT SAFE TO HAVE IN THE TREE, since a fake that can be selected by
accident is worse than no fake at all:

  IT REFUSES TO CONSTRUCT UNLESS SOMEBODY SAID SO. ``FakeBank()`` raises
  unless PAYPILOT_FAKE_BANK is set or ``enabled=True`` is passed explicitly.
  A config typo cannot reach it, and a deploy that somehow imports it still
  cannot instantiate it.

  EVERY VALUE IT RETURNS CARRIES A VISIBLE MARKER. Account ids start FAKE-,
  transaction ids start FAKE-TX-, tokens start fake-access- and the display
  label says SYNTHETIC. So a figure that escaped into a report, a log line or
  a client document announces itself rather than reading as a measurement.
  Canon's rule is that seeded data must never be the evidence a metric works;
  the marker is what lets a reader tell the two apart afterwards.

  ITS ENDPOINTS POINT AT .invalid. RFC 2606 reserves that TLD so it can never
  resolve. If any code path ever tries to call the fake over the wire, DNS
  fails loudly instead of a request reaching somebody else's server.

TWO THINGS IT DELIBERATELY DOES NOT DO.

  verify_webhook DOES NOT RETURN TRUE. It computes a real HMAC over the body
  and compares it in constant time, because a fake that accepts every body
  cannot exercise the rejection path, and the rejection path is the one
  nobody ever runs. A guard tested only on the accepting case has proven
  nothing.

  NOTHING IS DATED IN THE FILE. Every timestamp is derived from an injected
  clock, so there is no fixture that silently expires: a date-relative
  fixture generated at import is empty the following month and every
  count-the-rows check still passes.

AND IT ANSWERS THE OPEN QUESTION HONESTLY. parse_return_event returns None
for a feed entry that carries no return code, which is the third answer: the
feed said something and it was not a return. Whether a real AIS feed carries
a return reason code AT ALL is unverified and is recorded as unverified; the
fake can only show what the decision layer does once one arrives, never that
one does.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from app.bank.provider import (
    REASON_ABSENT_SUPPORTED,
    REASON_PRESENT,
    Account,
    ProviderError,
    ReturnEvent,
    TokenSet,
    Transaction,
    register,
)

# Reserved by RFC 2606: this name cannot resolve, anywhere, ever.
FAKE_HOST = "https://bank.invalid"

ENABLE_ENV = "PAYPILOT_FAKE_BANK"

# The marker every synthetic value carries. One constant, so a sweep for
# fake data in a report has exactly one string to look for.
MARKER = "FAKE"

# The webhook secret is a TEST secret and is in the source on purpose: it
# signs nothing real and verifies nothing real. A fake whose secret had to
# come from the environment would be one more reason not to run the tests.
TEST_WEBHOOK_SECRET = b"paypilot-fake-bank-test-secret-not-a-credential"

SIGNATURE_HEADER = "X-Fake-Signature"


def _utc(clock: Callable[[], datetime]) -> datetime:
    """A clock that forgets its timezone produces a comparison that lies."""
    now = clock()
    if now.tzinfo is None:
        raise ProviderError("clock returned a naive datetime; pass an aware one")
    return now.astimezone(UTC)


def sign_body(body: bytes) -> str:
    """The signature a caller would attach. Exported so a test can sign."""
    return hmac.new(TEST_WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()


@register
class FakeBank:
    """A provider that answers from memory. No network, no credentials."""

    PROVIDER_NAME = "fake"

    # Read paths only. Checked against the payment-initiation deny-list at
    # import by register(), exactly as a real adapter's are.
    ENDPOINTS = {
        "authorize": f"{FAKE_HOST}/v1/authorize",
        "token": f"{FAKE_HOST}/v1/token",
        "accounts": f"{FAKE_HOST}/v1/accounts",
        "transactions": f"{FAKE_HOST}/v1/accounts/{{account_id}}/transactions",
        "consents": f"{FAKE_HOST}/v1/consents",
    }

    # Minutes against days. The two expiries run on different clocks and the
    # fake keeps them far enough apart that a test can tell which one it is
    # looking at: an equal pair would pass under either reading.
    ACCESS_TTL = timedelta(minutes=10)
    CONSENT_TTL = timedelta(days=90)

    SCOPES = ("accounts", "transactions")

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        clock: Callable[[], datetime] | None = None,
        currency: str = "EUR",
    ) -> None:
        if enabled is None:
            enabled = os.environ.get(ENABLE_ENV) not in (None, "", "0")
        if not enabled:
            raise ProviderError(
                "FakeBank is off. Set "
                f"{ENABLE_ENV}=1 or pass enabled=True. It returns synthetic "
                "data and must never be reachable by a configuration typo."
            )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._currency = currency
        self._refreshes = 0

    # --- consent ----------------------------------------------------------

    def authorize_url(
        self, *, state: str, redirect_uri: str, scopes: tuple[str, ...]
    ) -> str:
        if not state:
            raise ProviderError("state must not be empty; it is the CSRF guard")
        if not redirect_uri:
            raise ProviderError("redirect_uri must not be empty")
        from urllib.parse import urlencode

        query = urlencode(
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "response_type": "code",
            }
        )
        return f"{self.ENDPOINTS['authorize']}?{query}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> TokenSet:
        if not code:
            raise ProviderError("no code to exchange")
        now = _utc(self._clock)
        return TokenSet(
            access_token=f"fake-access-{code}",
            refresh_token=f"fake-refresh-{code}",
            access_expires_at=now + self.ACCESS_TTL,
            consent_expires_at=now + self.CONSENT_TTL,
            scopes=self.SCOPES,
        )

    def refresh(self, *, refresh_token: str) -> TokenSet:
        """A refresh moves the ACCESS expiry and leaves the CONSENT expiry.

        That is the whole point of the two columns: a refresh cannot extend a
        PSD2 consent, so a fake that moved both would hide the one failure the
        split exists to make visible.
        """
        if not refresh_token.startswith("fake-refresh-"):
            raise ProviderError("refresh token not issued by this provider")
        self._refreshes += 1
        now = _utc(self._clock)
        return TokenSet(
            access_token=f"fake-access-refreshed-{self._refreshes}",
            refresh_token=refresh_token,
            access_expires_at=now + self.ACCESS_TTL,
            consent_expires_at=None,
            scopes=self.SCOPES,
        )

    # --- reads ------------------------------------------------------------

    def accounts(self, *, access_token: str) -> tuple[Account, ...]:
        self._require_token(access_token)
        return (
            Account(
                account_id=f"{MARKER}-ACC-1",
                currency=self._currency,
                iban_tail="0001",
                display_label=f"{MARKER} SYNTHETIC current account",
            ),
            Account(
                account_id=f"{MARKER}-ACC-2",
                currency=self._currency,
                iban_tail="0002",
                display_label=f"{MARKER} SYNTHETIC collections account",
            ),
        )

    def transactions(
        self,
        *,
        access_token: str,
        account_id: str,
        since: datetime,
        until: datetime,
    ) -> tuple[Transaction, ...]:
        """Generated INSIDE the window asked for, never against a fixed date.

        A fixture spread across a hardcoded month is empty the following month
        while every row-count check still passes, so the window is the input
        and the rows are derived from it.
        """
        self._require_token(access_token)
        if since.tzinfo is None or until.tzinfo is None:
            raise ProviderError("since and until must be timezone aware")
        if until <= since:
            raise ProviderError(f"until {until} is not after since {since}")
        span = until - since
        rows: list[Transaction] = []
        for i, (amount, text) in enumerate(
            (
                (-4500, "DD COLLECTION 4500"),
                (-1250, "DD COLLECTION 1250"),
                (4500, "RETURNED DD AC04 4500"),
                (9900, "CUSTOMER TRANSFER IN"),
            )
        ):
            rows.append(
                Transaction(
                    transaction_id=f"{MARKER}-TX-{account_id}-{i}",
                    amount_minor=amount,
                    currency=self._currency,
                    booked_at=since + span * ((i + 1) / 5),
                    description=f"{MARKER} {text}",
                )
            )
        return tuple(rows)

    # --- webhooks ---------------------------------------------------------

    def verify_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> bool:
        """A real comparison, so the refusing branch can be exercised."""
        supplied = None
        for key, value in headers.items():
            if key.lower() == SIGNATURE_HEADER.lower():
                supplied = value
                break
        if supplied is None:
            return False
        return hmac.compare_digest(supplied, sign_body(body))

    def parse_return_event(self, *, body: bytes) -> ReturnEvent | None:
        """None means the feed said something and it was not a return.

        The shape is deliberately crude: the fake exists to drive the decision
        layer, and inventing an elaborate provider envelope here would be a
        fixture asserting things about a provider nobody has read.
        """
        text = body.decode("utf-8", errors="replace")
        fields = {}
        for part in text.split(";"):
            if "=" in part:
                key, _, value = part.partition("=")
                fields[key.strip()] = value.strip()
        rail = fields.get("rail")
        code = fields.get("code")
        if not rail:
            return None

        # THE THIRD STATE, DRIVABLE OFFLINE. `returned=1` with no `code` is a
        # feed saying a debit came back without saying why, which is what a
        # conformant ASPSP may send because the reason field is optional. It
        # is here so the no-reason path can be exercised with no account and
        # no network: a state the fake cannot produce is a state only
        # production ever reaches.
        if not code:
            if fields.get("returned", "").strip() not in ("1", "true", "yes"):
                return None
            state = fields.get("reason_state", REASON_ABSENT_SUPPORTED).strip()
            return ReturnEvent(
                rail=rail,
                code=None,
                account_id=fields.get("account", f"{MARKER}-ACC-1"),
                amount_minor=int(fields.get("amount_minor", "0")),
                currency=fields.get("currency", self._currency),
                occurred_at=_utc(self._clock),
                raw_diagnostic=f"{MARKER} {text}",
                reason_state=state,
            )

        return ReturnEvent(
            rail=rail,
            code=code,
            account_id=fields.get("account", f"{MARKER}-ACC-1"),
            amount_minor=int(fields.get("amount_minor", "0")),
            currency=fields.get("currency", self._currency),
            occurred_at=_utc(self._clock),
            raw_diagnostic=f"{MARKER} {text}",
            reason_state=REASON_PRESENT,
        )

    # --- internals --------------------------------------------------------

    def _require_token(self, access_token: str) -> None:
        if not access_token.startswith("fake-access-"):
            raise ProviderError("access token not issued by this provider")
