"""Tests for Stripe Connect + application-fee charge execution.

The Stripe SDK is replaced with an in-memory fake, so nothing hits the network
and no real money moves. Covers the fee math, the connected-account routing, and
the safe-by-default behaviour (503 when no key is configured).
"""

from __future__ import annotations

from collections import OrderedDict, deque
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import stripe_pay


class _FakeStripe:
    """Minimal stand-in for the stripe module used by app.stripe_pay."""

    api_key = None

    class Account:
        @staticmethod
        def create(**kwargs):
            return SimpleNamespace(id="acct_test_123")

    class AccountLink:
        @staticmethod
        def create(**kwargs):
            return SimpleNamespace(url="https://connect.stripe.com/setup/s/test")

    class PaymentIntent:
        last_kwargs: dict = {}

        @staticmethod
        def create(**kwargs):
            _FakeStripe.PaymentIntent.last_kwargs = kwargs
            return SimpleNamespace(id="pi_test_1", status="succeeded")


@pytest.fixture
def stripe_on(monkeypatch):
    """Configure a (fake) platform key and swap in the fake SDK."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setattr(stripe_pay, "stripe", _FakeStripe)
    return monkeypatch


@pytest.fixture
def stripe_off(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# Unit: config + fee math
# ---------------------------------------------------------------------------

def test_is_configured_reflects_key(stripe_off):
    assert stripe_pay.is_configured() is False


def test_application_fee_percent_default_and_override(monkeypatch):
    monkeypatch.delenv("STRIPE_APPLICATION_FEE_PERCENT", raising=False)
    assert stripe_pay.application_fee_percent() == 15.0
    monkeypatch.setenv("STRIPE_APPLICATION_FEE_PERCENT", "12.5")
    assert stripe_pay.application_fee_percent() == 12.5


def test_execute_recovery_charge_computes_fee_and_routes(stripe_on):
    result = stripe_pay.execute_recovery_charge(
        account_id="acct_client", amount=1499.0, currency="usd", fee_percent=15
    )
    # 1499.00 -> 149900 cents; 15% -> 22485 cents.
    assert result["amount"] == 149900
    assert result["application_fee_amount"] == 22485
    assert result["connected_account"] == "acct_client"
    assert result["status"] == "succeeded"
    # Direct charge on the connected account carries the platform fee.
    sent = _FakeStripe.PaymentIntent.last_kwargs
    assert sent["stripe_account"] == "acct_client"
    assert sent["application_fee_amount"] == 22485
    assert sent["confirm"] is True and sent["off_session"] is True


def test_create_connect_onboarding_returns_account_and_url(stripe_on):
    out = stripe_pay.create_connect_onboarding("https://x/return", "https://x/refresh")
    assert out["account_id"] == "acct_test_123"
    assert out["onboarding_url"].startswith("https://connect.stripe.com/")


def test_execute_without_key_raises(stripe_off):
    with pytest.raises(RuntimeError):
        stripe_pay.execute_recovery_charge(account_id="acct_x", amount=10.0)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_connect_onboard_503_without_key(stripe_off):
    client = TestClient(api_module.app)
    r = client.post("/connect/onboard", json={})
    assert r.status_code == 503


def test_recover_charge_503_without_key(stripe_off):
    client = TestClient(api_module.app)
    r = client.post("/recover/charge", json={"account_id": "acct_x", "amount": 10.0, "currency": "usd"})
    assert r.status_code == 503


def test_connect_onboard_returns_link_when_configured(stripe_on):
    client = TestClient(api_module.app)
    r = client.post("/connect/onboard", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == "acct_test_123"
    assert body["onboarding_url"].startswith("https://connect.stripe.com/")


def test_recover_charge_executes_with_fee(stripe_on, monkeypatch):
    # Fresh rate state so the charge isn't throttled by earlier tests.
    monkeypatch.setattr(api_module, "_rate_hits", OrderedDict())
    monkeypatch.setattr(api_module, "_global_hits", deque())
    client = TestClient(api_module.app)
    r = client.post(
        "/recover/charge",
        json={"account_id": "acct_client", "amount": 200.0, "currency": "usd", "fee_percent": 15},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["amount"] == 20000
    assert body["application_fee_amount"] == 3000  # 15% of 200.00
    assert body["connected_account"] == "acct_client"


def test_config_reports_stripe_disabled(stripe_off):
    client = TestClient(api_module.app)
    assert client.get("/config").json()["stripe_enabled"] is False
