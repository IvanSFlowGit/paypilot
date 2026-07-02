"""Stripe Connect + application-fee charge execution - the paid, auto path.

This is what makes PayPilot's 15% collect itself. Your personal Stripe (the key
you set) is the PLATFORM; each client connects their own Stripe as a Standard
connected account; PayPilot executes the recovery charge on the client's account
with an ``application_fee`` that auto-routes your cut to the platform.

Safe by default: every function requires ``STRIPE_SECRET_KEY`` (a Fly secret,
never hardcoded). With no key set, :func:`is_configured` is False and the API
endpoints return 503, so the free keyless demo is completely untouched.

Build and verify against Stripe TEST keys first; flip to live only when the fee
split is proven.
"""

from __future__ import annotations

import os

import stripe

# Default platform take rate. Matches the /pricing page (flat 15%).
_DEFAULT_FEE_PERCENT = 15.0


def application_fee_percent() -> float:
    """Platform take rate as a percent (env override, default 15)."""
    try:
        return float(os.getenv("STRIPE_APPLICATION_FEE_PERCENT", str(_DEFAULT_FEE_PERCENT)))
    except ValueError:
        return _DEFAULT_FEE_PERCENT


def is_configured() -> bool:
    """True when a platform Stripe secret key is set (paid features enabled)."""
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def _client():
    """Return the stripe module with the platform key applied, or raise if unset."""
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not set - Stripe paid features are disabled.")
    stripe.api_key = key
    return stripe


def create_connect_onboarding(return_url: str, refresh_url: str) -> dict:
    """Create a Standard connected account for a client and an onboarding link.

    The client completes onboarding at ``onboarding_url``; store the returned
    ``account_id`` against that client to charge them later.
    """
    s = _client()
    account = s.Account.create(type="standard")
    link = s.AccountLink.create(
        account=account.id,
        return_url=return_url,
        refresh_url=refresh_url,
        type="account_onboarding",
    )
    return {"account_id": account.id, "onboarding_url": link.url}


def execute_recovery_charge(
    *,
    account_id: str,
    amount: float,
    currency: str = "usd",
    customer: str | None = None,
    payment_method: str | None = None,
    fee_percent: float | None = None,
) -> dict:
    """Charge a recovered payment on the client's connected account, taking a fee.

    Creates an off-session, auto-confirming PaymentIntent as a direct charge on
    the connected account (``stripe_account=account_id``) with
    ``application_fee_amount`` routing the platform's cut back to you. ``amount``
    is in major units (e.g. dollars); Stripe works in the minor unit, so it is
    converted to cents here.
    """
    s = _client()
    pct = application_fee_percent() if fee_percent is None else float(fee_percent)
    amount_minor = int(round(float(amount) * 100))
    fee_minor = int(round(amount_minor * pct / 100.0))

    params: dict = {
        "amount": amount_minor,
        "currency": (currency or "usd").lower(),
        "application_fee_amount": fee_minor,
        "off_session": True,
        "confirm": True,
    }
    if customer:
        params["customer"] = customer
    if payment_method:
        params["payment_method"] = payment_method

    intent = s.PaymentIntent.create(stripe_account=account_id, **params)
    return {
        "payment_intent_id": getattr(intent, "id", None),
        "status": getattr(intent, "status", None),
        "amount": amount_minor,
        "application_fee_amount": fee_minor,
        "currency": params["currency"],
        "connected_account": account_id,
    }
