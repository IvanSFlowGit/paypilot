#!/usr/bin/env python3
"""Refuse to ship public prose or styling that reads as AI-generated, and refuse to ship
anything no person has looked at.

Two separate gates, because they catch different things.

1. TELLS. Mechanical patterns that read as machine-made to a person: filler vocabulary,
   dashes, emoji, negative-parallelism sentences, and the visual habits of generated pages
   (gradient text, glow shadows, pill badges, blur). A pattern is only a candidate for a
   person to judge; it never proves authorship. It also cannot see what no pattern can:
   whether a sentence is true, plain or says anything. That stays with the reviewer.

2. REVIEW. ``docs/human-review.json`` records the sha256 of every public file as a named
   person last read it. Any public file that changed since, or was never recorded, fails.
   ``--attest`` writes the record and REFUSES to run without a terminal, so a session or a
   CI job cannot attest on a person's behalf.

Scope is the public surface: the HTML pages, ``llms.txt``, and the stylesheet and script.
Source code is not scanned for authorship, because nothing here can tell who wrote it; code
is held by tests, mutation checks and independent review instead.

Exit: 0 clean, 1 findings, 2 could not run (nothing to scan, or the record is unreadable).

    python3 scripts/check_ai_tells.py            # both gates
    python3 scripts/check_ai_tells.py --tells    # only the patterns
    python3 scripts/check_ai_tells.py --attest   # a person, in a terminal, after reading
    python3 scripts/check_ai_tells.py --selftest
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
RECORD = ROOT / "docs" / "human-review.json"
PUBLIC_SUFFIXES = (".html", ".txt", ".css", ".js")
SKIP_NAMES = {"og.html"}

BAR = chr(0x2014), chr(0x2013)

BANNED_WORDS = (
    "revolutionary", "game changing", "game-changing", "game changer", "game-changer",
    "seamless", "seamlessly", "unlock", "unleash", "leverage", "robust", "cutting edge",
    "cutting-edge", "state-of-the-art", "next-generation", "effortless", "effortlessly",
    "supercharge", "empower", "empowering", "elevate", "streamline", "harnessing", "harness the", "delve",
    "tapestry", "testament", "transformative", "holistic", "synergy", "quietly",
    "in today's fast-paced", "ever-evolving", "landscape", "navigate the complexities",
    "at the end of the day", "it's important to note", "it is important to note",
)

_EMOJI = re.compile(
    "[" + chr(0x1F300) + "-" + chr(0x1FAFF) + chr(0x2600) + "-" + chr(0x27BF)
    + chr(0x2B50) + chr(0x2705) + chr(0x274C) + chr(0xFE0F) + chr(0x1F000) + "-" + chr(0x1F2FF) + "]"
)
_NEG_PARALLEL = re.compile(
    r"\b(?:it(?:'s| is)|this is|that(?:'s| is)) not (?:just |only |merely |simply )?[^.;]{2,60}[,;] "
    r"(?:it(?:'s| is)|but|this is|that(?:'s| is))\b|\bnot (?:just|only|merely) [^.;]{2,60}\bbut\b|"
    r"\bisn't (?:just|only|merely)\b|\bmore than just\b",
    re.I,
)
_PSEUDO_CLEFT = re.compile(r"^(?:what|all|the (?:thing|part|point|goal|real \w+)) [^.?!]{3,70}\b(?:is|are|was)\b(?: to)? ", re.I)
_CLOSER = re.compile(r"\b(?:ready to|want to see how|curious (?:about|what)|let's (?:talk|chat|connect)|reach out today)\b", re.I)

# Visual habits of generated pages, matched in CSS text.
_CSS_TELLS = (
    (re.compile(r"background-clip\s*:\s*text", re.I), "gradient text"),
    (re.compile(r"text-shadow\s*:", re.I), "text shadow"),
    (re.compile(r"backdrop-filter\s*:", re.I), "frosted blur"),
    (re.compile(r"box-shadow\s*:[^;]*\b(?:[3-9]\d|[1-9]\d{2})px[^;]*rgba?\(", re.I), "large glow shadow"),
    (re.compile(r"border-radius\s*:\s*(?:9{3,}|[5-9]\d{1,2}|\d{3,})px", re.I), "pill or very round corners"),
    (re.compile(r"(?:linear|radial|conic)-gradient\(", re.I), "gradient"),
    (re.compile(r"@keyframes\s+\w*(?:pulse|glow|float|shimmer|gradient)", re.I), "decorative animation"),
    (re.compile(r"animation\s*:[^;]*\b(?:pulse|glow|float|shimmer)", re.I), "decorative animation"),
)


class Finding:
    def __init__(self, path: Path, line: int, kind: str, detail: str) -> None:
        self.path, self.line, self.kind, self.detail = path, line, kind, detail

    def __str__(self) -> str:
        try:
            rel = self.path.relative_to(ROOT)
        except ValueError:
            rel = self.path
        return f"{rel}:{self.line}: {self.kind}: {self.detail}"


class _Text(HTMLParser):
    """Visible text with line numbers: text nodes, title, alt and aria-label, meta content, and
    the strings inside JSON-LD, which answer engines read as plain prose."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[int, str]] = []
        self.css: list[tuple[int, str]] = []
        self._skip = False
        self._mode = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        line = self.getpos()[0]
        if tag in ("script", "style"):
            self._skip = True
            self._mode = "ld" if (tag == "script" and a.get("type") == "application/ld+json") else tag
        for key in ("alt", "aria-label", "title", "placeholder", "content"):
            v = a.get(key)
            if v and (tag != "meta" or a.get("name") in ("description", "og:description") or a.get("property") in ("og:title", "og:description", "og:image:alt")):
                self.items.append((line, v))
        if "style" in a and a["style"]:
            self.css.append((line, a["style"]))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
            self._mode = ""

    def handle_data(self, data):
        line = self.getpos()[0]
        if self._skip:
            if self._mode == "ld":
                self.items.extend((line, s) for s in _json_strings(data))
            elif self._mode == "style":
                self.css.append((line, data))
            return
        if data.strip():
            self.items.append((line, data))


def _json_strings(text: str) -> list[str]:
    try:
        obj = json.loads(text)
    except ValueError:
        return []
    out: list[str] = []

    def walk(o: object) -> None:
        if isinstance(o, str):
            out.append(o)
        elif isinstance(o, dict):
            for k, v in o.items():
                if k not in ("@context", "@type", "@id", "url", "image", "sameAs"):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return out


def scan_text(path: Path, line: int, text: str) -> list[Finding]:
    out: list[Finding] = []
    raw = html.unescape(text)
    for d in BAR:
        if d in raw:
            out.append(Finding(path, line, "dash", f"U+{ord(d):04X} in public text"))
    if _EMOJI.search(raw):
        out.append(Finding(path, line, "emoji", "emoji in public text"))
    low = " " + re.sub(r"\s+", " ", raw.lower()) + " "
    for w in BANNED_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
            out.append(Finding(path, line, "vocabulary", f"'{w}'"))
    flat = re.sub(r"\s+", " ", raw).strip()
    for sentence in re.split(r"(?<=[.!?])\s+", flat):
        if _NEG_PARALLEL.search(sentence):
            out.append(Finding(path, line, "negative parallelism", sentence[:70]))
        if _PSEUDO_CLEFT.search(sentence) and len(sentence) > 25:
            out.append(Finding(path, line, "pseudo-cleft", sentence[:70]))
        if _CLOSER.search(sentence):
            out.append(Finding(path, line, "sales closer", sentence[:70]))
    return out


_HEX = r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b"
_TEXT_COLOR = re.compile(r"(?<![-a-z])color\s*:\s*(?:(" + _HEX + r")|var\(--[a-z0-9-]+\s*,\s*(" + _HEX + r")\))", re.I)


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_on_white(hex_color: str) -> float:
    return 1.05 / (_luminance(hex_color) + 0.05)


def scan_contrast(path: Path, line: int, css: str) -> list[Finding]:
    """A literal text colour that is hard to read on the white pages. Rules that set their own
    background are skipped, since the colour may sit on it. This reads only literal hex values
    and var() fallbacks; it cannot see a colour a variable resolves to."""
    out: list[Finding] = []
    bodies = [m.group(2) for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css)] or [css]
    for body in bodies:
        if re.search(r"background", body, re.I):
            continue
        for m in _TEXT_COLOR.finditer(body):
            value = m.group(1) or m.group(2)
            ratio = contrast_on_white(value)
            if ratio < 4.5:
                out.append(Finding(path, line, "low contrast", f"text {value} on white is {ratio:.1f}:1, under 4.5:1"))
    return out


def scan_css(path: Path, line: int, css: str) -> list[Finding]:
    out: list[Finding] = scan_contrast(path, line, css)
    for rx, name in _CSS_TELLS:
        if rx.search(css):
            out.append(Finding(path, line, "visual tell", name))
    return out


def public_files(root: Path = STATIC) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in PUBLIC_SUFFIXES and p.name not in SKIP_NAMES
    )


def scan_file(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".html":
        p = _Text()
        p.feed(text)
        out = [f for line, t in p.items for f in scan_text(path, line, t)]
        out += [f for line, c in p.css for f in scan_css(path, line, c)]
        return _dedupe(out)
    if suffix == ".css":
        return _dedupe(scan_css(path, 1, text))
    if suffix == ".txt":
        return _dedupe([f for i, line in enumerate(text.splitlines(), 1) for f in scan_text(path, i, line)])
    if suffix == ".js":
        return [Finding(path, 1, "dash", f"U+{ord(d):04X}") for d in BAR if d in text]
    return []


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, int, str, str]] = set()
    out = []
    for f in findings:
        k = (str(f.path), f.line, f.kind, f.detail)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_state(files: list[Path], record: Path = RECORD) -> tuple[list[str], str]:
    """Problems with the human-review record, and a one-line description of it."""
    if not record.exists():
        return [f"no review record at {record.name}: nobody has attested to these files"], "absent"
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        recorded = data["files"]
        who = str(data["reviewer"]).strip()
        when = str(data["reviewed_at"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"could not read {record}: {exc!r}") from exc
    if not who:
        return ["the review record names no reviewer"], "unnamed"
    problems = []
    seen = set()
    for p in files:
        rel = str(p.relative_to(ROOT))
        seen.add(rel)
        if rel not in recorded:
            problems.append(f"{rel}: never reviewed")
        elif recorded[rel] != sha(p):
            problems.append(f"{rel}: changed since {who} reviewed it on {when[:10]}")
    for rel in recorded:
        if rel not in seen:
            problems.append(f"{rel}: recorded but no longer a public file (re-attest to drop it)")
    return problems, f"reviewed by {who} on {when[:10]}"


def attest(files: list[Path], record: Path = RECORD, *, interactive: bool | None = None, reviewer: str = "") -> int:
    tty = sys.stdin.isatty() and sys.stdout.isatty() if interactive is None else interactive
    if not tty:
        print("refusing: --attest needs a terminal. A session or a CI job cannot read these files on a "
              "person's behalf, so it cannot attest for one.")
        return 1
    name = reviewer or subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    print(f"{len(files)} public files will be recorded as read by '{name}':")
    for p in files:
        print("  ", p.relative_to(ROOT))
    ans = input("Type READ to confirm you have read every one of them: ").strip()
    if ans != "READ" or not name:
        print("not attested")
        return 1
    record.write_text(
        json.dumps(
            {"reviewer": name, "reviewed_at": datetime.now(UTC).isoformat(timespec="seconds"),
             "files": {str(p.relative_to(ROOT)): sha(p) for p in files}},
            indent=2, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"attested: {record.relative_to(ROOT)}")
    return 0


def run(argv: list[str]) -> int:
    files = public_files()
    if not files:
        print("could not run: no public files found under app/static")
        return 2
    do_tells = "--review" not in argv
    do_review = "--tells" not in argv
    rc = 0
    if do_tells:
        findings = [f for p in files for f in scan_file(p)]
        print(f"tells: scanned {len(files)} public files, {len(findings)} finding(s)")
        for f in findings:
            print("  ", f)
        if findings:
            rc = 1
    if do_review:
        problems, desc = review_state(files)
        print(f"review: {desc}")
        for pr in problems:
            print("  ", pr)
        if problems:
            rc = 1
    if rc:
        print("REFUSED: do not deploy. Fix each finding, read the files, then run "
              "`python3 scripts/check_ai_tells.py --attest` in your own terminal.")
    return rc


def selftest() -> int:
    import tempfile

    bad = 0

    def check(name: str, cond: bool) -> None:
        nonlocal bad
        print(("ok   " if cond else "FAIL ") + name)
        bad += 0 if cond else 1

    root = Path(tempfile.mkdtemp())
    p = root / "x.html"
    d = chr(0x2014)
    planted = f"<html><body><p>This is seamless {d} and it's not just a tool, it's a platform.</p></body></html>"
    p.write_text(planted)
    kinds = {f.kind for f in scan_file(p)}
    check("catches a dash, vocabulary and negative parallelism", {"dash", "vocabulary", "negative parallelism"} <= kinds)
    p.write_text("<html><body><p>Rules decide who is chased. A person approves every message.</p></body></html>")
    check("spares plain prose", not scan_file(p))
    p.write_text("<style>h1{background:linear-gradient(red,blue);-webkit-background-clip:text}</style>")
    check("catches gradient text", any(f.kind == "visual tell" for f in scan_file(p)))
    p.write_text('<script type="application/ld+json">{"a":"We unlock value"}</script>')
    check("reads prose inside JSON-LD", any(f.kind == "vocabulary" for f in scan_file(p)))
    p.write_text("<p>The footer says 2026 &mdash; ok</p>")
    check("catches an entity that renders to a dash", any(f.kind == "dash" for f in scan_file(p)))
    p.write_text("<p>Plain.</p>")
    rec = root / "rev.json"
    files = [p]
    # review gate works in a temp root: monkeypatch ROOT for relative paths
    global ROOT
    old = ROOT
    ROOT = root
    try:
        probs, _ = review_state(files, rec)
        check("refuses with no review record", bool(probs))
        check("refuses attest without a terminal", attest(files, rec, interactive=False) == 1 and not rec.exists())
        rec.write_text(json.dumps({"reviewer": "A Person", "reviewed_at": "2026-01-01T00:00:00+00:00",
                                    "files": {"x.html": sha(p)}}))
        check("passes a matching record", not review_state(files, rec)[0])
        p.write_text("<p>Changed.</p>")
        check("refuses a file changed after review", bool(review_state(files, rec)[0]))
        rec.write_text(json.dumps({"reviewer": "", "reviewed_at": "x", "files": {"x.html": sha(p)}}))
        check("refuses a record with no reviewer", bool(review_state(files, rec)[0]))
    finally:
        ROOT = old
    check("a clean control file would not be blank", len(planted) > 0)
    print("selftest:", "FAIL" if bad else "pass")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--attest" in sys.argv:
        files = public_files()
        sys.exit(attest(files) if files else 2)
    sys.exit(run(sys.argv[1:]))
