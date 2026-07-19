"""Currency-aware conversion between Stripe's minor units and display amounts.

Stripe sends every amount as an integer in the currency's smallest unit, but
"smallest unit" is not always 1/100. JPY is whole yen; KWD has three decimals.
A flat divide by 100 under-reports a Japanese invoice by 100x and over-reports
a Kuwaiti one by 10x, in the dashboard, the impact block, and any email that
quotes an amount.

Storage is unaffected and stays exact: the ledger always holds the integer
minor-unit value Stripe sent, alongside its currency code. This module is only
for turning that into something a human reads.

Lives in its own module so both :mod:`app.stripe_map` and :mod:`app.report` can
use one table without an import cycle.
"""

from __future__ import annotations

#: Currencies with no minor unit. Stripe sends these as whole major units.
ZERO_DECIMAL = frozenset(
    {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
        "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
)

#: Currencies with three decimal places.
THREE_DECIMAL = frozenset({"bhd", "jod", "kwd", "omr", "tnd"})


def exponent(currency: str) -> int:
    """Number of decimal places for ``currency`` (0, 2 or 3)."""
    code = (currency or "").strip().lower()
    if code in ZERO_DECIMAL:
        return 0
    if code in THREE_DECIMAL:
        return 3
    return 2


def minor_to_major(amount_minor: int, currency: str) -> float:
    """Convert integer minor units into a displayable amount."""
    places = exponent(currency)
    if places == 0:
        return float(int(amount_minor or 0))
    return round((amount_minor or 0) / (10**places), places)
