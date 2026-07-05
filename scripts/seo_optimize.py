#!/usr/bin/env python3
"""Auto SEO / AEO / GEO / CRO optimizer + re-indexer for the PayPilot frontend.

Audits the static site for search (SEO), answer-engine (AEO/GEO) and conversion
(CRO) health, prints a PASS/FAIL report, and re-submits the sitemap to IndexNow
so search + AI engines re-crawl. Run on every deploy (or manually). Exits 1 on a
CRITICAL regression so a bad frontend change gates the deploy - it never rewrites
pages blindly (safe by design); it flags what a human should fix.
"""
import re
import sys
import json
import urllib.request
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HOST = "paypilot.fly.dev"
KEY = "9bb79b93b9818189cfe6fe608bea1bca"


def read(name: str) -> str:
    p = STATIC / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def audit():
    html = read("index.html")
    out = []

    def c(name, ok, critical=False):
        out.append((name, bool(ok), critical))

    # SEO
    title = re.search(r"<title>([^<]+)</title>", html)
    c("SEO title present & <=60 chars", title and len(title.group(1)) <= 60, True)
    md = re.search(r'<meta name="description" content="([^"]+)"', html)
    c("SEO meta description & <=160 chars", md and len(md.group(1)) <= 160, True)
    c("SEO Open Graph (title/description/url)", all(f"og:{k}" in html for k in ("title", "description", "url")), True)
    c("SEO mobile viewport", 'name="viewport"' in html, True)
    # AEO / GEO
    c("AEO JSON-LD SoftwareApplication", '"SoftwareApplication"' in html, True)
    c("AEO JSON-LD FAQPage", '"FAQPage"' in html, True)
    c("GEO llms.txt non-empty", bool(read("llms.txt").strip()), True)
    robots = read("robots.txt")
    c("GEO robots.txt allows + links sitemap", "Sitemap:" in robots and "Allow: /" in robots, True)
    c("GEO sitemap.xml has URLs", "<loc>" in read("sitemap.xml"))
    # CRO
    c("CRO primary CTA present", "btn primary" in html, True)
    c("CRO H1 present", "<h1" in html)
    c("CRO hire + demo + source CTAs", all(k in html.lower() for k in ("hire", "demo", "github")))
    # PERF (CWV proxies - the page must stay light + non-render-blocking)
    idx = STATIC / "index.html"
    c("PERF index.html < 100KB", idx.exists() and idx.stat().st_size < 100_000)
    c("PERF no render-blocking external CSS", not re.search(r'<link[^>]*rel="stylesheet"[^>]*href="https?://', html))
    # A11y
    c("A11y html lang set", bool(re.search(r"<html[^>]*lang=", html)), True)
    c("A11y every image has alt", all("alt=" in m for m in re.findall(r"<img[^>]*>", html)))
    # Schema depth
    c("Schema Organization", '"Organization"' in html)
    c("Schema WebSite", '"WebSite"' in html)
    # Compliance (EU AI Act + governance) - every project must ship + link a statement
    c("COMPLIANCE page present + linked", (STATIC / "compliance.html").exists() and "/compliance" in html, True)
    return out


def lighthouse():
    """Real Lighthouse scores via PageSpeed Insights - activates when
    PAGESPEED_API_KEY is set (free key from Google Cloud). Returns None when
    unset so the optimizer still runs on heuristics alone."""
    import os
    key = os.environ.get("PAGESPEED_API_KEY")
    if not key:
        return None
    cats = "".join(f"&category={c}" for c in ("performance", "accessibility", "seo", "best-practices"))
    url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url=https://{HOST}/&strategy=mobile&key={key}{cats}"
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            d = json.load(r)
        c = d.get("lighthouseResult", {}).get("categories", {})
        return {k: round((v.get("score") or 0) * 100) for k, v in c.items()}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:60]}


def reindex() -> str:
    urls = [f"https://{HOST}/", f"https://{HOST}/pricing", f"https://{HOST}/terms"]
    body = json.dumps({
        "host": HOST, "key": KEY,
        "keyLocation": f"https://{HOST}/{KEY}.txt",
        "urlList": urls,
    }).encode()
    req = urllib.request.Request(
        "https://api.indexnow.org/indexnow", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return f"HTTP {r.status}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {str(exc)[:80]}"


def main() -> None:
    checks = audit()
    print("PayPilot SEO / AEO / GEO / CRO audit:")
    for name, ok, critical in checks:
        tag = "PASS" if ok else ("FAIL (critical)" if critical else "warn")
        print(f"  {tag:16} {name}")
    print(f"\nIndexNow re-submit: {reindex()}")
    lh = lighthouse()
    if lh is not None:
        print("\nLighthouse (PageSpeed Insights, mobile):")
        for k, v in lh.items():
            print(f"  {k}: {v}")
    critical_fails = [n for n, ok, crit in checks if not ok and crit]
    if isinstance(lh, dict) and isinstance(lh.get("performance"), int) and lh["performance"] < 80:
        critical_fails.append("Lighthouse performance < 80")
    if critical_fails:
        print(f"\nCRITICAL regressions block deploy: {critical_fails}")
        sys.exit(1)
    print("\nAll critical checks pass - frontend is SEO/AEO/GEO/CRO healthy.")


if __name__ == "__main__":
    main()
