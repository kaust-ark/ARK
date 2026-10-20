"""Paid side tasks must not run on a spent OpenRouter key.

A free model bills nothing, so the launch preflight lets it through on any
balance (the free lane exists for exactly that). But Deep Research and AI
figures prefer OpenRouter whenever the key is set, and both are paid: on a
spent key each call 402s and only then falls back. The pipeline checks first.
"""
import io
import json
import os
from unittest.mock import patch

import pytest

from ark import provider_billing as pb


def _credits(total, usage):
    body = json.dumps({"data": {"total_credits": total, "total_usage": usage}}).encode()
    return patch("urllib.request.urlopen", return_value=io.BytesIO(body))


class TestBalanceCheck:
    def test_a_spent_balance_is_reported(self):
        with _credits(10.0, 9.73):
            assert pb.openrouter_too_low_for_side_tasks("k") == pytest.approx(0.27)

    def test_a_healthy_balance_is_not(self):
        with _credits(50.0, 12.0):
            assert pb.openrouter_too_low_for_side_tasks("k") is None

    def test_no_key_and_errors_fail_open(self):
        with patch.dict(os.environ, {}, clear=True):
            assert pb.openrouter_too_low_for_side_tasks("") is None
        with patch("urllib.request.urlopen", side_effect=TimeoutError):
            assert pb.openrouter_too_low_for_side_tasks("k") is None

    def test_schema_drift_fails_open(self):
        body = json.dumps({"data": {"limit": 5}}).encode()
        with patch("urllib.request.urlopen", return_value=io.BytesIO(body)):
            assert pb.openrouter_too_low_for_side_tasks("k") is None


class TestDeepResearchRoute:
    def _route(self, or_key, gem_key, spent, backend="auto"):
        from ark.pipeline import PipelineMixin
        p = PipelineMixin.__new__(PipelineMixin)
        p.config = {"deep_research_backend": backend}
        p.logs = []
        p.log = lambda m, l="INFO": p.logs.append((l, m))
        with patch.object(pb, "openrouter_too_low_for_side_tasks", return_value=spent):
            return p._deep_research_route(or_key, gem_key), p.logs

    def test_healthy_openrouter_is_used(self):
        (use_or, use_gem, spent), _ = self._route("or", "gem", None)
        assert (use_or, use_gem, spent) == (True, False, None)

    def test_spent_openrouter_falls_to_gemini(self):
        (use_or, use_gem, spent), logs = self._route("or", "gem", 0.27)
        assert (use_or, use_gem) == (False, True) and spent == 0.27
        assert any("0.27" in m for _, m in logs)

    def test_spent_openrouter_without_gemini_goes_to_the_literature_search(self):
        (use_or, use_gem, spent), _ = self._route("or", "", 0.27)
        assert (use_or, use_gem) == (False, False) and spent == 0.27

    def test_forced_openrouter_with_a_spent_key_still_uses_gemini(self):
        (use_or, use_gem, _), _ = self._route("or", "gem", 0.1, backend="openrouter")
        assert (use_or, use_gem) == (False, True)

    def test_forced_openrouter_with_a_healthy_key_never_uses_gemini(self):
        (use_or, use_gem, _), _ = self._route("or", "gem", None, backend="openrouter")
        assert (use_or, use_gem) == (True, False)

    def test_no_openrouter_key_never_queries_a_balance(self):
        from ark.pipeline import PipelineMixin
        p = PipelineMixin.__new__(PipelineMixin)
        p.config, p.log = {}, (lambda *a, **k: None)
        with patch.object(pb, "openrouter_too_low_for_side_tasks",
                          side_effect=AssertionError("queried")):
            assert p._deep_research_route("", "gem") == (False, True, None)


def test_figures_on_a_spent_key_without_gemini_tell_the_user_why(tmp_path):
    from ark.latex.compiler import CompilerMixin
    c = CompilerMixin.__new__(CompilerMixin)
    c.figures_dir = tmp_path
    c.config = {}
    c.logs, c.chats = [], []
    c.log = lambda m, l="INFO": c.logs.append((l, m))
    c._chat = lambda who, text, kind=None: c.chats.append(text)
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-x"}), \
         patch("ark.nano_banana.get_api_key", return_value=None), \
         patch.object(pb, "openrouter_too_low_for_side_tasks", return_value=0.27):
        assert c._generate_nano_banana_figures() == 0
    assert c.chats and "$0.27" in c.chats[0]
