"""After an orchestrator-side wait, the experimenter that submitted the jobs
collects their results in its own session instead of a cold one."""
from types import SimpleNamespace

import ark
from ark.execution import collect_after_jobs


def _host():
    calls = []
    host = SimpleNamespace(log_step=lambda *a, **k: None,
                           run_agent=lambda role, task, timeout=0, **kw: calls.append((role, task, kw)) or "ok")
    return host, calls


def test_a_long_wait_resumes_the_submitting_session():
    host, calls = _host()
    assert collect_after_jobs(host, ["s" * 32], waited_s=900) == "ok"
    role, task, kw = calls[0]
    assert role == "experimenter" and kw == {"continue_session": "s" * 32}
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
