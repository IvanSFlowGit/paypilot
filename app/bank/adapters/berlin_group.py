"""A Berlin Group NextGenPSD2 AIS adapter, read-only, no credential required
to exercise the part that answers the design question.

WHAT WAS MEASURED, AND IT IS THE REASON THIS FILE EXISTS. The open question
in the plan was: does a rail return REASON survive into an AIS transaction
feed at all? If it never does, a bank feed can only say money came back and
the reason has to arrive from the rail's own return report instead.

It does survive, on at least one real ASPSP, and it is OPTIONAL. ASN Bank's
published PSD2 AIS interface v1.25 (1 July 2025) defines, in the attribute
table headed Transactions:

    returnInformationCode | String | Mandatory N | "A 4-digit code indicating
    why a SEPA payment is returned (ISO 20022 ExternalReturn Reason1Code) or
    SCT instant reversed due to negative conformation (AB05, AB06, AB09)."

Quoted with the document's own line wrap inside ExternalReturnReason1Code
and its own spelling of conformation. ISO 20022 ExternalReturnReason1Code is
the same code set app/data/sepa_return_codes.csv is built from, so where the
field arrives the existing SEPA table decides it with nothing new needed.
Hash, byte count and controls: docs/claims.md row 12.

THREE CONSEQUENCES, EACH BUILT RATHER THAN NOTED.

ONE, THE FIELD NAME IS NOT A CONSTANT. Berlin Group leaves this to the
ASPSP, so the name is per-provider configuration and absence of a configured
name is its own state. An adapter hardcoding one bank's spelling reports
every other bank as having no reason, which is the false negative that
retires the design.

TWO, MANDATORY N MEANS ABSENCE IS NOT EVIDENCE. A feed that reports a
returned debit and omits the reason is conformant. So this adapter never
collapses that into not-a-return and never invents a code: it returns the
third ReturnEvent state and names which kind of absence it is.

THREE, WHAT NEEDS A CREDENTIAL REFUSES RATHER THAN RETURNING EMPTY. There is
no sandbox credential on this machine, so exchange_code, refresh, accounts
and transactions raise ProviderError naming exactly what is missing. An
empty tuple would be indistinguishable from an account with no transactions,
which is the silent-degradation failure this estate keeps recording. The
half that needed no credential, turning a transaction into a decision, is
the half the question turned on and it is fully exercised offline.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.bank.provider import (
    REASON_ABSENT_SUPPORTED,
    REASON_PRESENT,
    REASON_UNSUPPORTED,
    Account,
    ProviderError,
    ReturnEvent,
    TokenSet,
    Transaction,
    register,
)

# Reserved by RFC 2606. No profile here points at a real bank, so nothing in
# this module can reach one by accident.
UNCONFIGURED_HOST = "https://aspsp.invalid"


@dataclass(frozen=True)
class AspspProfile:
    """What one ASPSP was READ to do, never what the standard permits.

    reason_field is None when that provider's published transaction schema
    carries no return-reason field. None and "not looked yet" are different
    facts, so a provider nobody has read has no profile at all rather than a
    profile with None in it.
    """

    aspsp: str
    reason_field: str | None
    rail_default: str
    source: str  # where it was read
    read_on: str  # when

    def reason_state_for(self, tx: Mapping[str, Any]) -> tuple[str | None, str]:
        """Return (code, reason_state) for one transaction object.

        The ordering is the whole point. Ask whether this ASPSP exposes a
        reason field AT ALL before asking whether this transaction carries
        one, because the two absences mean different things and the second
        question cannot distinguish them.
        """
        if self.reason_field is None:
            return None, REASON_UNSUPPORTED
        raw = tx.get(self.reason_field)
        if raw is None or str(raw).strip() == "":
            return None, REASON_ABSENT_SUPPORTED
        return str(raw).strip(), REASON_PRESENT


# Measured profiles only. Each row names its source and its read date, and a
# provider absent from here is absent because nobody has read it.
PROFILES: dict[str, AspspProfile] = {
    "asn": AspspProfile(
        aspsp="ASN Bank NV",
        reason_field="returnInformationCode",
        rail_default="sepa",
        source="openbanking.asnbank.nl PSD2 interface AIS v1.25, Transactions table",
        read_on="2026-10-16",
    ),
}


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        raise ProviderError("transaction carries no booking date")
    try:
        # Berlin Group bookingDate is an ISO date; bookingDateTime may carry a
        # zone. Accept either and never guess a zone that was not given.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProviderError(f"unparseable booking date {text!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _minor(amount: object, currency: str) -> int:
    """Berlin Group amounts are decimal strings. Parse as a decimal string and
    never as a float: a float cannot hold 0.07 and money is not approximate.
    """
    text = str(amount or "").strip()
    if not text:
        raise ProviderError("transaction carries no amount")
    neg = text.startswith("-")
    text = text.lstrip("+-")
    if "." in text:
        whole, _, frac = text.partition(".")
    else:
        whole, frac = text, ""
    # Two minor units covers every currency this adapter is configured for.
    # A zero-decimal or three-decimal currency would need its own exponent,
    # so it is refused rather than silently scaled by the wrong factor.
    if currency.upper() not in _TWO_DECIMAL:
        raise ProviderError(
            f"currency {currency!r} has no configured minor-unit exponent; "
            "scaling it by 100 would be a guess about somebody's money"
        )
    # REFUSE A LONGER FRACTION RATHER THAN TRUNCATE IT. Truncating turns
    # "0.006" into 0 and reports it as an answer, which is the silent
    # degradation this estate keeps recording: a wrong figure about
    # somebody's money with no error anywhere. A two-decimal currency
    # carrying three decimals means the feed is not what we think it is,
    # and that is a finding rather than a rounding decision.
    if len(frac) > 2:
        raise ProviderError(
            f"amount {amount!r} carries {len(frac)} decimal places and "
            f"{currency.upper()} has two; refusing rather than truncating, "
            "because a silently shortened amount is a wrong figure about money"
        )
    frac = (frac + "00")[:2]
    if not (whole + frac).isdigit():
        raise ProviderError(f"unparseable amount {amount!r}")
    value = int(whole or "0") * 100 + int(frac)
    return -value if neg else value


_TWO_DECIMAL = frozenset({"EUR", "GBP", "USD", "CHF", "SEK", "DKK", "NOK", "PLN"})


@register
class BerlinGroupAis:
    """AIS only. Seven methods, four of which refuse without a credential."""

    PROVIDER_NAME = "berlin_group_ais"

    # AIS paths only. There is no initiation path here and the registry's
    # endpoint deny-list reads these strings at import time.
    ENDPOINTS = {
        "authorize": f"{UNCONFIGURED_HOST}/oauth2/authorize",
        "token": f"{UNCONFIGURED_HOST}/oauth2/token",
        "consents": f"{UNCONFIGURED_HOST}/v1/consents",
        "accounts": f"{UNCONFIGURED_HOST}/v1/accounts",
        "transactions": f"{UNCONFIGURED_HOST}/v1/accounts/{{id}}/transactions",
    }

    def __init__(self, *, aspsp: str = "asn") -> None:
        if aspsp not in PROFILES:
            raise ProviderError(
                f"no measured profile for ASPSP {aspsp!r}. Known: "
                f"{sorted(PROFILES)}. A profile is added only after that "
                "provider's own transaction schema has been read, because a "
                "guessed field name reports every return as having no reason."
            )
        self.profile = PROFILES[aspsp]

    # --- the half that needs a credential, and refuses without one --------

    def authorize_url(
        self, *, state: str, redirect_uri: str, scopes: tuple[str, ...]
    ) -> str:
        raise ProviderError(
            "no ASPSP base URL or client registration is configured for "
            f"{self.profile.aspsp}; returning a URL built against "
            f"{UNCONFIGURED_HOST} would be a link that cannot resolve"
        )

    def exchange_code(self, *, code: str, redirect_uri: str) -> TokenSet:
        raise ProviderError(self._no_credential("exchange_code"))

    def refresh(self, *, refresh_token: str) -> TokenSet:
        raise ProviderError(self._no_credential("refresh"))

    def accounts(self, *, access_token: str) -> tuple[Account, ...]:
        # An empty tuple here would be indistinguishable from a consent that
        # covers no accounts, so it refuses instead.
        raise ProviderError(self._no_credential("accounts"))

    def transactions(
        self,
        *,
        access_token: str,
        account_id: str,
        since: datetime,
        until: datetime,
    ) -> tuple[Transaction, ...]:
        raise ProviderError(self._no_credential("transactions"))

    def verify_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> bool:
        # NOT IMPLEMENTED AND REFUSING RATHER THAN RETURNING FALSE. False
        # reads as "this signature is bad", which is a claim about the
        # payload. The truth is that no signing secret is configured, which
        # is a claim about us.
        raise ProviderError(
            "no webhook signing secret is configured for "
            f"{self.profile.aspsp}; returning False would report an "
            "unconfigured verifier as a failed signature"
        )

    # --- the half that answers the design question, offline ---------------

    def parse_return_event(self, *, body: bytes) -> ReturnEvent | None:
        """Map one Berlin Group transaction object to a ReturnEvent.

        Accepts either a bare transaction object or the `transactions.booked`
        envelope the standard returns, because a caller holding one real
        response should not have to unwrap it to find out whether it parses.
        """
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"body is not JSON: {exc}") from exc
        tx = _first_transaction(payload)
        if tx is None:
            return None

        currency = str(tx.get("transactionAmount", {}).get("currency", "")).strip()
        amount_minor = _minor(
            tx.get("transactionAmount", {}).get("amount"), currency
        )
        code, reason_state = self.profile.reason_state_for(tx)

        # IS THIS A RETURN AT ALL? Two independent signals, because relying on
        # the reason field alone cannot see a return on an ASPSP that exposes
        # no reason field, which is exactly the case this adapter exists for.
        looks_returned = code is not None or _declares_return(tx)
        if not looks_returned:
            return None

        return ReturnEvent(
            rail=str(tx.get("rail") or self.profile.rail_default),
            code=code,
            account_id=str(tx.get("internalTransactionId") or tx.get("entryReference") or ""),
            amount_minor=amount_minor,
            currency=currency,
            occurred_at=_utc(tx.get("bookingDateTime") or tx.get("bookingDate")),
            raw_diagnostic=(
                f"{self.profile.aspsp} "
                f"field={self.profile.reason_field} "
                f"state={reason_state}"
            ),
            reason_state=reason_state,
        )

    # --- internals --------------------------------------------------------

    def _no_credential(self, method: str) -> str:
        return (
            f"{method} needs a live {self.profile.aspsp} consent and this "
            "machine holds no ASPSP credential. Refusing rather than "
            "returning an empty result, because empty is indistinguishable "
            "from a real answer of nothing."
        )


def _first_transaction(payload: object) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        booked = payload.get("transactions", {})
        if isinstance(booked, dict):
            rows = booked.get("booked") or booked.get("pending")
            if isinstance(rows, list) and rows:
                first = rows[0]
                return first if isinstance(first, dict) else None
        if "transactionAmount" in payload:
            return payload
    return None


def _declares_return(tx: Mapping[str, Any]) -> bool:
    """A return without a reason code still announces itself somewhere.

    REPORTED RATHER THAN MEASURED, and the module says so. ASN's spec states
    that a returned debit flips the amount sign and swaps the counterparty
    fields, which is a signal, and it is NOT sufficient on its own: a credit
    arriving for any other reason looks the same. So this reads an explicit
    declaration only, and a caller wanting sign-and-counterparty inference
    has to ask for it, because inferring a return from a positive amount
    would classify every incoming payment as a returned debit.
    """
    flag = tx.get("returnIndicator")
    if isinstance(flag, bool):
        return flag
    return str(flag or "").strip().lower() in ("1", "true", "yes")
