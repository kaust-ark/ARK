"""After an orchestrator-side wait, the experimenter that submitted the jobs
collects their results in its own session instead of a cold one."""
from types import SimpleNamespace

import pytest

import ark
from ark.execution import ExecutionMixin, collect_after_jobs


def _host():
    calls = []
    host = SimpleNamespace(log_step=lambda *a, **k: None,
                           run_agent=lambda role, task, timeout=0, **kw: calls.append((role, task, kw)) or "ok")
    return host, calls


def test_a_long_wait_resumes_the_submitting_session():
    host, calls = _host()
    outcome = {}
    assert collect_after_jobs(host, ["s" * 32], waited_s=900, outcome_out=outcome) == "ok"
    role, task, kw = calls[0]
    assert role == "experimenter" and kw["continue_session"] == "s" * 32
    assert kw["outcome_out"] is outcome
    assert "results/" in task and "do not fill in" in task


def test_no_session_or_no_real_wait_does_nothing():
    host, calls = _host()
    assert collect_after_jobs(host, [], waited_s=900) is None
    assert collect_after_jobs(host, ["s" * 32], waited_s=5) is None
    assert calls == []


def test_nothing_runs_during_a_stop():
    host, calls = _host()
    ark.shutting_down.set()
    try:
        assert collect_after_jobs(host, ["s" * 32], waited_s=900) is None
    finally:
        ark.shutting_down.clear()
    assert calls == []


@pytest.mark.parametrize("collected_successfully", [True, False])
def test_issue_completion_uses_the_collection_turn_outcome(tmp_path, monkeypatch, collected_successfully):
    ticks = iter([0, 61])
    monkeypatch.setattr("ark.execution.time", SimpleNamespace(time=lambda: next(ticks)))
    noop = lambda *args, **kwargs: None
    host = ExecutionMixin.__new__(ExecutionMixin)
    host.code_dir = tmp_path
    host.log_step = noop
    host._save_action_plan = noop
    host._check_human_intervention = lambda **kwargs: False
    host._generate_figures_from_results = noop
    host._experiment_evidence_files = lambda: [tmp_path / "results" / "metrics.json"]
    host._compute_backend = SimpleNamespace(
        setup=lambda: {}, sync_to_backend=noop, get_agent_instructions=lambda: "",
        wait_for_completion=lambda **kwargs: True, sync_from_backend=lambda *args: True,
        teardown=noop,
    )

    def run_agent(role, task, *, timeout, outcome_out, session_out=None, continue_session=None):
        if session_out is not None:
            session_out.append("a" * 32)
            outcome_out["completed"] = not collected_successfully
            return "Submitted the job."
        assert continue_session == "a" * 32
        outcome_out["completed"] = collected_successfully
        return "Collected the results." if collected_successfully else ""

    host.run_agent = run_agent
    issue = {"id": "E1", "title": "Collect metrics"}
    host._run_experiment_task(issue, {"issues": [issue]})
    assert issue["status"] == ("completed" if collected_successfully else "failed")
