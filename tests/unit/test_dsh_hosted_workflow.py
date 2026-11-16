"""Hosted workflow boot and transitions with the real pinned DSH runtime."""

from datetime import datetime, timedelta
import os
from types import SimpleNamespace

import pytest

from ark.orchestrator.dsh_workflow import hosted_workflow_enabled, run_workflow


def test_hosted_uses_dsh_with_rollback(monkeypatch):
    monkeypatch.delenv("ARK_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("ARK_WORKFLOW_RUNTIME", raising=False)
    assert hosted_workflow_enabled("project-id")
    assert not hosted_workflow_enabled(None)
    monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", "python")
    assert not hosted_workflow_enabled("project-id")
    monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", "dsh")
    monkeypatch.setenv("ARK_AGENT_RUNTIME", "cli")
    assert not hosted_workflow_enabled("project-id")


@pytest.mark.parametrize("fail_execute", [False, True])
def test_real_hosted_workflow_resumes_python_checkpoint(monkeypatch, fail_execute):
    interpreter = os.environ.get("ARK_DSH_TEST_PYTHON")
    if not interpreter:
        pytest.skip("Set ARK_DSH_TEST_PYTHON to exercise the pinned runtime")
    monkeypatch.setenv("ARK_DSH_PYTHON", interpreter)
    calls = []

    def record(label, result=None):
        def run(*args, **kwargs):
            calls.append((label, args, kwargs))
            return result
        return run

    state = {"iteration": 1, "status": "in_progress", "steps_done": ["plan"]}
    project = SimpleNamespace(
        iteration=1, max_iterations=2, max_end_time=datetime.now() + timedelta(minutes=2),
        _stop_requested=False, _asked_this_iteration=False, _terminal_error=None,
        _quota_exhausted=fail_execute, paper_accept_threshold=8, hooks=None,
        _research_idea="Find a small measurable question", log=lambda *_: None,
        log_section=lambda *_: None, log_step=lambda *_: None,
        _prepare_research_workspace=record("setup", True),
        _should_run_research_phase=lambda: False, _run_research_phase=record("research"),
        _get_max_iteration_target=lambda: 2, _should_run_dev_phase=lambda: True,
        _begin_dev_phase=lambda: (state, 0, 1), _load_findings_summary=lambda: "findings",
        _save_dev_phase_state=lambda *_: None, _send_dev_phase_telegram=lambda *_: None,
        _abort_dev_on_terminal_error=lambda *_: False, _plan_experiments=record("dev-plan"),
        _experiment_approval_gate=record("approve"), _run_experiments=record("dev-run"),
        _analyze_results=record("dev-analyze"), _evaluate_completeness=record("dev-evaluate", True),
        _start_initial_draft=record("writing"), _generate_all_figures=record("figures"),
        _write_initial_draft=record("draft"), _deliver_dev_phase=record("deliver"),
        load_paper_state=lambda: {"current_score": 6}, get_resume_step=lambda: 3,
        _step_compile=record("compile", True), _step_review=record("review", ("existing", 7, [])),
        _check_acceptance=lambda *_: (False, False, False), _step_plan=record("plan", ({}, "")),
        _step_execute=record("execute"), _handle_quota_exhausted=record("quota", False),
        _step_validate=record("validate"), _iteration_finalize=record("finalize", False),
        _handle_terminal_error=record("terminal", False),
    )
    project._iteration_prep = lambda: setattr(project, "iteration", project.iteration + 1)
    assert run_workflow(project) == 1
    labels = [label for label, *_ in calls]
    assert labels == ["setup", "dev-run", "dev-analyze", "dev-evaluate", "writing", "figures", "draft", "deliver",
                      "compile", "review", "plan", "execute"] + (
        ["quota"] if fail_execute else ["validate", "finalize"]
    )
    assert calls[1][2]["resumed"] is True
    assert next(args for label, args, _ in calls if label == "execute") == (4, 5, 3, {}, "")
    assert state["steps_done"] == ["plan", "run", "analyze", "evaluate"]
