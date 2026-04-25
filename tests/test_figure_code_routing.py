"""FIGURE_CODE_REQUIRED tasks must route to the coder agent, not writer.

Context: writer was given FIGURE_CODE_REQUIRED work historically, but
in practice writer defaults to editing main.tex (its primary domain)
and frequently skips the Python edit entirely, producing a recurring
"WARNING: FIGURE_CODE_REQUIRED task but Python not modified!" that
wastes a whole review iteration. Plotting scripts are Python — that's
coder's domain.
"""

import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_paper_orchestrator(tmp_path):
    from ark.orchestrator import Orchestrator

    project_dir = tmp_path / "projects" / "testpaper"
    project_dir.mkdir(parents=True)
    config = {
        "code_dir": str(tmp_path),
        "mode": "paper",
        "latex_dir": "Latex",
        "figures_dir": "Latex/figures",
        "create_figures_script": "scripts/create_paper_figures.py",
    }
    (project_dir / "config.yaml").write_text(yaml.dump(config))
    (project_dir / "agents").mkdir()
    (tmp_path / "auto_research" / "state").mkdir(parents=True)
    (tmp_path / "auto_research" / "logs").mkdir(parents=True)
    (tmp_path / "Latex" / "figures").mkdir(parents=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "create_paper_figures.py").write_text(
        "import matplotlib.pyplot as plt\n\n"
        "def fig_ablation():\n"
        "    fig, ax = plt.subplots(figsize=(6, 4))\n"
        "    ax.set_ylabel('%')\n"
        "    fig.savefig('fig_ablation.pdf')\n"
    )

    with patch("ark.orchestrator.core.ARK_ROOT", tmp_path):
        with patch("ark.cli.ensure_project_symlinks", return_value=None):
            orch = Orchestrator(project="testpaper", mode="paper", code_dir=str(tmp_path))
    return orch


class TestFigureCodeRouting:
    def test_figure_code_required_invokes_coder_not_writer(self, mock_paper_orchestrator):
        """The heart of the fix: agent name passed to run_agent must be
        'coder' when the first non-concept figure task is processed."""
        orch = mock_paper_orchestrator
        action_plan = {
            "issues": [
                {
                    "id": "m1",
                    "type": "FIGURE_CODE_REQUIRED",
                    "title": "fig_ablation y-axis too small",
                    "description": "Increase y-axis font to 9pt",
                    "status": "pending",
                    "actions": [{
                        "agent": "writer",
                        "target_file": "scripts/create_paper_figures.py",
                        "target_function": "fig_ablation",
                        "modification": "ax.set_ylabel('Reduction (%)', fontsize=9)",
                    }],
                },
                {
                    "id": "m2",
                    "type": "WRITING_ONLY",
                    "title": "fix typo",
                    "description": "fix a typo in section 2",
                    "status": "pending",
                    "actions": [],
                },
            ],
        }

        invocations = []

        def fake_run_agent(agent_type, prompt, **kwargs):
            invocations.append((agent_type, prompt))
            return ""

        orch.run_agent = fake_run_agent
        orch._save_action_plan = lambda *a, **k: None
        orch.compile_latex = lambda *a, **k: None
        orch._quota_exhausted = False

        orch._run_writing_phase(action_plan, prior_context="")

        figure_invocations = [
            (agent, p) for agent, p in invocations
            if "FIGURE_CODE_REQUIRED" in p or "plotting script" in p.lower()
        ]
        assert figure_invocations, "expected at least one figure-code invocation"
        agent_name, prompt = figure_invocations[0]
        assert agent_name == "coder", (
            f"FIGURE_CODE_REQUIRED must route to coder, got {agent_name!r}. "
            f"Writer's LaTeX-first bias causes it to skip the .py edit."
        )

    def test_prompt_forbids_latex_edits_and_fabrication(self, mock_paper_orchestrator):
        """Hard rules must be present in the coder prompt so coder doesn't
        slip into the writer's old failure mode (editing main.tex) or
        invent functions that don't exist."""
        orch = mock_paper_orchestrator
        action_plan = {
            "issues": [
                {
                    "id": "m1",
                    "type": "FIGURE_CODE_REQUIRED",
                    "title": "fig_ablation",
                    "description": "split panel",
                    "status": "pending",
                    "actions": [{
                        "target_file": "scripts/create_paper_figures.py",
                        "target_function": "fig_ablation",
                    }],
                },
                {
                    "id": "m2",
                    "type": "WRITING_ONLY",
                    "title": "fix typo",
                    "description": "fix a typo",
                    "status": "pending",
                    "actions": [],
                },
            ],
        }
        captured = []
        orch.run_agent = lambda agent, prompt, **kw: captured.append(prompt) or ""
        orch._save_action_plan = lambda *a, **k: None
        orch.compile_latex = lambda *a, **k: None
        orch._quota_exhausted = False

        orch._run_writing_phase(action_plan, prior_context="")

        prompt = next((p for p in captured if "FIGURE_CODE_REQUIRED" in p), "")
        assert prompt, "figure-code prompt not captured"
        assert "Do NOT edit main.tex" in prompt or "do NOT edit main.tex" in prompt.lower() or "not edit main.tex" in prompt.lower()
        assert "not invent" in prompt.lower() or "do not invent" in prompt.lower()
        assert "not fabricate" in prompt.lower() or "fabricate data" in prompt.lower()
