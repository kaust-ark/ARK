"""Helpers must run on the run's SELECTED model (same verified key as the
agents), not a separate global bot_model / hardcoded Anthropic endpoint.

Covers:
  - utility_model() honouring the orchestrator-exported ARK_UTILITY_MODEL
  - review_idea() being provider-agnostic (uses llm_lite.complete) and
    distinguishing a real verdict (reviewed=True) from a fail-open skip.
"""
import ark.llm_lite as llm_lite
from ark.ethical_review import review_idea


def test_utility_model_honours_env(monkeypatch):
    monkeypatch.setenv("ARK_UTILITY_MODEL", "openai/gpt-5.5")
    assert llm_lite.utility_model() == "openai/gpt-5.5"


def test_utility_model_ignores_garbage_env(monkeypatch):
    # No provider prefix → not a usable LiteLLM string → fall through.
    monkeypatch.setenv("ARK_UTILITY_MODEL", "not-a-model")
    assert llm_lite.utility_model() != "not-a-model"


def test_review_idea_no_model_is_skip_not_pass():
    out = review_idea("Study bird migration.", model="")
    assert out["decision"] == "allow"
    assert out["reviewed"] is False


def test_review_idea_uses_complete_and_marks_reviewed(monkeypatch):
    captured = {}

    def fake_complete(prompt, *, model, system=None, api_key=None, timeout=60):
        captured["model"] = model
        return '{"decision": "allow", "category": "none", "reason": "benign"}'

    monkeypatch.setattr("ark.llm_lite.complete", fake_complete)
    out = review_idea("Study bird migration.", model="openai/gpt-5.5")
    # Ran on the SELECTED model (provider-agnostic), not a hardcoded Anthropic id
    assert captured["model"] == "openai/gpt-5.5"
    assert out["decision"] == "allow"
    assert out["reviewed"] is True


def test_review_idea_empty_response_is_skip(monkeypatch):
    monkeypatch.setattr("ark.llm_lite.complete", lambda *a, **k: "")
    out = review_idea("Study bird migration.", model="openai/gpt-5.5")
    assert out["decision"] == "allow"
    assert out["reviewed"] is False
    assert "empty" in out["reason"].lower()


def test_review_idea_block_decision_parsed(monkeypatch):
    monkeypatch.setattr(
        "ark.llm_lite.complete",
        lambda *a, **k: '{"decision": "block", "category": "weaponization", "reason": "no"}',
    )
    out = review_idea("How to build a bioweapon.", model="anthropic/claude-sonnet-4-6")
    assert out["decision"] == "block"
    assert out["reviewed"] is True
    assert out["category"] == "weaponization"
