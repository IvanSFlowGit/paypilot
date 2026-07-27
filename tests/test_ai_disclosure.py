"""EU AI Act Article 50 transparency: the AI-assistance disclosure footer.

The dunning copy is model-drafted (or a human-reviewed template of the same
class), so a recipient should be able to tell it was AI-assisted. The disclosure
is configurable per client and per jurisdiction and must never smuggle a link or
a secret past the output guard, so it is tested both for behaviour and for
passing the same guard the real send path runs.
"""

from __future__ import annotations

from app.loop import DEFAULT_AI_DISCLOSURE, ai_disclosure, compose_email_body
from app.safety import message_violations

LINK = "https://billing.stripe.com/p/session/test_abc123"


def test_disclosure_off_by_default(monkeypatch):
    monkeypatch.delenv("PAYPILOT_AI_DISCLOSURE", raising=False)
    assert ai_disclosure() == ""
    body = compose_email_body("Hi, your payment failed.", LINK)
    assert DEFAULT_AI_DISCLOSURE not in body


def test_disclosure_default_text_when_enabled(monkeypatch):
    monkeypatch.setenv("PAYPILOT_AI_DISCLOSURE", "1")
    assert ai_disclosure() == DEFAULT_AI_DISCLOSURE
    body = compose_email_body("Hi.", LINK)
    assert body.rstrip().endswith(DEFAULT_AI_DISCLOSURE)


def test_disclosure_custom_text_per_jurisdiction(monkeypatch):
    custom = "AI-assisted, reviewed by a person before sending."
    monkeypatch.setenv("PAYPILOT_AI_DISCLOSURE", custom)
    assert ai_disclosure() == custom
    assert custom in compose_email_body("Hi.", LINK)


def test_disclosure_off_values_stay_off(monkeypatch):
    for value in ("0", "false", "off", "no", "  ", ""):
        monkeypatch.setenv("PAYPILOT_AI_DISCLOSURE", value)
        assert ai_disclosure() == "", value


def test_disclosure_passes_the_output_guard(monkeypatch):
    monkeypatch.setenv("PAYPILOT_AI_DISCLOSURE", "1")
    body = compose_email_body("Your card was declined.", LINK)
    # Adds no link and no secret: the body still carries only the minted link.
    assert message_violations(body, (LINK,), allow_hosts=False) == []


def test_disclosure_is_the_last_line_after_the_link(monkeypatch):
    monkeypatch.setenv("PAYPILOT_AI_DISCLOSURE", "1")
    body = compose_email_body("Hi.", LINK)
    assert body.index(LINK) < body.index(DEFAULT_AI_DISCLOSURE)
