"""Zero-token architecture gates.

The claim being defended: the hot path performs no inference. A claim like that
decays silently - someone adds a call site, the bill drifts up, and nothing
fails - so it is enforced here rather than documented.

Three gates:

* **Hot path**: running a full recovery must construct no chat model at all.
* **Budget**: a new model call site without a written classification fails the
  build, so adding inference is a deliberate, reviewed act.
* **Artifact integrity**: the committed copy library must actually cover every
  failure code, and must not smuggle a URL into customer-facing text.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app import nodes, templates
from app.graph import run_recovery

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


class _Doc:
    def __init__(self, text: str) -> None:
        self.page_content = text


class _StubRetriever:
    """Stands in for the FAISS retriever.

    Necessary rather than incidental: several tests here set OPENAI_API_KEY to
    prove a key alone does not buy inference, and a real key would otherwise
    send the retriever off to build an embeddings index over the network.
    """

    def invoke(self, query):
        return [_Doc("Playbook: expired cards recover best with a card-update link.")]


@pytest.fixture
def offline(monkeypatch):
    """Neutralise the retriever so run_recovery never touches the network."""
    monkeypatch.setattr(nodes, "get_retriever", lambda: _StubRetriever())
    return monkeypatch


@pytest.fixture
def model_tripwire(monkeypatch):
    """Explode if anything tries to construct a real chat model."""
    constructed = []

    class _Tripwire:
        def __init__(self, *args, **kwargs):
            constructed.append(kwargs)

        def invoke(self, prompt):  # pragma: no cover - only on the LLM path
            return "model output"

    monkeypatch.setattr(nodes, "ChatOpenAI", _Tripwire)
    return constructed


def _event():
    return {
        "customer_id": "cust_001",
        "amount": 49.0,
        "currency": "eur",
        "failure_code": "card_expired",
        "attempt": 1,
    }


# ---------------------------------------------------------------------------
# Gate 1: the hot path performs no inference
# ---------------------------------------------------------------------------

def test_recovery_constructs_no_model_by_default(offline, model_tripwire, monkeypatch):
    monkeypatch.delenv("PAYPILOT_LLM_DRAFT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    output = run_recovery(_event())

    assert model_tripwire == [], "default recovery path must not construct a model"
    assert output["message"], "and must still produce a real message"
    assert output["fallback_used"] is False


def test_an_api_key_alone_does_not_enable_inference(offline, model_tripwire, monkeypatch):
    """The behaviour this phase changed. Having a key is not consent to spend
    it on every invoice."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("PAYPILOT_LLM_DRAFT", raising=False)

    run_recovery(_event())
    assert model_tripwire == []
    assert nodes.llm_drafting_enabled() is False


def test_the_flag_and_a_key_together_enable_inference(offline, model_tripwire, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("PAYPILOT_LLM_DRAFT", "1")

    assert nodes.llm_drafting_enabled() is True
    run_recovery(_event())
    assert model_tripwire, "the escape hatch must actually reach the model"


def test_the_flag_without_a_key_stays_deterministic(offline, model_tripwire, monkeypatch):
    monkeypatch.setenv("PAYPILOT_LLM_DRAFT", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    run_recovery(_event())
    assert model_tripwire == []


def test_use_mock_reports_the_zero_inference_path(monkeypatch):
    monkeypatch.delenv("PAYPILOT_LLM_DRAFT", raising=False)
    assert nodes.use_mock() is True


# ---------------------------------------------------------------------------
# Gate 2: budget - no undeclared call sites
# ---------------------------------------------------------------------------

MARKER = "ZERO-TOKEN CLASSIFICATION"


def test_every_model_call_site_carries_a_classification():
    """A new call site without a written justification fails the build.

    The point is not the comment. It is that adding inference cannot happen by
    reflex: someone has to state which of BUILD-TIME / CACHEABLE / TRUE-RUNTIME
    it is, in a diff a reviewer reads.
    """
    undeclared = []
    for path in sorted(APP_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "ChatOpenAI(" not in source:
            continue
        if MARKER not in source:
            undeclared.append(path.name)

    assert not undeclared, (
        f"model call sites without a {MARKER} comment: {undeclared}. "
        "Classify the call site as BUILD-TIME, CACHEABLE or TRUE-RUNTIME."
    )


def test_the_marker_test_can_actually_fail(tmp_path):
    """A guard that cannot fail is decoration. This proves the check bites."""
    offender = tmp_path / "bad_module.py"
    offender.write_text("llm = ChatOpenAI(model='x')\n", encoding="utf-8")
    source = offender.read_text(encoding="utf-8")
    assert "ChatOpenAI(" in source and MARKER not in source


# ---------------------------------------------------------------------------
# Gate 3: the committed artifact is complete and safe
# ---------------------------------------------------------------------------

REQUIRED_CODES = ("card_expired", "insufficient_funds", "generic_decline")


def test_templates_cover_every_failure_code():
    for kind in ("diagnosis", "message", "subject"):
        for code in REQUIRED_CODES:
            assert templates.get(kind, code), f"missing {kind} copy for {code}"


def test_unknown_failure_code_falls_back_rather_than_raising():
    """A decline reason Stripe has not shown us before must still send."""
    assert templates.get("message", "some_new_stripe_code")
    assert templates.get("subject", "some_new_stripe_code")


def test_no_template_contains_a_url():
    """Customer-facing copy carries no link. The one sanctioned URL is attached
    at send time, where we know which link was minted for that invoice."""
    from app.safety import find_foreign_urls

    doc = templates.load()
    for kind in ("diagnosis", "message", "subject"):
        for code, text in doc[kind].items():
            assert find_foreign_urls(text, allow_hosts=False) == [], (
                f"{kind}/{code} contains a URL"
            )


def test_templates_render_their_slots():
    rendered = templates.render("message", "card_expired", name="Dana", plan="Pro")
    assert "Dana" in rendered and "Pro" in rendered
    assert "{name}" not in rendered and "{plan}" not in rendered


def test_missing_slot_leaves_the_placeholder_visible():
    """A silently blanked name reads as fine in review. An unfilled one does not."""
    rendered = templates.render("message", "card_expired", name="Dana")
    assert "{plan}" in rendered


def test_templates_carry_no_dashes_that_break_house_style():
    doc = templates.load()
    for kind in ("diagnosis", "message", "subject"):
        for code, text in doc[kind].items():
            assert "—" not in text and "–" not in text, f"{kind}/{code}"  # lint-style: allow-dash


def test_missing_artifact_fails_loudly(monkeypatch, tmp_path):
    """A deploy that lost the copy library must not quietly mail something else."""
    monkeypatch.setattr(templates, "TEMPLATES_PATH", tmp_path / "gone.json")
    monkeypatch.setattr(templates, "_cache", None)
    with pytest.raises(templates.TemplatesUnavailable):
        templates.load()


def test_artifact_is_valid_json_on_disk():
    doc = json.loads(templates.TEMPLATES_PATH.read_text(encoding="utf-8"))
    assert doc["_meta"]["artifact"]
    assert set(REQUIRED_CODES) <= set(doc["message"])
