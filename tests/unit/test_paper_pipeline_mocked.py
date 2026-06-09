"""Integration test: full paper iteration with mocked agents.

Mocks all external calls (claude CLI, pdflatex, bibtex, git, mail, squeue)
at the subprocess level and verifies the end-to-end pipeline produces correct
state files, scores, agent call sequence, and cost tracking.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ---------------------------------------------------------------------------
#  Test cases
# ---------------------------------------------------------------------------

class TestIntegration:
    """Integration tests for the full paper iteration pipeline."""

    @pytest.fixture(autouse=True)
    def _mock_telegram(self):
        """Prevent tests from sending real Telegram messages."""
        with patch("ark.telegram.TelegramConfig.is_configured", new_callable=lambda: property(lambda self: False)):
            yield

    def test_pip_installability(self):
        """Verify package can be imported."""
        import ark
        from ark.orchestrator import Orchestrator
        from ark.cli import main
        from ark.memory import SimpleMemory
        from ark.engines import AgentMixin
        from ark.latex import CompilerMixin
        from ark.execution import ExecutionMixin
        from ark.pipeline import PipelineMixin

    def test_compile_latex_creates_pdf(self, mock_integration_project):
        """compile_latex() should create main.pdf."""
        orch, controller = mock_integration_project
        assert orch.compile_latex() is True
        assert (orch.latex_dir / "main.pdf").exists()

    def test_full_paper_iteration(self, mock_integration_project):
        """Run one full paper iteration and verify all state files."""
        orch, controller = mock_integration_project

        result = orch.run_paper_iteration()

        # Should continue (score 7 < threshold 8)
        assert result is True
        assert orch.iteration == 1

        # paper_state.yaml
        paper_state = orch.load_paper_state()
        assert paper_state["current_score"] == 7.0
        assert len(paper_state["reviews"]) == 1
        assert paper_state["reviews"][0]["score"] == 7.0

        # latest_review.md
        assert orch.latest_review_file.exists()
        review_content = orch.latest_review_file.read_text()
        assert "7" in review_content

        # action_plan.yaml
        assert orch.action_plan_file.exists()
        plan = orch._load_action_plan()
        assert len(plan.get("issues", [])) > 0

        # checkpoint.yaml
        assert orch.checkpoint_file.exists()
        checkpoint = orch.load_checkpoint()
        assert checkpoint["iteration"] == 1

        # Memory
        assert orch.memory.scores == [7.0]

        # Cost tracking
        assert len(orch._agent_stats) > 0
        for stat in orch._agent_stats:
            assert "agent_type" in stat
            assert "elapsed_seconds" in stat

        # Agent call sequence: should include key agents
        called = controller.agent_calls
        assert "reviewer" in called
        assert "planner" in called
        assert "writer" in called

    def test_paper_accepted_stops(self, mock_integration_project_factory):
        """Score >= threshold should stop iteration (return False)."""
        orch, controller = mock_integration_project_factory(
            project_name="test_accept", review_score=9.0
        )
        result = orch.run_paper_iteration()

        # Score 9 >= threshold 8 → paper accepted, should stop
        assert result is False
        paper_state = orch.load_paper_state()
        assert paper_state["status"] == "accepted"

    def test_score_zero_retries_reviewer(self, mock_integration_project_factory):
        """When initial score is 0 but review exists, should retry reviewer."""
        from tests.conftest import MockController

        class RetryController(MockController):
            """First reviewer call returns no score, second returns 7.0."""
            def _agent_stdout(self, agent_type, prompt):
                if agent_type == "reviewer":
                    self._reviewer_call_count += 1
                    if self._reviewer_call_count == 1:
                        # First call: long output but no parseable score
                        return "The paper is well written and presents interesting findings. " * 10
                    else:
                        return ("Overall Score: 7.0/10\nReview report update complete\n"
                                "The review has been updated with the explicit score.\n"
                                "All sections have been evaluated thoroughly.\n")
                return super()._agent_stdout(agent_type, prompt)

            def _write_review(self):
                if self._reviewer_call_count == 1:
                    # No score in file either
                    (self.state_dir / "latest_review.md").write_text(
                        "# Review\nThe paper is well written. " * 20 + "\n"
                        "### M1. Need experiments\n### m1. Typos\n"
                    )
                else:
                    super()._write_review()

        orch, controller = mock_integration_project_factory(
            project_name="test_retry", review_score=7.0, controller_cls=RetryController
        )
        result = orch.run_paper_iteration()

        # Should have called reviewer twice (initial + retry)
        reviewer_calls = [c for c in controller.agent_calls if c == "reviewer"]
        assert len(reviewer_calls) >= 2

        # Score should be 7.0 after retry
        paper_state = orch.load_paper_state()
        assert paper_state["current_score"] == 7.0

    def test_figure_phase_runs(self, mock_integration_project):
        """Verify figure phase runs during iteration (figure_fixer only if images available)."""
        orch, controller = mock_integration_project
        orch.run_paper_iteration()
        # Figure phase runs but figure_fixer only called if pdf_to_images returns page images.
        # In test environment without fitz, it skips figure_fixer gracefully.
        assert "reviewer" in controller.agent_calls  # Pipeline still runs

    def test_memory_updated(self, mock_integration_project):
        """After iteration, memory should record the score."""
        orch, controller = mock_integration_project
        orch.run_paper_iteration()
        assert orch.memory.scores == [7.0]
        assert orch.memory.best_score == 7.0

    def test_cost_tracking(self, mock_integration_project):
        """After iteration, _agent_stats should be populated."""
        orch, controller = mock_integration_project
        orch.run_paper_iteration()
        assert len(orch._agent_stats) > 0
        for stat in orch._agent_stats:
            assert "agent_type" in stat
            assert "elapsed_seconds" in stat
            assert "timestamp" in stat

    def test_cost_tracking_token_fields(self, mock_integration_project):
        """When claude returns JSON, _agent_stats and cost_report.yaml carry
        real token + USD aggregates parsed from the envelope."""
        orch, controller = mock_integration_project
        controller.json_mode = True
        orch.run_paper_iteration()

        # Per-call stats include the new token/cost fields
        assert orch._agent_stats, "expected at least one agent call"
        for stat in orch._agent_stats:
            for key in ("input_tokens", "output_tokens",
                        "cache_read_tokens", "cache_creation_tokens",
                        "cost_usd", "model"):
                assert key in stat, f"missing {key} in stat"
            # The mock envelope sets these to fixed values for non-error calls
            if not stat.get("error"):
                assert stat["input_tokens"] == 100
                assert stat["output_tokens"] == 50
                assert stat["cache_read_tokens"] == 800
                assert stat["cache_creation_tokens"] == 200
                assert stat["cost_usd"] == controller.json_cost_per_call
                assert stat["model"] == "test-model"

        # cost_report.yaml is written live and aggregates correctly
        report_path = orch.state_dir / "cost_report.yaml"
        assert report_path.exists(), "live cost report should exist after run"
        report = yaml.safe_load(report_path.read_text())
        n_calls = report["total_agent_calls"]
        assert n_calls > 0
        assert report["total_cost_usd"] == round(
            n_calls * controller.json_cost_per_call, 6
        )
        assert report["total_input_tokens"] == n_calls * 100
        assert report["total_output_tokens"] == n_calls * 50
        assert report["total_cache_read_tokens"] == n_calls * 800
        assert report["total_cache_creation_tokens"] == n_calls * 200
        # per-agent buckets carry the same fields
        for agent_name, bucket in report["per_agent"].items():
            assert "total_cost_usd" in bucket
            assert "total_input_tokens" in bucket
            assert bucket["total_cost_usd"] >= 0

    def test_cost_tracking_malformed_json(self, mock_integration_project):
        """When claude stdout is not valid JSON, agents fall back to plain
        text without crashing and stats append with zero cost fields."""
        orch, controller = mock_integration_project
        # json_mode stays False — the mock writes no base_state.json, so the
        # orchestrator reads no token/cost and records zero-cost stats
        orch.run_paper_iteration()
        assert orch._agent_stats, "expected at least one agent call"
        for stat in orch._agent_stats:
            # Fallback path leaves cost fields at their zero defaults
            assert stat.get("cost_usd", 0) == 0
            assert stat.get("input_tokens", 0) == 0
            assert stat.get("output_tokens", 0) == 0
        # Live cost report still written even with zero costs
        assert (orch.state_dir / "cost_report.yaml").exists()

    def test_terminal_error_in_review_aborts_fast(self, mock_integration_project_factory):
        """A non-retryable agent error (bad key/model) during the REVIEW step
        must abort the run (return False) — not get swallowed by the empty-review
        'retry' branch and loop forever. Regression test for the terminal-error
        fast-abort: the reviewer is the first agent call, so the check has to
        happen right after review, before plan/execute."""
        from io import BytesIO
        from unittest.mock import MagicMock
        from tests.conftest import MockController

        class AuthErrorController(MockController):
            """Reviewer call returns an OpenHands ConversationErrorEvent
            (AuthenticationError) instead of a normal MessageEvent."""
            def subprocess_popen(self, cmd, **kwargs):
                prompt = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
                agent_type = self._detect_agent(prompt)
                self.agent_calls.append(agent_type)
                if agent_type == "reviewer":
                    out = ("Conversation ID: testconverr\n" + json.dumps({
                        "kind": "ConversationErrorEvent",
                        "code": "AuthenticationError",
                        "detail": "invalid x-api-key",
                    }) + "\n")
                    proc = MagicMock()
                    proc.communicate.return_value = (out, "")
                    proc.returncode = 0
                    proc.stdout = BytesIO(out.encode())
                    proc.stderr = BytesIO(b"")
                    return proc
                return super().subprocess_popen(cmd, **kwargs)

        orch, controller = mock_integration_project_factory(
            project_name="test_terminal", controller_cls=AuthErrorController
        )
        result = orch.run_paper_iteration()

        # Aborted, not retried
        assert result is False
        assert orch._terminal_error and "AuthenticationError" in orch._terminal_error
        # Bailed out at review — never reached planning or writing
        assert "planner" not in controller.agent_calls
        assert "writer" not in controller.agent_calls

    def test_quota_cap_surfaces_real_error_and_aborts_fast(self, mock_integration_project_factory):
        """A workspace usage/spend cap (LiteLLM 'LLMBadRequestError' whose detail
        is a usage-limit message) must SURFACE the provider's real message and
        abort fast — not be misread as a transient rate-limit and busy-wait.
        Regression for the real prod event: OpenHands already retried internally,
        so a surfaced error is final; we abort with the real detail, no 300s/30min.
        The error code carries an 'LLM' prefix, which the old exact-match terminal
        set missed entirely."""
        from io import BytesIO
        from unittest.mock import MagicMock
        from tests.conftest import MockController

        class QuotaCapController(MockController):
            def subprocess_popen(self, cmd, **kwargs):
                prompt = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
                agent_type = self._detect_agent(prompt)
                self.agent_calls.append(agent_type)
                if agent_type == "reviewer":
                    out = ("Conversation ID: testquota\n" + json.dumps({
                        "kind": "ConversationErrorEvent",
                        "code": "LLMBadRequestError",
                        "detail": ("litellm.BadRequestError: AnthropicException - "
                                   "You have reached your specified workspace API "
                                   "usage limits. You will regain access on 2026-07-01."),
                    }) + "\n")
                    proc = MagicMock()
                    proc.communicate.return_value = (out, "")
                    proc.returncode = 0
                    proc.stdout = BytesIO(out.encode())
                    proc.stderr = BytesIO(b"")
                    return proc
                return super().subprocess_popen(cmd, **kwargs)

        orch, controller = mock_integration_project_factory(
            project_name="test_quota", controller_cls=QuotaCapController
        )
        result = orch.run_paper_iteration()

        # Aborted fast with the REAL provider message — not a busy-wait, not a
        # misleading "check your key".
        assert result is False
        assert orch._terminal_error
        assert "LLMBadRequestError" in orch._terminal_error
        assert "usage limit" in orch._terminal_error.lower()
        # Bailed at review — never reached planning or writing
        assert "planner" not in controller.agent_calls
        assert "writer" not in controller.agent_calls
