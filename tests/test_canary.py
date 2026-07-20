"""Tests for the copy-detection canary and, above all, its alert email.

The scan half is uninteresting to test: it shells out to ``gh``. The alert half
is where the risk is, and it is the reason this file exists. A GitHub code
search result is written by whoever published the matching repo: the repo name,
the file path and the html_url are all attacker-chosen strings. Pasting them
into an email put an attacker-controlled link, and anything else they cared to
shape, into the owner's inbox - triggered by the attacker, because publishing a
public repo that quotes the canary is all it takes to make the alert fire.

So the tests here assert two things together, and neither on its own is enough:
the untrusted strings do not reach the outbound body, AND the operator is still
told a detection happened. A guard that silences the alert has not fixed the
bug, it has removed the feature.

No network: the Resend call is monkeypatched at ``httpx.post``, the same seam
``tests/test_delivery.py`` uses.
"""

from __future__ import annotations

import pytest

from app.safety import message_violations
from scripts import canary

# A search result as an attacker would publish it: a link they control, a
# secret-shaped blob in the path, and a terminal escape in the repo name.
HOSTILE_URL = "https://evil.test/claim-your-refund"
HOSTILE_REPO = "evilcorp/\x1b[2Kstolen-paypilot"
HOSTILE_PATH = "docs/ghp_AAAAAAAAAAAAAAAAAAAAAAAA.md"


def _hostile_report() -> dict:
    return {
        "canary": canary.CANARY,
        "own_repo": canary.OWN_REPO,
        "github_findings": {
            canary.CANARY: [
                {"repo": HOSTILE_REPO, "path": HOSTILE_PATH, "url": HOSTILE_URL},
            ],
            canary.PHRASES[0]: [
                {"repo": "someone/else", "path": "a.py", "url": "https://other.test/x"},
            ],
        },
        "web_queries": [],
    }


@pytest.fixture
def alert_env(monkeypatch):
    """Configured to alert: a key, an address, and nothing else."""
    monkeypatch.delenv("CANARY_RESEND_API_KEY", raising=False)
    monkeypatch.delenv("CANARY_FROM_EMAIL", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("CANARY_ALERT_EMAIL", "owner@mine.test")
    monkeypatch.setattr("app.mailer._BACKOFF_SECONDS", 0.0)


@pytest.fixture
def captured_posts(monkeypatch):
    """Capture every Resend call instead of making one."""
    posts: list[dict] = []

    class _Resp:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"id": "rs_canary"}

    def _post(url, *a, **kwargs):
        posts.append(kwargs.get("json") or {})
        return _Resp()

    monkeypatch.setattr("httpx.post", _post)
    return posts


# ---------------------------------------------------------------------------
# The defect: untrusted search results in an outbound email body
# ---------------------------------------------------------------------------

def test_attacker_controlled_hit_never_reaches_the_alert_body(alert_env, captured_posts):
    """The regression that matters. A repo the attacker published must not be
    able to put its own link in the owner's inbox."""
    assert canary.email_alert(_hostile_report()) is True

    assert len(captured_posts) == 1, "one alert, one send"
    body = captured_posts[0]["text"]

    # The repo's own guard, on the body we are about to hand a mail provider.
    assert message_violations(body) == [], body
    assert HOSTILE_URL not in body
    assert HOSTILE_PATH not in body
    assert "ghp_" not in body
    assert "\x1b" not in body, "no terminal escapes in an operator alert"


def test_the_operator_is_still_told_a_detection_happened(alert_env, captured_posts):
    """Positive validation: refusing to paste the hit must not mean silence."""
    assert canary.email_alert(_hostile_report()) is True

    payload = captured_posts[0]
    body = payload["text"]
    assert payload["to"] == ["owner@mine.test"]
    assert "copy" in payload["subject"].lower()
    # The canary is ours, so naming it is safe, and it is what tells the owner
    # this is a copy rather than a lead.
    assert canary.CANARY in body
    assert "2" in body, "the number of matched fingerprints is reported"
    assert "github" in body.lower(), "and where to go look at them by hand"


def test_a_poisoned_body_is_refused_rather_than_sent(alert_env, captured_posts, monkeypatch):
    """Belt and braces: if composition ever regressed and produced a dirty body,
    the alert degrades to the minimal notice - it does not ship the body and it
    does not go quiet."""
    monkeypatch.setattr(
        canary, "alert_body", lambda report: f"pay at {HOSTILE_URL} now"
    )
    assert canary.email_alert(_hostile_report()) is True

    body = captured_posts[0]["text"]
    assert HOSTILE_URL not in body
    assert message_violations(body) == []
    assert canary.CANARY in body


def test_the_sender_refuses_a_body_that_fails_the_guard(alert_env, captured_posts):
    """The single sender is the enforcement point, so nothing routed through it
    can ship a foreign link even if a future caller composes badly."""
    from app import mailer

    result = mailer.send_operator_alert(
        to="owner@mine.test", subject="s", body=f"click {HOSTILE_URL}"
    )
    assert result["status"] == mailer.STATUS_SUPPRESSED
    assert result["error"] == "failed_output_guard"
    assert captured_posts == [], "no send may happen after a guard failure"


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def test_alert_body_is_built_only_from_our_own_constants():
    body = canary.alert_body(_hostile_report())
    assert message_violations(body) == []
    for untrusted in (HOSTILE_REPO, HOSTILE_PATH, HOSTILE_URL, "someone/else"):
        assert untrusted not in body


def test_an_unrecognised_fingerprint_is_counted_not_quoted():
    """The findings keys come from our own list. Anything else is data from
    somewhere unexpected, so it is counted, never printed."""
    report = _hostile_report()
    report["github_findings"]["rm -rf / ; https://evil.test/x"] = [
        {"repo": "a/b", "path": "c", "url": "https://evil.test/x"}
    ]
    body = canary.alert_body(report)
    assert "evil.test" not in body
    assert message_violations(body) == []


def test_a_phrase_only_hit_says_lead_not_copy():
    report = _hostile_report()
    del report["github_findings"][canary.CANARY]
    body = canary.alert_body(report)
    assert "lead" in body.lower()
    assert message_violations(body) == []


# ---------------------------------------------------------------------------
# Refusals: unconfigured is a no-op, not a crash and not a send
# ---------------------------------------------------------------------------

def test_no_findings_means_no_email(alert_env, captured_posts):
    empty = {"canary": canary.CANARY, "own_repo": canary.OWN_REPO,
             "github_findings": {}, "web_queries": []}
    assert canary.email_alert(empty) is False
    assert captured_posts == []


def test_no_address_configured_is_a_silent_no_op(monkeypatch, captured_posts):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("CANARY_ALERT_EMAIL", raising=False)
    assert canary.email_alert(_hostile_report()) is False
    assert captured_posts == []


def test_no_key_configured_is_a_silent_no_op(monkeypatch, captured_posts):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("CANARY_RESEND_API_KEY", raising=False)
    monkeypatch.setenv("CANARY_ALERT_EMAIL", "owner@mine.test")
    assert canary.email_alert(_hostile_report()) is False
    assert captured_posts == []


# ---------------------------------------------------------------------------
# The printed report is untrusted data too
# ---------------------------------------------------------------------------

def test_printed_hits_cannot_carry_terminal_escapes(monkeypatch, capsys):
    """stdout is a terminal. An escape sequence in a repo name can erase the
    lines above it, which is how a report gets rewritten by what it reports on."""
    monkeypatch.setattr(canary, "scan", _hostile_report)
    monkeypatch.setattr(canary, "email_alert", lambda report: False)
    canary.main([])
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "stolen-paypilot" in out, "the operator still sees which repo it was"
