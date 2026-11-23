"""The strict policy drops inline script, so no served page may depend on it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PAGES = sorted(p for p in STATIC.glob("*.html") if p.name not in {"og.html"})


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_served_page_carries_inline_script_or_event_handlers(page):
    text = page.read_text(encoding="utf-8")
    for m in re.finditer(r"<script\b([^>]*)>", text, re.I):
        attrs = m.group(1)
        assert "src=" in attrs or "application/ld+json" in attrs, f"{page.name}: inline script"
    assert not re.search(r"\son[a-z]+\s*=", text, re.I), f"{page.name}: inline event handler"
    assert "javascript:" not in text.lower(), f"{page.name}: javascript: URL"


def test_the_script_the_index_loads_is_served_and_carries_no_inline_handlers():
    assert re.search(r'<script src="/static/paypilot\.js"', (STATIC / "index.html").read_text(encoding="utf-8"))
    js = (STATIC / "paypilot.js").read_text(encoding="utf-8")
    assert not re.search(r"\bon[a-z]+\s*=\s*[\"']", js), "an inline handler inside a JS template"
    assert "eval(" not in js and "new Function" not in js


def test_the_page_policy_allows_no_inline_script_and_the_script_file_is_served():
    c = TestClient(api.app)
    r = c.get("/static/paypilot.js")
    assert r.status_code == 200
    csp = c.get("/static/index.html").headers["content-security-policy"]
    script_src = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
    assert "unsafe-inline" not in script_src and "'self'" in script_src
