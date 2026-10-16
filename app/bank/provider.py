"""The bank provider seam: read-only by construction.

Every adapter under app/bank/adapters/ implements BankProvider and nothing
else. The protocol declares seven methods and NOT ONE OF THEM MOVES MONEY,
so an adapter cannot initiate a payment by implementing the interface it is
required to implement. Payment initiation (PIS) is out of scope for this
package and is kept out structurally rather than by a sentence in a README.

A sentence is not a control, so `register` refuses at import time:

  Layer 1, METHOD NAMES. A method whose name is a bare money noun, or that
  carries a mutating verb AND a money noun together, breaks the import of
  the module that declares it.

  Layer 2, DECLARED ENDPOINTS. An adapter's ENDPOINTS mapping is read and
  every URL path segment checked against the same nouns, because an adapter
  can reach a payment-initiation endpoint through a generic request helper
  whose own name is innocent. Two assertions at different layers: the first
  catches the obvious shape and the second catches the one that hides.

WHAT THE DENY-LIST CANNOT SEE, stated rather than implied. It reads names
and declared strings. An adapter that builds a path at runtime from
fragments, or posts to a URL handed to it by a caller, defeats both layers.
That residual is why the protocol has no money-moving method in the first
place: the deny-list is the second control and the interface is the first.

WHY BARE NOUNS ARE REFUSED EVEN THOUGH A READ COULD BE INNOCENT. `payments`
as a read method is legitimate at a bank that exposes payment status under
that name. It is refused here anyway, because this package is AIS-only and
a method called `payments` is one edit away from being the thing that sends
one. `payment_failed_events` passes, and is carried as a control: a money
noun with no mutating verb is ordinary reading and must not be blocked, or
the guard is stepped around inside a week.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit


class ProviderError(RuntimeError):
    """An adapter could not answer. Never raised to mean 'no data'."""


class PaymentInitiationRefused(RuntimeError):
    """An adapter declared something that could move money. Import fails."""


class RegistryError(RuntimeError):
    """An adapter does not satisfy the protocol it claims to implement."""


# --- the shapes an adapter returns -----------------------------------------
# No full IBAN, no account holder name, no counterparty name. The decision
# layer does not need them and a field that exists gets logged eventually.


@dataclass(frozen=True)
class TokenSet:
    """Tokens only, never bank credentials.

    access_expires_at and consent_expires_at run on DIFFERENT CLOCKS: an
    access token lapses in minutes and a PSD2 consent lapses in days, so a
    single expiry column would silently answer the wrong question.
    """

    access_token: str
    refresh_token: str | None
    access_expires_at: datetime
    consent_expires_at: datetime | None
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class Account:
    account_id: str
    currency: str
    iban_tail: str  # last four only; never the full IBAN
    display_label: str  # provider's own label, untrusted text


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    amount_minor: int  # negative for a debit leaving the account
    currency: str
    booked_at: datetime
    description: str  # provider's own text, UNTRUSTED, never into a prompt


@dataclass(frozen=True)
class ReturnEvent:
    """A rail return observed in a bank feed, if one survives at all.

    rail and code feed app.rails.lookup(rail, code) directly. parse_return_event
    returns None when an event carries no return code, which is the honest
    third answer: the feed said something and it was not a return.
    """

    rail: str
    code: str
    account_id: str
    amount_minor: int
    currency: str
    occurred_at: datetime
    raw_diagnostic: str  # what the provider called it, for the unmapped case


@runtime_checkable
class BankProvider(Protocol):
    """Seven methods. None of them moves money."""

    def authorize_url(
        self, *, state: str, redirect_uri: str, scopes: tuple[str, ...]
    ) -> str: ...

    def exchange_code(self, *, code: str, redirect_uri: str) -> TokenSet: ...

    def refresh(self, *, refresh_token: str) -> TokenSet: ...

    def accounts(self, *, access_token: str) -> tuple[Account, ...]: ...

    def transactions(
        self,
        *,
        access_token: str,
        account_id: str,
        since: datetime,
        until: datetime,
    ) -> tuple[Transaction, ...]: ...

    def verify_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> bool: ...

    def parse_return_event(self, *, body: bytes) -> ReturnEvent | None: ...


# DECLARED, never derived from the Protocol, because a value derived from its
# own source agrees with it by construction and the control becomes vacuous.
# _validate_protocol() compares this against the Protocol in both directions,
# so a rename on either side breaks the import of this module.
REQUIRED_METHODS: tuple[str, ...] = (
    "accounts",
    "authorize_url",
    "exchange_code",
    "parse_return_event",
    "refresh",
    "transactions",
    "verify_webhook",
)

MONEY_NOUNS: frozenset[str] = frozenset(
    {
        "payment",
        "payments",
        "payout",
        "payouts",
        "transfer",
        "transfers",
        "mandate",
        "mandates",
        "remittance",
        "remittances",
        "disbursement",
        "disbursements",
    }
)

MUTATING_VERBS: frozenset[str] = frozenset(
    {
        "create",
        "initiate",
        "submit",
        "send",
        "execute",
        "make",
        "post",
        "schedule",
        "setup",
        "authorise",
        "authorize",
        "issue",
        "release",
        "trigger",
    }
)

# A bare money noun as a method name, plus the two names that carry no noun
# and still mean initiation.
HARD_BANNED_METHODS: frozenset[str] = frozenset(MONEY_NOUNS | {"pay", "pis"})

# Path segments that only ever appear on a payment-initiation endpoint.
DENIED_PATH_WORDS: frozenset[str] = frozenset(MONEY_NOUNS | {"pis"})


def _words(name: str) -> frozenset[str]:
    return frozenset(p for p in name.lower().replace("-", "_").split("_") if p)


def _method_refusal(name: str) -> str | None:
    """Why this method name may not exist here, or None."""
    lowered = name.lower()
    if lowered in HARD_BANNED_METHODS:
        return f"{name!r} is a money noun, and this package is read-only"
    parts = _words(name)
    verbs = parts & MUTATING_VERBS
    nouns = parts & MONEY_NOUNS
    if verbs and nouns:
        return (
            f"{name!r} carries the verb {sorted(verbs)!r} with the noun "
            f"{sorted(nouns)!r}, which is payment initiation"
        )
    return None


def _endpoint_refusal(url: str) -> str | None:
    """Why this declared endpoint may not exist here, or None.

    Only the PATH is read. A host is not checked, because a bank whose
    hostname contains a money noun is ordinary and refusing it would block
    correct work.
    """
    path = urlsplit(url).path if "//" in url else url
    for segment in path.split("/"):
        hit = _words(segment) & DENIED_PATH_WORDS
        if hit:
            return (
                f"endpoint {url!r} has the path segment {segment!r}, which "
                f"names {sorted(hit)!r}"
            )
    return None


def _validate_protocol() -> None:
    declared = set(REQUIRED_METHODS)
    on_protocol = {
        name
        for name, value in vars(BankProvider).items()
        if not name.startswith("_") and callable(value)
    }
    if declared != on_protocol:
        raise RegistryError(
            "REQUIRED_METHODS and BankProvider disagree: "
            f"declared-only {sorted(declared - on_protocol)!r}, "
            f"protocol-only {sorted(on_protocol - declared)!r}"
        )
    if sorted(REQUIRED_METHODS) != list(REQUIRED_METHODS):
        raise RegistryError("REQUIRED_METHODS is not sorted")
    for name in REQUIRED_METHODS:
        if _method_refusal(name) is not None:
            raise RegistryError(
                f"the protocol itself declares {name!r}, which the deny-list "
                "refuses; the protocol is wrong, not the deny-list"
            )


_validate_protocol()


def _validate_adapter(adapter: type) -> None:
    if not inspect.isclass(adapter):
        raise RegistryError(f"register expects a class, got {type(adapter).__name__}")

    for name, value in inspect.getmembers(adapter, callable):
        if name.startswith("_"):
            continue
        reason = _method_refusal(name)
        if reason is not None:
            raise PaymentInitiationRefused(
                f"{adapter.__name__} declares a method that could move money: {reason}"
            )

    endpoints = getattr(adapter, "ENDPOINTS", None)
    if endpoints is not None:
        if isinstance(endpoints, Mapping):
            pairs = list(endpoints.items())
        else:
            pairs = [(str(i), v) for i, v in enumerate(endpoints)]
        for key, url in pairs:
            if not isinstance(url, str):
                raise RegistryError(
                    f"{adapter.__name__}.ENDPOINTS[{key!r}] is not a string"
                )
            reason = _endpoint_refusal(url)
            if reason is not None:
                raise PaymentInitiationRefused(
                    f"{adapter.__name__} declares a payment endpoint: {reason}"
                )

    missing = [m for m in REQUIRED_METHODS if not callable(getattr(adapter, m, None))]
    if missing:
        raise RegistryError(
            f"{adapter.__name__} does not implement {missing!r} from BankProvider"
        )


_REGISTERED: dict[str, type] = {}


def register(adapter: type) -> type:
    """Class decorator. Refuses at the import of the module declaring it."""
    _validate_adapter(adapter)
    name = getattr(adapter, "PROVIDER_NAME", None)
    if not isinstance(name, str) or not name:
        raise RegistryError(
            f"{adapter.__name__} must set PROVIDER_NAME to a non-empty string"
        )
    if name in _REGISTERED and _REGISTERED[name] is not adapter:
        raise RegistryError(f"two adapters both claim PROVIDER_NAME {name!r}")
    _REGISTERED[name] = adapter
    return adapter


def providers() -> tuple[str, ...]:
    return tuple(sorted(_REGISTERED))


def get(name: object) -> type:
    if not isinstance(name, str):
        raise RegistryError(f"provider name must be a string, got {type(name).__name__}")
    try:
        return _REGISTERED[name]
    except KeyError:
        raise RegistryError(
            f"unknown provider {name!r}; known: {providers()!r}"
        ) from None
