"""The gate that keeps generated-looking public prose and unread files from being deployed."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_ai_tells.py"
_spec = importlib.util.spec_from_file_location("check_ai_tells", SCRIPT)
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_ai_tells"] = gate
_spec.loader.exec_module(gate)

DASH = chr(0x2014)


def _kinds(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return {f.kind for f in gate.scan_file(p)}


def test_the_shipped_public_files_carry_no_tell():
    found = [str(f) for p in gate.public_files() for f in gate.scan_file(p)]
    assert found == []


def test_the_gate_scans_the_public_surface_and_would_notice_it_vanish():
    names = {p.name for p in gate.public_files()}
    assert {"index.html", "pricing.html", "llms.txt", "paypilot.css", "paypilot.js"} <= names


@pytest.mark.parametrize(
    "name,text,kind",
    [
        ("a.html", f"<p>Fast {DASH} and simple.</p>", "dash"),
        ("a.html", "<p>Fast &mdash; and simple.</p>", "dash"),
        ("a.html", "<p>A seamless way to recover payments.</p>", "vocabulary"),
        ("a.html", "<p>We unleash revenue.</p>", "vocabulary"),
        ("a.html", "<p>It's not just a tool, it's a platform.</p>", "negative parallelism"),
        ("a.html", "<p>This is not only fast but also correct.</p>", "negative parallelism"),
        ("a.html", "<h2>What the product really is for is recovering money</h2>", "pseudo-cleft"),
        ("a.html", "<p>Ready to see how it works?</p>", "sales closer"),
        ("a.html", "<p>Done " + chr(0x2705) + "</p>", "emoji"),
        ("a.html", '<img alt="A seamless dashboard" src="x">', "vocabulary"),
        ("a.html", '<meta name="description" content="Unlock growth">', "vocabulary"),
        ("a.html", '<script type="application/ld+json">{"text":"A robust agent"}</script>', "vocabulary"),
        ("a.html", "<style>h1{background:linear-gradient(red,blue)}</style>", "visual tell"),
        ("a.html", "<style>h1{-webkit-background-clip:text}</style>", "visual tell"),
        ("a.html", "<style>.p{border-radius:999px}</style>", "visual tell"),
        ("a.html", "<style>.c{box-shadow:0 20px 60px rgba(0,0,0,.4)}</style>", "visual tell"),
        ("a.html", '<div style="backdrop-filter:blur(8px)">x</div>', "visual tell"),
        ("x.css", ".a{text-shadow:0 0 8px red}", "visual tell"),
        ("x.txt", "Leverage our platform.", "vocabulary"),
        ("x.js", f"var a = 1; // note {DASH}", "dash"),
    ],
)
def test_each_tell_is_caught_where_it_can_hide(tmp_path, name, text, kind):
    assert kind in _kinds(tmp_path, name, text)


@pytest.mark.parametrize(
    "text",
    [
        "<p>Written rules decide who is chased. A person approves every message.</p>",
        "<p>A test harness drives the load. The harness is a script.</p>",
        "<p>What PayPilot touches</p>",
        "<style>.card{border:1px solid #d3d9df;border-radius:2px}</style>",
        "<script>var landscape = 1;</script>",
    ],
)
def test_plain_prose_and_plain_styles_are_spared(tmp_path, text):
    assert _kinds(tmp_path, "ok.html", text) == set()


def _review(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    f = tmp_path / "page.html"
    f.write_text("<p>Plain.</p>", encoding="utf-8")
    return f, tmp_path / "review.json"


def test_no_record_means_nobody_has_read_the_files(tmp_path, monkeypatch):
    f, rec = _review(tmp_path, monkeypatch)
    problems, desc = gate.review_state([f], rec)
    assert problems and desc == "absent"


def test_a_matching_record_passes_and_an_edit_after_review_fails(tmp_path, monkeypatch):
    f, rec = _review(tmp_path, monkeypatch)
    rec.write_text(json.dumps({"reviewer": "A Person", "reviewed_at": "2026-11-23T00:00:00+00:00",
                               "files": {"page.html": gate.sha(f)}}))
    assert gate.review_state([f], rec)[0] == []
    f.write_text("<p>Edited after.</p>", encoding="utf-8")
    assert any("changed since A Person" in p for p in gate.review_state([f], rec)[0])


def test_a_new_file_and_a_removed_file_both_fail(tmp_path, monkeypatch):
    f, rec = _review(tmp_path, monkeypatch)
    rec.write_text(json.dumps({"reviewer": "A Person", "reviewed_at": "2026-11-23T00:00:00+00:00",
                               "files": {"page.html": gate.sha(f), "gone.html": "0" * 64}}))
    new = tmp_path / "new.html"
    new.write_text("<p>New.</p>", encoding="utf-8")
    problems = gate.review_state([f, new], rec)[0]
    assert any("new.html: never reviewed" in p for p in problems)
    assert any("gone.html" in p and "no longer a public file" in p for p in problems)


def test_a_record_with_no_reviewer_name_fails(tmp_path, monkeypatch):
    f, rec = _review(tmp_path, monkeypatch)
    rec.write_text(json.dumps({"reviewer": "  ", "reviewed_at": "x", "files": {"page.html": gate.sha(f)}}))
    assert gate.review_state([f], rec)[0]


def test_an_unreadable_record_is_could_not_run_not_a_pass(tmp_path, monkeypatch):
    f, rec = _review(tmp_path, monkeypatch)
    rec.write_text("{not json")
    with pytest.raises(SystemExit):
        gate.review_state([f], rec)


def test_attest_refuses_without_a_terminal_and_writes_nothing(tmp_path, monkeypatch):
    f, rec = _review(tmp_path, monkeypatch)
    assert gate.attest([f], rec, interactive=False) == 1
    assert not rec.exists()
    out = subprocess.run([sys.executable, str(SCRIPT), "--attest"], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 1 and "needs a terminal" in out.stdout


def test_attest_wrong_word_writes_nothing(tmp_path, monkeypatch):
    f, rec = _review(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _="": "yes")
    assert gate.attest([f], rec, interactive=True, reviewer="A Person") == 1
    assert not rec.exists()
    monkeypatch.setattr("builtins.input", lambda _="": "READ")
    assert gate.attest([f], rec, interactive=True, reviewer="A Person") == 0
    assert gate.review_state([f], rec)[0] == []


def test_the_script_selftest_passes_and_the_script_has_no_dash_literals():
    out = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout
    assert DASH not in SCRIPT.read_text(encoding="utf-8") and chr(0x2013) not in SCRIPT.read_text(encoding="utf-8")


def test_deploy_is_blocked_by_both_gates_in_ci_and_in_make():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "check_ai_tells.py --tells" in ci
    assert ci.index("check_ai_tells.py --tells") < ci.index("Run tests")
    assert "check_ai_tells.py --review" in ci
    assert ci.index("check_ai_tells.py --review") < ci.index("flyctl deploy")
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\ndeploy: ai-tells" in make


def test_finding_no_public_files_is_could_not_run_never_a_pass(monkeypatch, capsys):
    monkeypatch.setattr(gate, "public_files", lambda root=gate.STATIC: [])
    assert gate.run([]) == 2
    assert "could not run" in capsys.readouterr().out


@pytest.mark.parametrize(
    "css",
    [
        "p, li { color:var(--cdb, #c3cee0); }",
        ".a { color:#9fb0cc; }",
        ".a { color:#ccc }",
        "<x>",
    ],
)
def test_pale_text_on_the_white_pages_is_caught_including_a_var_fallback(tmp_path, css):
    if css == "<x>":
        css = '<p style="color:#bbb">text</p>'
        found = _kinds(tmp_path, "a.html", css)
    else:
        found = _kinds(tmp_path, "a.html", f"<style>{css}</style>")
    assert "low contrast" in found


@pytest.mark.parametrize(
    "css",
    [
        "p { color:var(--ink); }",
        ".a { color:#1a2530; }",
        ".a { color:#4f5b67; }",
        ".btn { background:#1f3a5f; color:#fff; }",
        "pre { background:#0a1226; color:#cfe0ff; }",
    ],
)
def test_readable_text_and_text_on_its_own_background_are_spared(tmp_path, css):
    assert "low contrast" not in _kinds(tmp_path, "a.html", f"<style>{css}</style>")


def test_contrast_maths_matches_known_ratios():
    assert round(gate.contrast_on_white("#000000"), 1) == 21.0
    assert round(gate.contrast_on_white("#ffffff"), 1) == 1.0
    assert 4.4 < gate.contrast_on_white("#767676") < 4.6
