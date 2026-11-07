# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The usage tally: it must answer "has anyone outside called the API", and say so when it cannot."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import usage

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture()
def vol(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(tmp_path / "store.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "adm-test")
    usage.reset_for_tests()
    return tmp_path


def _client():
    from app.api import app

    return TestClient(app)


ADMIN = {"Authorization": "Bearer adm-test"}


def test_off_and_says_so_when_there_is_no_volume(monkeypatch):
    monkeypatch.delenv("PAYPILOT_DB_PATH", raising=False)
    assert usage.record("GET", "/x", 200, "1.2.3.0/24") is False
    s = usage.summary()
    assert s["enabled"] is False and "PAYPILOT_DB_PATH" in s["reason"]


def test_a_repeat_call_increments_one_row_and_counts_prefixes(vol):
    for _ in range(3):
        usage.record("POST", "/decide", 200, "5.6.7.0/24", now=NOW)
    usage.record("POST", "/decide", 200, "8.8.8.0/24", now=NOW)
    usage.record("POST", "/decide", 401, "8.8.8.0/24", now=NOW)
    s = usage.summary(now=NOW)
    (route,) = s["routes"]
    assert route["route"] == "/decide" and route["calls"] == 5
    assert (route["ok_2xx"], route["rejected_4xx"], route["errors_5xx"]) == (4, 1, 0)
    assert route["distinct_prefixes"] == 2
    assert route["top_prefixes"][0] == {"prefix": "5.6.7.0/24", "calls": 3}
    with sqlite3.connect(vol / "usage.db") as c:
        assert c.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 3


def test_excluding_my_own_prefix_leaves_everyone_else(vol):
    usage.record("POST", "/decide", 200, "9.9.9.0/24", now=NOW)
    usage.record("POST", "/decide", 200, "9.9.9.0/24", now=NOW)
    usage.record("POST", "/decide", 200, "4.4.4.0/24", now=NOW)
    all_ = usage.summary(now=NOW)
    others = usage.summary(exclude=("9.9.9.0/24",), now=NOW)
    assert all_["total_calls"] == 3
    assert others["total_calls"] == 1 and others["routes"][0]["top_prefixes"][0]["prefix"] == "4.4.4.0/24"
    only_me = usage.summary(exclude=("4.4.4.0/24",), now=NOW)
    assert only_me["total_calls"] == 2, "the control: excluding someone else must not remove my rows"


def test_the_window_and_retention_are_honoured(vol):
    old = NOW - timedelta(days=usage.RETENTION_DAYS + 5)
    usage.record("GET", "/old", 200, "1.1.1.0/24", now=old)
    usage.record("GET", "/recent", 200, "1.1.1.0/24", now=NOW - timedelta(days=3))
    usage.record("GET", "/today", 200, "1.1.1.0/24", now=NOW)
    names = {r["route"] for r in usage.summary(days=7, now=NOW)["routes"]}
    assert names == {"/recent", "/today"}
    assert {r["route"] for r in usage.summary(days=2, now=NOW)["routes"]} == {"/today"}
    with sqlite3.connect(vol / "usage.db") as c:
        assert c.execute("SELECT COUNT(*) FROM api_usage WHERE route='/old'").fetchone()[0] == 0, (
            "rows past retention are deleted, not just hidden"
        )


def test_a_write_failure_never_raises_and_is_reported(vol, monkeypatch):
    (vol / "usage.db").mkdir()  # a directory where the file should be: sqlite cannot open it
    assert usage.record("GET", "/x", 200, "1.2.3.0/24") is False
    assert usage._write_failures == 1  # noqa: SLF001
    s = usage.summary()
    assert s["readable"] is False and "routes" not in s, "an unreadable tally must not read as a quiet week"
    assert s["write_failures_since_boot"] == 1


def test_only_the_route_template_is_stored_never_the_concrete_path(vol):
    c = _client()
    c.get("/decisions/INV-SECRET-123")
    c.get("/decisions/INV-SECRET-456")
    s = usage.summary()
    routes = {r["route"] for r in s["routes"]}
    assert "/decisions/{invoice_id}" in routes
    with sqlite3.connect(vol / "usage.db") as db:
        blob = " ".join(str(v) for row in db.execute("SELECT * FROM api_usage") for v in row)
    assert "INV-SECRET" not in blob


def test_a_request_that_matched_no_route_is_counted_as_unmatched(vol):
    _client().get("/wp-login.php")
    (route,) = usage.summary()["routes"]
    assert route["route"] == usage.UNMATCHED and route["rejected_4xx"] == 1


def test_health_static_and_usage_itself_are_not_recorded(vol):
    c = _client()
    c.get("/health")
    c.get("/static/does-not-exist.css")
    c.get("/usage", headers=ADMIN)
    assert usage.summary()["routes"] == []
    c.get("/refusals-sample")
    assert [r["route"] for r in usage.summary()["routes"]] == ["/refusals-sample"], (
        "the control: a public page view is recorded, so an empty list above means skipped, not broken"
    )


def test_server_errors_are_classified_as_errors_not_rejections(vol):
    usage.record("POST", "/decide", 503, "1.1.1.0/24", now=NOW)
    r = usage.summary(now=NOW)["routes"][0]
    assert (r["ok_2xx"], r["rejected_4xx"], r["errors_5xx"]) == (0, 0, 1)


def test_the_usage_route_is_admin_gated_and_hidden_from_the_schema(vol):
    from app.api import app

    c = _client()
    assert c.get("/usage").status_code == 401
    assert c.get("/usage", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = c.get("/usage", headers=ADMIN)
    assert ok.status_code == 200 and ok.json()["enabled"] is True
    assert "/usage" not in app.openapi()["paths"]


def test_the_exclude_query_removes_my_prefix_through_the_route(vol):
    usage.record("POST", "/decide", 200, "9.9.9.0/24")
    usage.record("POST", "/decide", 200, "4.4.4.0/24")
    c = _client()
    body = c.get("/usage?exclude=9.9.9.0/24", headers=ADMIN).json()
    assert body["total_calls"] == 1 and body["excluded_prefixes"] == ["9.9.9.0/24"]
