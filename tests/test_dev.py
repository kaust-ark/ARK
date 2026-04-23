"""Tests for DevMixin: development iteration loop, test runner, task grouping."""

import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_dev_orchestrator(tmp_path):
    """Create a minimal Orchestrator with DevMixin for dev mode testing."""
    from ark.orchestrator import Orchestrator

    # Create required project structure
    project_dir = tmp_path / "projects" / "testdev"
    project_dir.mkdir(parents=True)
    config = {
        "code_dir": str(tmp_path),
        "test_command": "pytest -v",
        "code_review_threshold": 7,
        "mode": "dev",
    }
    (project_dir / "config.yaml").write_text(yaml.dump(config))
    (project_dir / "agents").mkdir()

    # Create state dirs
    state_dir = tmp_path / "auto_research" / "state"
    state_dir.mkdir(parents=True)
    log_dir = tmp_path / "auto_research" / "logs"
    log_dir.mkdir(parents=True)
    latex_dir = tmp_path / "Latex"
    latex_dir.mkdir(parents=True)
    figures_dir = tmp_path / "Latex" / "figures"
    figures_dir.mkdir(parents=True)

    with patch("ark.orchestrator.ARK_ROOT", tmp_path):
        with patch("ark.cli.ensure_project_symlinks", return_value=None):
            orch = Orchestrator(project="testdev", mode="dev", code_dir=str(tmp_path))

    return orch


class TestDevStateIO:
    def test_load_empty_state(self, mock_dev_orchestrator):
        state = mock_dev_orchestrator.load_dev_state()
        assert state["spec_loaded"] is False
        assert state["tasks"] == []
        assert state["test_history"] == []

    def test_save_and_load_state(self, mock_dev_orchestrator):
        state = {
            "spec_loaded": True,
            "spec": "test spec",
            "tasks": [
                {"id": "T1", "title": "Task 1", "status": "pending"},
                {"id": "T2", "title": "Task 2", "status": "completed"},
            ],
            "test_history": [],
            "code_review_scores": [],
            "last_test_results": {},
        }
        mock_dev_orchestrator.save_dev_state(state)
        loaded = mock_dev_orchestrator.load_dev_state()
        assert loaded["spec_loaded"] is True
        assert len(loaded["tasks"]) == 2
        assert loaded["tasks"][0]["id"] == "T1"

    def test_load_state_sanitizes_none_tasks(self, mock_dev_orchestrator):
        """Tasks list with None entries should be filtered out."""
        state = {
            "tasks": [
                {"id": "T1", "title": "Valid", "status": "pending"},
                None,
                {"id": "T2", "title": "Also valid", "status": "completed"},
            ],
        }
        mock_dev_orchestrator.save_dev_state(state)
        loaded = mock_dev_orchestrator.load_dev_state()
        assert len(loaded["tasks"]) == 2
        assert all(t is not None for t in loaded["tasks"])


class TestParseTestResults:
    def test_pytest_format(self, mock_dev_orchestrator):
        output = "5 passed, 2 failed, 1 error in 3.5s"
        results = mock_dev_orchestrator._parse_test_results(output)
        assert results["passed"] == 5
        assert results["failed"] == 2
        assert results["errors"] == 1

    def test_pytest_all_passed(self, mock_dev_orchestrator):
        output = "10 passed in 1.2s"
        results = mock_dev_orchestrator._parse_test_results(output)
        assert results["passed"] == 10
        assert results["failed"] == 0
        assert results["errors"] == 0

    def test_unittest_format(self, mock_dev_orchestrator):
        output = "Ran 8 tests in 0.5s\n\nOK"
        results = mock_dev_orchestrator._parse_test_results(output)
        assert results["passed"] == 8
        assert results["failed"] == 0

    def test_unittest_failures(self, mock_dev_orchestrator):
        output = "Ran 10 tests in 2.0s\n\nFAILED (failures=3, errors=1)"
        results = mock_dev_orchestrator._parse_test_results(output)
        assert results["failed"] == 3
        assert results["errors"] == 1
        assert results["passed"] == 6

    def test_no_results(self, mock_dev_orchestrator):
        output = "No tests found"
        results = mock_dev_orchestrator._parse_test_results(output)
        assert results["passed"] == 0
        assert results["failed"] == 0
        assert results["errors"] == 0


class TestGroupTasksByDependency:
    def test_no_dependencies(self, mock_dev_orchestrator):
        tasks = [
            {"id": "T1", "title": "A"},
            {"id": "T2", "title": "B"},
            {"id": "T3", "title": "C"},
        ]
        groups = mock_dev_orchestrator._group_tasks_by_dependency(tasks)
        assert len(groups) == 1  # All in one group
        assert len(groups[0]) == 3

    def test_linear_dependencies(self, mock_dev_orchestrator):
        tasks = [
            {"id": "T1", "title": "A", "depends_on": []},
            {"id": "T2", "title": "B", "depends_on": ["T1"]},
            {"id": "T3", "title": "C", "depends_on": ["T2"]},
        ]
        groups = mock_dev_orchestrator._group_tasks_by_dependency(tasks)
        assert len(groups) == 3
        assert groups[0][0]["id"] == "T1"
        assert groups[1][0]["id"] == "T2"
        assert groups[2][0]["id"] == "T3"

    def test_parallel_with_shared_dependency(self, mock_dev_orchestrator):
        tasks = [
            {"id": "T1", "title": "Base", "depends_on": []},
            {"id": "T2", "title": "Feature A", "depends_on": ["T1"]},
            {"id": "T3", "title": "Feature B", "depends_on": ["T1"]},
        ]
        groups = mock_dev_orchestrator._group_tasks_by_dependency(tasks)
        assert len(groups) == 2
        assert len(groups[0]) == 1  # T1
        assert len(groups[1]) == 2  # T2, T3 in parallel

    def test_empty_tasks(self, mock_dev_orchestrator):
        groups = mock_dev_orchestrator._group_tasks_by_dependency([])
        assert groups == []

    def test_missing_dependency_handled(self, mock_dev_orchestrator):
        """Task depending on non-existent ID should still be scheduled."""
        tasks = [
            {"id": "T1", "title": "A", "depends_on": ["T_NONEXISTENT"]},
        ]
        groups = mock_dev_orchestrator._group_tasks_by_dependency(tasks)
        assert len(groups) >= 1


class TestParseCodeReviewScore:
    def test_standard_format(self, mock_dev_orchestrator):
        output = "## Score: 7.5/10"
        assert mock_dev_orchestrator._parse_code_review_score(output) == 7.5

    def test_overall_score(self, mock_dev_orchestrator):
        output = "Overall Score: 8/10\nSome other text"
        assert mock_dev_orchestrator._parse_code_review_score(output) == 8.0

    def test_code_review_score(self, mock_dev_orchestrator):
        output = "Code Review Score: 6.5/10"
        assert mock_dev_orchestrator._parse_code_review_score(output) == 6.5

    def test_from_file(self, mock_dev_orchestrator):
        review_file = mock_dev_orchestrator.state_dir / "code_review.md"
        review_file.write_text("# Code Review\n\n## Score: 9/10\n")
        assert mock_dev_orchestrator._parse_code_review_score("no score") == 9.0

    def test_no_score_defaults_to_5(self, mock_dev_orchestrator):
        assert mock_dev_orchestrator._parse_code_review_score("no score here") == 5.0


class TestShouldSwitchToPaper:
    def test_not_ready_no_tasks(self, mock_dev_orchestrator):
        dev_state = {"tasks": [], "code_review_scores": [], "last_test_results": {}}
        assert mock_dev_orchestrator._should_switch_to_paper(dev_state) is False

    def test_not_ready_incomplete_tasks(self, mock_dev_orchestrator):
        dev_state = {
            "tasks": [{"id": "T1", "status": "pending"}],
            "code_review_scores": [{"score": 8}],
            "last_test_results": {"failed": 0, "errors": 0},
        }
        assert mock_dev_orchestrator._should_switch_to_paper(dev_state) is False

    def test_not_ready_low_review_score(self, mock_dev_orchestrator):
        dev_state = {
            "tasks": [{"id": "T1", "status": "completed"}],
            "code_review_scores": [{"score": 5}],
            "last_test_results": {"failed": 0, "errors": 0},
        }
        assert mock_dev_orchestrator._should_switch_to_paper(dev_state) is False

    def test_not_ready_failing_tests(self, mock_dev_orchestrator):
        dev_state = {
            "tasks": [{"id": "T1", "status": "completed"}],
            "code_review_scores": [{"score": 8}],
            "last_test_results": {"failed": 2, "errors": 0},
        }
        assert mock_dev_orchestrator._should_switch_to_paper(dev_state) is False

    def test_auto_switch(self, mock_dev_orchestrator):
        mock_dev_orchestrator.config["auto_switch_to_paper"] = True
        dev_state = {
            "tasks": [{"id": "T1", "status": "completed"}],
            "code_review_scores": [{"score": 8}],
            "last_test_results": {"failed": 0, "errors": 0},
        }
        assert mock_dev_orchestrator._should_switch_to_paper(dev_state) is True


class TestSwitchToPaperMode:
    def test_mode_changes(self, mock_dev_orchestrator):
        mock_dev_orchestrator.mode = "dev"
        mock_dev_orchestrator._switch_to_paper_mode()
        assert mock_dev_orchestrator.mode == "paper"
        assert mock_dev_orchestrator.iteration == 0

    def test_dev_state_records_switch(self, mock_dev_orchestrator):
        mock_dev_orchestrator.save_dev_state({"tasks": [], "spec_loaded": True})
        mock_dev_orchestrator._switch_to_paper_mode()
        state = mock_dev_orchestrator.load_dev_state()
        assert "paper_switch_at" in state


class TestLoadSpec:
    def test_from_goal_anchor(self, mock_dev_orchestrator):
        mock_dev_orchestrator.config["goal_anchor"] = "My research project spec"
        spec = mock_dev_orchestrator._load_spec()
        assert "research project spec" in spec

    def test_from_dev_state(self, mock_dev_orchestrator):
        mock_dev_orchestrator.save_dev_state({"spec": "Stored spec text", "spec_loaded": True, "tasks": []})
        spec = mock_dev_orchestrator._load_spec()
        assert spec == "Stored spec text"

    def test_empty_when_nothing_configured(self, mock_dev_orchestrator):
        spec = mock_dev_orchestrator._load_spec()
        assert spec == ""


class TestReviewFeedbackLoop:
    """Verify that the dev planner consumes the latest code review, not just its score.

    This is the review->planning closure: without it, dev multi-round has no
    mechanism to act on reviewer-identified issues (only on test failures).
    """

    def test_planner_receives_review_as_prior_context(self, mock_dev_orchestrator):
        orch = mock_dev_orchestrator
        review_file = orch.state_dir / "code_review.md"
        review_md = (
            "# Code Review -- Iteration 1\n\n"
            "## Score: 6/10\n\n"
            "## Issues\n"
            "### C1. Missing error handling in parser\n"
            "Parser crashes on malformed input.\n"
        )
        review_file.write_text(review_md)
        orch.save_dev_state({
            "spec_loaded": True, "spec": "S",
            "tasks": [{"id": "T1", "status": "completed"}],
            "code_review_scores": [{"iteration": 1, "score": 6}],
            "last_test_results": {"passed": 1, "failed": 0, "errors": 0},
        })

        with patch.object(orch, "run_agent", return_value="") as mock_run:
            orch._run_dev_planning_phase(orch.load_dev_state())

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert kwargs.get("prior_context") == review_md, \
            "full review markdown must be passed as prior_context"
        prompt = args[1] if len(args) >= 2 else kwargs["task"]
        assert "auto_research/state/code_review.md" in prompt, \
            "prompt must point planner at the review file"
        assert "C1" in prompt or "reviewer issue" in prompt.lower(), \
            "prompt must instruct planner to translate review issues into tasks"

    def test_planner_handles_missing_review(self, mock_dev_orchestrator):
        orch = mock_dev_orchestrator
        assert not (orch.state_dir / "code_review.md").exists()
        orch.save_dev_state({
            "spec_loaded": True, "spec": "S", "tasks": [],
            "code_review_scores": [], "last_test_results": {},
        })

        with patch.object(orch, "run_agent", return_value="") as mock_run:
            orch._run_dev_planning_phase(orch.load_dev_state())

        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("prior_context") == "", \
            "no review file → prior_context must be empty"
        args, _ = mock_run.call_args
        prompt = args[1] if len(args) >= 2 else kwargs["task"]
        assert "No reviews yet" in prompt

    def test_prompt_drops_old_score_only_dump(self, mock_dev_orchestrator):
        """Regression: prompt must NOT surface only the score dict as 'Code Review Feedback'.

        The old code did `yaml.dump(review_scores[-1])`, which told the planner
        the feedback WAS just {iteration, score, timestamp}. The review content
        (issues to fix) was never surfaced. This test locks in the fix.
        """
        orch = mock_dev_orchestrator
        (orch.state_dir / "code_review.md").write_text(
            "## Score: 6/10\n### C1. Fix X\n"
        )
        orch.save_dev_state({
            "spec_loaded": True, "spec": "",
            "tasks": [{"id": "T1", "status": "completed"}],
            "code_review_scores": [
                {"iteration": 1, "score": 6, "timestamp": "2026-04-22T00:00:00"},
            ],
            "last_test_results": {},
        })

        with patch.object(orch, "run_agent", return_value="") as mock_run:
            orch._run_dev_planning_phase(orch.load_dev_state())

        args, kwargs = mock_run.call_args
        prompt = args[1] if len(args) >= 2 else kwargs["task"]
        assert "timestamp: '2026-04-22" not in prompt, \
            "regression: prompt should not dump the score-dict as the review"


class TestCodeReviewThreshold:
    def test_default_threshold(self, mock_dev_orchestrator):
        assert mock_dev_orchestrator.code_review_threshold == 7

    def test_custom_threshold(self, tmp_path):
        from ark.orchestrator import Orchestrator

        project_dir = tmp_path / "projects" / "custom"
        project_dir.mkdir(parents=True)
        config = {
            "code_dir": str(tmp_path),
            "code_review_threshold": 9,
        }
        (project_dir / "config.yaml").write_text(yaml.dump(config))
        (project_dir / "agents").mkdir()

        (tmp_path / "auto_research" / "state").mkdir(parents=True)
        (tmp_path / "auto_research" / "logs").mkdir(parents=True)
        (tmp_path / "Latex" / "figures").mkdir(parents=True)

        with patch("ark.orchestrator.ARK_ROOT", tmp_path):
            with patch("ark.cli.ensure_project_symlinks", return_value=None):
                orch = Orchestrator(project="custom", code_dir=str(tmp_path))

        assert orch.code_review_threshold == 9
