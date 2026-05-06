"""Unit tests for ark.ethical_review.

These tests do NOT hit the real Anthropic API. They monkeypatch
``urllib.request.urlopen`` to return canned bodies.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from ark import ethical_review


def _mock_response(body: dict):
    """Build an object that quacks like a urlopen() context manager."""
    raw = json.dumps(body).encode("utf-8")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return raw

    return _Resp()


def _wrap_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


class TestReviewIdea:
    def test_empty_idea_allowed_without_api_call(self):
        result = ethical_review.review_idea("", api_key="sk-fake")
        assert result["decision"] == "allow"
        assert result["category"] == "none"

    def test_missing_api_key_fails_open(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = ethical_review.review_idea("Some normal idea", api_key="")
        assert result["decision"] == "allow"
        assert "no ANTHROPIC_API_KEY" in result["reason"].lower() or "no anthropic_api_key" in result["reason"].lower()

    def test_allow_decision_parsed(self):
        body = _wrap_text('{"decision": "allow", "category": "none", "reason": "ordinary ML research"}')
        with patch("urllib.request.urlopen", return_value=_mock_response(body)):
            result = ethical_review.review_idea(
                "Improve transformer attention efficiency.",
                api_key="sk-fake",
            )
        assert result["decision"] == "allow"
        assert result["category"] == "none"
        assert "transformer" not in result["reason"]  # reason from model, not idea

    def test_block_decision_parsed(self):
        body = _wrap_text(
            '{"decision": "block", "category": "weaponization", '
            '"reason": "operational design of a chemical weapon"}'
        )
        with patch("urllib.request.urlopen", return_value=_mock_response(body)):
            result = ethical_review.review_idea(
                "Design a nerve agent for mass casualties.",
                api_key="sk-fake",
            )
        assert result["decision"] == "block"
        assert result["category"] == "weaponization"
        assert "chemical weapon" in result["reason"]

    def test_response_with_code_fences_still_parses(self):
        body = _wrap_text(
            '```json\n{"decision": "allow", "category": "none", "reason": "ok"}\n```'
        )
        with patch("urllib.request.urlopen", return_value=_mock_response(body)):
            result = ethical_review.review_idea("Routine paper.", api_key="sk-fake")
        assert result["decision"] == "allow"

    def test_unparseable_response_fails_open(self):
        body = _wrap_text("I cannot comply with that request, sorry.")
        with patch("urllib.request.urlopen", return_value=_mock_response(body)):
            result = ethical_review.review_idea("Routine paper.", api_key="sk-fake")
        assert result["decision"] == "allow"
        assert "could not parse" in result["reason"].lower() or "parse error" in result["reason"].lower()

    def test_network_error_fails_open(self):
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            result = ethical_review.review_idea("Routine paper.", api_key="sk-fake")
        assert result["decision"] == "allow"
        assert "review error" in result["reason"].lower()

    def test_invalid_decision_falls_back_to_allow(self):
        body = _wrap_text(
            '{"decision": "maybe", "category": "none", "reason": "unsure"}'
        )
        with patch("urllib.request.urlopen", return_value=_mock_response(body)):
            result = ethical_review.review_idea("Routine paper.", api_key="sk-fake")
        assert result["decision"] == "allow"
