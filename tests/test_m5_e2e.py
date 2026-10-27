"""One shared deployment, two merchants, the whole path: webhook, approval request, approval, send.

Proves the pieces compose: each tenant's mail leaves under its own key to its own approver, nothing
reaches a customer before a person approves, and the other tenant sees nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import stripe_client, tenant_admin
from app.api import app
from app.store import get_database, get_platform, reset_database, store_for

client = TestClient(app, raise_server_exceptions=False)
BASE = "https://pay.platform.test"
PORTAL = "https://billing.stripe.com/p/session/test"


class _Resp:
    status_code = 200
    content = b"{}"

    @staticmethod
    def json():
        return {"id": "rs_ok"}


@pytest.fixture
def world(monkeypatch, isolated_store):
    monkeypatch.setenv("PAYPILOT_TENANCY", "multi")
    monkeypatch.setenv("PAYPILOT_TENANT_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PAYPILOT_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("PAYPILOT_SEND_COOLDOWN_HOURS", "0")
    for name in ("OPENAI_API_KEY", "STRIPE_WEBHOOK_SECRET", "PAYPILOT_SEND_EMAIL",
                 "PAYPILOT_APPROVER_EMAILS", "PAYPILOT_ALLOWED_RECIPIENTS", "RESEND_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    import app.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "_retriever", None)
    monkeypatch.setattr(stripe_client, "create_portal_session", lambda cid, *_a, **_k: PORTAL)
    sent: list[dict] = []

    def _post(url, json=None, headers=None, timeout=None):
        sent.append({"to": json["to"], "text": json["text"], "key": (headers or {}).get("Authorization", "")})
        return _Resp()

    monkeypatch.setattr("httpx.post", _post)
    from collections import OrderedDict

    from app import api as api_module

    monkeypatch.setattr(api_module, "_idem_store", OrderedDict())  # the replay cache is process-wide
    reset_database()
    platform, db = get_platform(), get_database()
    for tid in ("alpha", "beta"):
        tenant_admin.create_tenant(platform, db, tid, tid.title())
        tenant_admin.rotate_secrets(db, tid, {"stripe_webhook_secret": f"whsec_{tid}",
                                              "resend_api_key": f"re_{tid}"})
        tenant_admin.update_config(db, tid, {"approver_emails": [f"boss@{tid}.test"],
                                             "update_url": f"https://pay.{tid}.test/card",
                                             "allowed_link_hosts": [f"pay.{tid}.test"]})
        tenant_admin.set_platform_fields(
            db, tid, send_enabled=True, allowed_recipients=[f"cust@{tid}.test"],
            allowed_from_domains=[f"{tid}.test"],
        )
    yield sent
    reset_database()


def _event(invoice_id, customer_email):
    return {
        "id": f"evt_{invoice_id}", "type": "invoice.payment_failed",
        "data": {"object": {
            "object": "invoice", "id": invoice_id, "customer": "cus_X", "subscription": "sub_X",
            "customer_email": customer_email, "amount_due": 4900, "currency": "eur",
            "attempt_count": 1,
            "payment_intent": {"last_payment_error": {"decline_code": "expired_card"}},
            "metadata": {"paypilot_customer_id": "cust_001"},
        }},
    }


def _deliver(tenant, event):
    raw = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(f"whsec_{tenant}".encode(), f"{ts}".encode() + b"." + raw, hashlib.sha256).hexdigest()
    return client.post(f"/t/{tenant}/webhooks/stripe", content=raw,
                       headers={"stripe-signature": f"t={ts},v1={sig}"})


def test_each_tenants_approval_and_send_stay_inside_that_tenant(world):
    sent = world
    assert _deliver("alpha", _event("in_a", "cust@alpha.test")).status_code == 200
    assert _deliver("beta", _event("in_b", "cust@beta.test")).status_code == 200

    # approval requests: to each tenant's own approver, under each tenant's own key, no customer mail yet
    by_to = {tuple(p["to"]): p for p in sent}
    assert by_to[("boss@alpha.test",)]["key"] == "Bearer re_alpha"
    assert by_to[("boss@beta.test",)]["key"] == "Bearer re_beta"
    assert not [p for p in sent if any(t.startswith("cust@") for t in p["to"])]

    link = re.search(r"/approve/(\d+)\?t=([0-9a-f]+)", by_to[("boss@alpha.test",)]["text"])
    approval_id, token = int(link.group(1)), link.group(2)
    assert get_platform().approval_tenant(approval_id) == "alpha"

    page = client.get(f"/approve/{approval_id}?t={token}")
    assert page.status_code == 200
    assert not [p for p in sent if any(t.startswith("cust@") for t in p["to"])], "viewing sends nothing"

    done = client.post(f"/approve/{approval_id}", content=f"t={token}&action=approve",
                       headers={"content-type": "application/x-www-form-urlencoded"})
    assert done.status_code == 200
    customer_mails = [p for p in sent if any(t.startswith("cust@") for t in p["to"])]
    assert len(customer_mails) == 1
    assert customer_mails[0]["to"] == ["cust@alpha.test"] and customer_mails[0]["key"] == "Bearer re_alpha"

    assert store_for("alpha").sent_message_count("in_a") == 1
    assert store_for("beta").sent_message_count("in_b") == 0
    assert store_for("alpha").get_failure("in_b") is None
    assert store_for("beta").get_failure("in_a") is None


def test_a_link_for_one_tenant_is_nothing_in_another(world):
    sent = world
    _deliver("alpha", _event("in_a", "cust@alpha.test"))
    link = re.search(r"/approve/(\d+)\?t=([0-9a-f]+)", next(p for p in sent if p["to"] == ["boss@alpha.test"])["text"])
    approval_id, token = int(link.group(1)), link.group(2)
    # a wrong token and an unknown id look the same
    assert client.get(f"/approve/{approval_id}?t={'0' * 64}").status_code == 404
    assert client.get(f"/approve/9999?t={token}").status_code == 404


def test_a_suspended_tenant_cannot_approve_a_pending_request(world):
    sent = world
    _deliver("alpha", _event("in_a", "cust@alpha.test"))
    link = re.search(r"/approve/(\d+)\?t=([0-9a-f]+)", next(p for p in sent if p["to"] == ["boss@alpha.test"])["text"])
    approval_id, token = int(link.group(1)), link.group(2)
    tenant_admin.suspend(get_platform(), store_for("alpha"), "unpaid")
    done = client.post(f"/approve/{approval_id}", content=f"t={token}&action=approve",
                       headers={"content-type": "application/x-www-form-urlencoded"})
    assert done.status_code in (200, 404)
    assert not [p for p in sent if p["to"] == ["cust@alpha.test"]]
