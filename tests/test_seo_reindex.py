"""IndexNow submits every page in the sitemap, not a list somebody typed.

THE LIST WAS HARDCODED TO THREE AND THE SITE SERVES FIVE. Measured 2026-10-02:
/loadtest had been live since August and /ach-returns since October, and neither
was ever submitted, so every deploy told the search and answer engines about three
pages. Nothing errored, because a hardcoded list cannot report what is absent from
it. This is the same shape as any list standing in for the population it describes.

scripts/seo_optimize.py is the Fly release command, so a regression here gates a
deploy. These tests run offline and make no network call: reindex() is never
invoked, only the URL derivation it depends on.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PATH = ROOT / "scripts" / "seo_optimize.py"
_spec = importlib.util.spec_from_file_location("seo_optimize", _PATH)
seo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seo)

SITEMAP = ROOT / "app" / "static" / "sitemap.xml"
#: Pages that are deliberately not in the sitemap: the root is submitted as "/",
#: og is a share-card fragment, and billing-update is a stand-in reached by link.
NOT_INDEXED = {"index", "og", "billing-update"}


def _sitemap_locs() -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", SITEMAP.read_text(encoding="utf-8"))


def test_control_the_sitemap_has_more_than_the_old_hardcoded_three():
    """The defect is only visible if the sitemap genuinely holds more than the
    three URLs the old list carried."""
    assert len(_sitemap_locs()) >= 5


def test_every_sitemap_url_is_submitted():
    assert seo.sitemap_urls() == list(dict.fromkeys(_sitemap_locs()))


def test_the_new_pages_are_actually_in_what_gets_submitted():
    submitted = " ".join(seo.sitemap_urls())
    for page in ("/loadtest", "/ach-returns"):
        assert page in submitted, page


def test_no_url_is_submitted_twice():
    urls = seo.sitemap_urls()
    assert len(urls) == len(set(urls))


def test_every_served_page_is_in_the_sitemap():
    pages = {p.stem for p in (ROOT / "app" / "static").glob("*.html")} - NOT_INDEXED
    listed = " ".join(seo.sitemap_urls())
    missing = sorted(p for p in pages if p not in listed)
    assert not missing, f"served but never submitted: {missing}"


def test_reindex_refuses_rather_than_submitting_nothing(monkeypatch):
    """An empty sitemap must not silently post an empty list and report success.
    No network call is made: the guard returns before the request is built."""
    monkeypatch.setattr(seo, "read", lambda name: "" if name == "sitemap.xml" else "x")
    monkeypatch.setattr(
        seo.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reindex opened a socket")),
    )
    assert "refused" in seo.reindex()


def test_the_audit_reports_sitemap_coverage():
    names = [name for name, _, _ in seo.audit()]
    assert any("sitemap covers every page" in n for n in names)
