"""Unit tests for ark.ethical_review.

review_idea() is provider-agnostic: it runs on the run's SELECTED model via
``ark.llm_lite.complete`` (not a hardcoded Anthropic endpoint). These tests
monkeypatch ``complete`` to return canned verdict text — no real API calls.
"""

from __future__ import annotations

from unittest.mock import patch

from ark import ethical_review

_MODEL = "anthropic/claude-sonnet-4-6"


def _patch_complete(text):
    return patch("ark.llm_lite.complete", return_value=text)


class TestReviewIdea:
    def test_empty_idea_allowed_without_api_call(self):
        result = ethical_review.review_idea("", model=_MODEL)
        assert result["decision"] == "allow"
        assert result["category"] == "none"
        assert result["reviewed"] is False

    def test_no_model_is_skip_not_pass(self):
        result = ethical_review.review_idea("Some normal idea", model="")
        assert result["decision"] == "allow"
        assert result["reviewed"] is False
        assert "no model" in result["reason"].lower()

    def test_allow_decision_parsed(self):
        with _patch_complete('{"decision": "allow", "category": "none", "reason": "ordinary ML research"}'):
            result = ethical_review.review_idea(
                "Improve transformer attention efficiency.", model=_MODEL
            )
        assert result["decision"] == "allow"
        assert result["category"] == "none"
        assert result["reviewed"] is True
        assert "transformer" not in result["reason"]  # reason from model, not idea

    def test_block_decision_parsed(self):
        with _patch_complete(
            '{"decision": "block", "category": "weaponization", '
            '"reason": "operational design of a chemical weapon"}'
        ):
            result = ethical_review.review_idea(
                "Design a nerve agent for mass casualties.", model=_MODEL
            )
        assert result["decision"] == "block"
        assert result["category"] == "weaponization"
        assert result["reviewed"] is True
        assert "chemical weapon" in result["reason"]

    def test_response_with_code_fences_still_parses(self):
        with _patch_complete('```json\n{"decision": "allow", "category": "none", "reason": "ok"}\n```'):
            result = ethical_review.review_idea("Routine paper.", model=_MODEL)
        assert result["decision"] == "allow"
        assert result["reviewed"] is True

    def test_unparseable_response_fails_open(self):
        with _patch_complete("I cannot comply with that request, sorry."):
            result = ethical_review.review_idea("Routine paper.", model=_MODEL)
        assert result["decision"] == "allow"
        assert result["reviewed"] is False
        assert "could not parse" in result["reason"].lower() or "parse error" in result["reason"].lower()

    def test_empty_response_fails_open(self):
        # complete() returns "" on any failure (e.g. provider error) — fail-open,
        # but reviewed=False so the caller does not report it as a real pass.
        with _patch_complete(""):
            result = ethical_review.review_idea("Routine paper.", model=_MODEL)
        assert result["decision"] == "allow"
        assert result["reviewed"] is False
        assert "empty" in result["reason"].lower()

    def test_invalid_decision_falls_back_to_allow(self):
        with _patch_complete('{"decision": "maybe", "category": "none", "reason": "unsure"}'):
            result = ethical_review.review_idea("Routine paper.", model=_MODEL)
        assert result["decision"] == "allow"
        assert result["reviewed"] is True
