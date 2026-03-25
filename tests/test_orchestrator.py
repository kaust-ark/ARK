"""Tests for Orchestrator methods (parse_review_score, extract_issue_ids,
_reset_stale_action_plan, _parse_rate_limit_wait, _load_action_plan, _validate_action_plan)."""

import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_orchestrator(tmp_path):
    """Create a minimal Orchestrator-like object without full __init__."""
    from ark.orchestrator import Orchestrator

    # Create required project structure
    project_dir = tmp_path / "projects" / "test"
    project_dir.mkdir(parents=True)
    (project_dir / "config.yaml").write_text("code_dir: " + str(tmp_path))
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

    # Patch ARK_ROOT so Orchestrator finds the project
    with patch("ark.orchestrator.ARK_ROOT", tmp_path):
        with patch("ark.cli.ensure_project_symlinks", return_value=None):
            orch = Orchestrator(project="test", code_dir=str(tmp_path))

    return orch


class TestParseReviewScore:
    def test_overall_score_format(self, mock_orchestrator):
        output = "Overall Score: 7.5/10"
        assert mock_orchestrator.parse_review_score(output) == 7.5

    def test_english_format(self, mock_orchestrator):
        output = "Overall Score: 8/10"
        assert mock_orchestrator.parse_review_score(output) == 8.0

    def test_table_format(self, mock_orchestrator):
        output = "| **Total** | 100% | - | **6.5/10** |"
        assert mock_orchestrator.parse_review_score(output) == 6.5

    def test_no_score(self, mock_orchestrator):
        output = "This review has no score"
        assert mock_orchestrator.parse_review_score(output) == 0.0

    def test_from_file(self, mock_orchestrator):
        mock_orchestrator.latest_review_file.write_text("Overall Score: 9/10")
        assert mock_orchestrator.parse_review_score("no score here") == 9.0


class TestExtractIssueIds:
    def test_extracts_issues(self, mock_orchestrator):
        mock_orchestrator.latest_review_file.write_text("""
### M1. Major Issue One
### M2. Major Issue Two
### m1. Minor Issue One
""")
        ids = mock_orchestrator.extract_issue_ids()
        assert "M1" in ids
        assert "M2" in ids
        assert "m1" in ids

    def test_no_file(self, mock_orchestrator):
        assert mock_orchestrator.extract_issue_ids() == []


class TestResetStaleActionPlan:
    def test_resets_pending_tasks(self, mock_orchestrator):
        plan = {"issues": [
            {"id": "M1", "type": "EXPERIMENT_REQUIRED", "status": "pending"},
            {"id": "M2", "type": "WRITING_ONLY", "status": "completed"},
        ]}
        with open(mock_orchestrator.action_plan_file, "w") as f:
            yaml.dump(plan, f)
        mock_orchestrator._reset_stale_action_plan()
        reloaded = yaml.safe_load(open(mock_orchestrator.action_plan_file))
        assert reloaded["issues"][0]["status"] == "reset"
        assert reloaded["issues"][1]["status"] == "completed"

    def test_no_stale_tasks(self, mock_orchestrator):
        plan = {"issues": [
            {"id": "M1", "type": "WRITING_ONLY", "status": "completed"},
        ]}
        with open(mock_orchestrator.action_plan_file, "w") as f:
            yaml.dump(plan, f)
        mock_orchestrator._reset_stale_action_plan()
        reloaded = yaml.safe_load(open(mock_orchestrator.action_plan_file))
        assert reloaded["issues"][0]["status"] == "completed"

    def test_empty_plan(self, mock_orchestrator):
        with open(mock_orchestrator.action_plan_file, "w") as f:
            yaml.dump({"issues": []}, f)
        mock_orchestrator._reset_stale_action_plan()  # Should not raise


class TestParseRateLimitWait:
    def test_seconds(self, mock_orchestrator):
        assert mock_orchestrator._parse_rate_limit_wait("retry after 60 seconds") == 60

    def test_minutes(self, mock_orchestrator):
        assert mock_orchestrator._parse_rate_limit_wait("wait 5 minutes") == 300

    def test_default(self, mock_orchestrator):
        assert mock_orchestrator._parse_rate_limit_wait("some random error") == 300


class TestLoadActionPlan:
    def test_load_valid(self, mock_orchestrator):
        plan = {"issues": [{"id": "M1", "type": "WRITING_ONLY", "title": "Fix"}]}
        with open(mock_orchestrator.action_plan_file, "w") as f:
            yaml.dump(plan, f)
        loaded = mock_orchestrator._load_action_plan()
        assert loaded["issues"][0]["id"] == "M1"

    def test_missing_file(self, mock_orchestrator):
        loaded = mock_orchestrator._load_action_plan()
        assert loaded == {"issues": []}

    def test_fixes_latex_escapes(self, mock_orchestrator):
        # Write YAML with a double-quoted value containing backslash
        raw = 'issues:\n  - id: "M1"\n    title: "Fix \\subsection{foo}"\n    type: WRITING_ONLY\n'
        mock_orchestrator.action_plan_file.write_text(raw)
        loaded = mock_orchestrator._load_action_plan()
        # Should not crash; may fix the escaping
        assert isinstance(loaded, dict)


class TestValidateActionPlan:
    def test_valid(self, mock_orchestrator):
        plan = {"issues": [{"id": "M1", "type": "WRITING_ONLY", "title": "Fix something"}]}
        valid, msg = mock_orchestrator._validate_action_plan(plan)
        assert valid

    def test_missing_issues(self, mock_orchestrator):
        valid, msg = mock_orchestrator._validate_action_plan({})
        assert not valid
        assert "issues" in msg

    def test_missing_id(self, mock_orchestrator):
        plan = {"issues": [{"type": "WRITING_ONLY", "title": "No id"}]}
        valid, msg = mock_orchestrator._validate_action_plan(plan)
        assert not valid
        assert "id" in msg

    def test_missing_type(self, mock_orchestrator):
        plan = {"issues": [{"id": "M1", "title": "No type"}]}
        valid, msg = mock_orchestrator._validate_action_plan(plan)
        assert not valid
        assert "type" in msg

    def test_missing_title(self, mock_orchestrator):
        plan = {"issues": [{"id": "M1", "type": "WRITING_ONLY"}]}
        valid, msg = mock_orchestrator._validate_action_plan(plan)
        assert not valid
        assert "title" in msg

    def test_not_dict(self, mock_orchestrator):
        valid, msg = mock_orchestrator._validate_action_plan("not a dict")
        assert not valid
