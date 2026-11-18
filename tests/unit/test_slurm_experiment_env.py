"""Slurm experiments run in the PROJECT's conda env, not the shared base.

Two stacked defects (2026-08-11): the webapp pinned the experiment compute
config to the shared `slurm_conda_env`, and SlurmCompute's "use the project
env" fallback looked for `.env` — a directory provisioning has never
created (it makes `.conda_env`), so the branch was dead. A submitted Slurm
job therefore activated the shared base env, which holds neither the
packages the agent installed for this project nor, since the env was
slimmed, any GPU stack. It is also read-only, so installing there fails.
"""

from pathlib import Path

import pytest

from ark.compute.slurm import SlurmBackend


def _compute(code_dir: Path, compute_config=None):
    return SlurmBackend(
        config={"experiment_compute_backend": compute_config or {"type": "slurm"}},
        project_name="proj",
        code_dir=code_dir,
    )


def _make_project_env(code_dir: Path):
    env = code_dir / SlurmBackend.PROJECT_ENV_DIRNAME
    (env / "conda-meta").mkdir(parents=True)
    return env


def test_project_env_is_used_when_present(tmp_path):
    env = _make_project_env(tmp_path)
    c = _compute(tmp_path)
    assert c.conda_env == str(env)


def test_project_env_beats_a_shared_env_handed_down_in_config(tmp_path):
    """The shared base env must never win over the project's own."""
    env = _make_project_env(tmp_path)
    c = _compute(tmp_path, {"type": "slurm", "conda_env": "ark-base"})
    assert c.conda_env == str(env)


def test_explicit_env_still_used_when_no_project_env(tmp_path):
    c = _compute(tmp_path, {"type": "slurm", "conda_env": "ark-base"})
    assert c.conda_env == "ark-base"


def test_dirname_matches_what_provisioning_creates():
    """The bug was a name mismatch; pin the two together."""
    from website.dashboard.jobs import PROJECT_ENV_DIRNAME
    assert SlurmBackend.PROJECT_ENV_DIRNAME == PROJECT_ENV_DIRNAME


def test_agent_instructions_do_not_call_a_shared_env_project_local(tmp_path):
    _make_project_env(tmp_path)
    text = _compute(tmp_path).get_agent_instructions()
    assert "do not substitute a shared env" not in text
    assert SlurmBackend.PROJECT_ENV_DIRNAME in text


def test_agent_is_told_when_inline_execution_is_acceptable(tmp_path):
    """The old text said only "submit GPU jobs", so CPU work ran inline with
    no record of that choice — the user picked Slurm and could not tell."""
    text = _compute(tmp_path).get_agent_instructions()
    assert "inline" in text
    assert "findings" in text        # the choice must be recorded


def test_worker_command_reaches_research_and_cached_experiment_prompt(
        mock_integration_project, monkeypatch):
    """Regression for a context/specialization that copied the controller's Python."""
    from unittest.mock import Mock
    from ark import engines

    orchestrator, _ = mock_integration_project
    project = orchestrator.code_dir
    (project / "sandbox").mkdir()
    (project / "sandbox" / "run.sh").touch()
    _make_project_env(project)
    backend = _compute(project, {"type": "slurm", "conda_env": "shared-controller"})
    orchestrator._compute_backend = backend
    monkeypatch.setattr(backend, "_is_rocs_testbed", lambda: True)
    monkeypatch.setattr("ark.sandbox.structural_sandbox_status", lambda: (False, ""))
    worker = "./sandbox/run.sh .conda_env/bin/python"
    assert worker + " scripts/train.py" in backend.get_agent_instructions()

    legacy = "## Project-Specific Knowledge\nUse /controller/bin/python"
    (orchestrator.agents_dir / "experimenter.prompt").write_text(legacy)
    runner = Mock()
    runner.execute.return_value = (0, "", "", 1, False)
    runner.parse_output.return_value = {"result": "done", "usage": None}
    monkeypatch.setattr(engines, "get_cli_for_model", lambda *args, **kwargs: runner)
    for role, task in [("researcher", "Generate project_context.md"),
                       ("researcher", "Specialize the experimenter's prompt"),
                       ("experimenter", "Execute the plan")]:
        orchestrator.run_agent(role, task)
        prompt = runner.execute.call_args.kwargs["prompt"]
        assert worker + " -B -c" in prompt
        assert "inside the experiment's Slurm allocation" in prompt
        assert "older project context or specialized prompt" in prompt
        if role == "experimenter":
            assert prompt.index(legacy) < prompt.index(worker)

    orchestrator.run_agent("experimenter", "Continue", interrupted_session="prior",
                           resume_instructions="Do not resubmit a job.")
    assert "Do not resubmit a job." in runner.resume_instructions
    assert worker + " -B -c" in runner.resume_instructions
