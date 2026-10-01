"""The Recharge onboarding script: dry run by default, idempotent, never leaks the token."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "recharge_register_webhooks.py"
_spec = importlib.util.spec_from_file_location("recharge_register_webhooks", _PATH)
reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reg)

TOKEN = "sk_test_never_print_me"
ADDRESS = "https://paypilot.fly.dev/webhooks/recharge"


class FakeApi:
    def __init__(self, listing=None):
        self.listing = listing if listing is not None else {"webhooks": []}
        self.posts = []

    def __call__(self, method, token, body=None):
        assert token == TOKEN
        if method == "GET":
            return self.listing
        self.posts.append(body)
        return {"webhook": {"id": len(self.posts), **body}}


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("RECHARGE_API_TOKEN", TOKEN)


def test_dry_run_creates_nothing(token, capsys):
    api = FakeApi()
    assert reg.main([], request=api) == 0
    assert api.posts == []
    out = capsys.readouterr().out
    assert "would create  charge/failed" in out and "Dry run" in out


def test_apply_creates_both_topics(token):
    api = FakeApi()
    assert reg.main(["--apply"], request=api) == 0
    assert [p["topic"] for p in api.posts] == ["charge/failed", "charge/max_retries_reached"]
    assert all(p["address"] == ADDRESS for p in api.posts)


def test_already_registered_topic_is_skipped(token):
    api = FakeApi({"webhooks": [{"topic": "charge/failed", "address": ADDRESS}]})
    assert reg.main(["--apply"], request=api) == 0
    assert [p["topic"] for p in api.posts] == ["charge/max_retries_reached"]


def test_same_topic_on_another_address_is_not_skipped(token):
    api = FakeApi([{"topic": "charge/failed", "address": "https://elsewhere.example/hook"}])
    assert reg.main(["--apply"], request=api) == 0
    assert len(api.posts) == 2


def test_missing_token_refuses(monkeypatch, capsys):
    monkeypatch.delenv("RECHARGE_API_TOKEN", raising=False)
    assert reg.main([], request=FakeApi()) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_http_base_url_refuses(token):
    assert reg.main(["--base-url", "http://insecure.example"], request=FakeApi()) == 1


def test_token_never_printed(token, capsys):
    reg.main(["--apply"], request=FakeApi())
    assert TOKEN not in capsys.readouterr().out
