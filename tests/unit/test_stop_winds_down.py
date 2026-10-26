"""A Stop must stop the run, not just its main thread.

954d7885, 2026-10-26: the dashboard's Stop SIGTERMs the launch unit. The
orchestrator's handler raised SystemExit on the main thread only, which then
waited for the two parallel experimenters. One sat in a SLURM wait, the other
read its SIGTERMed CLI (-15) as an empty run and started a fresh agent on the
owner's key. The unit hung until systemd's SIGKILL 90 s later, every time.
"""
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import ark
import ark.engines as eng

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clear_stop_flag():
    ark.shutting_down.clear()
    yield
    ark.shutting_down.clear()


def _agent(tmp_path, execute, parse):
    """The real run_agent, with a fake runner in place of the openhands CLI."""
    calls = []

    class Runner:
        def execute(self, **kw):
            calls.append(kw)
            return execute()

        def parse_output(self, stdout):
            return parse()

    (tmp_path / "experimenter.prompt").write_text("You run experiments.")
    o = eng.AgentMixin()
    noop = lambda *a, **k: None
    for name, val in dict(
            agents_dir=tmp_path, state_dir=tmp_path, code_dir=tmp_path,
            model="m", memory=SimpleNamespace(get_context_for_agent=lambda a: ""),
            _agent_stats=[], _agent_empty_count=0, _rate_limit_notified=False,
            _terminal_error=None, log=noop, log_step=noop, log_summary_box=noop,
            send_notification=noop, save_checkpoint=noop, _write_cost_report=noop,
            _cleanup_cli_state=noop, _build_path_boundary=lambda: "",
            _summarize_agent_output=lambda a, r: []).items():
        setattr(o, name, val)
    return o, calls, Runner


def _run(o, Runner, countdown=lambda s: SimpleNamespace(run=lambda: None)):
    with mock.patch.object(eng, "get_cli_for_model", lambda m: Runner()), \
            mock.patch.object(eng, "RateLimitCountdown", countdown):
        return o.run_agent("experimenter", "run E1", timeout=600)


class TestRunAgentAfterAStop:
    def test_no_agent_starts_once_the_run_is_stopping(self, tmp_path):
        o, calls, Runner = _agent(tmp_path, lambda: (0, "{}", "", 1, False),
                                  lambda: {"result": "x"})
        ark.shutting_down.set()
        assert _run(o, Runner) == ""
        assert calls == []

    def test_a_cli_killed_by_the_stop_is_not_rerun(self, tmp_path):
        """The 954d7885 case: SIGTERM reaches the CLI, it exits -15 with no
        answer. Before, that was an empty run and a fresh agent started."""
        def killed():
            ark.shutting_down.set()
            return -15, "", "", 915, False
        o, calls, Runner = _agent(tmp_path, killed, lambda: {"result": ""})
        t0 = time.monotonic()
        _run(o, Runner)
        assert len(calls) == 1
        assert time.monotonic() - t0 < 5, "waited out the empty-run backoff"

    def test_an_error_event_during_the_stop_is_not_terminal(self, tmp_path):
        def killed():
            ark.shutting_down.set()
            return -15, "", "", 3, False
        o, calls, Runner = _agent(
            tmp_path, killed,
            lambda: {"result": "", "error_code": "AuthenticationError",
                     "error_detail": "connection closed"})
        _run(o, Runner)
        assert o._terminal_error is None and len(calls) == 1

    def test_a_stop_during_a_transient_wait_drops_the_retry(self, tmp_path):
        o, calls, Runner = _agent(
            tmp_path, lambda: (0, "{}", "", 3, False),
            lambda: {"result": "", "error_code": "InternalServerError",
                     "error_detail": "upstream 500"})
        _run(o, Runner, countdown=lambda s: SimpleNamespace(run=ark.shutting_down.set))
        assert len(calls) == 1

    def test_without_a_stop_an_empty_run_is_still_retried(self, tmp_path):
        o, calls, Runner = _agent(tmp_path, lambda: (0, "{}", "", 3, False),
                                  lambda: {"result": ""})
        with mock.patch.object(ark.shutting_down, "wait", lambda s: False):
            _run(o, Runner)
        assert len(calls) == 4


def test_the_rate_limit_countdown_ends_on_a_stop():
    from ark.ui import RateLimitCountdown
    ark.shutting_down.set()
    t0 = time.monotonic()
    RateLimitCountdown(300).run()
    assert time.monotonic() - t0 < 2


def test_the_slurm_wait_returns_when_the_run_stops(tmp_path):
    import threading
    from ark.compute.slurm import SlurmBackend
    lines = []
    b = SlurmBackend({}, "p", tmp_path, log_fn=lambda m, *a: lines.append(m))
    b._query_target_jobs = lambda: (["123"], 0)
    t0 = time.monotonic()
    threading.Timer(0.3, ark.shutting_down.set).start()
    assert b.wait_for_completion(max_wait_hours=4) is False
    assert time.monotonic() - t0 < 5
    assert any("jobs running, waiting" in m for m in lines)  # it really waited
    assert "Run stopping" in lines[-1]


class TestTheSignalHandler:
    def test_it_flags_the_stop_arms_the_guard_and_exits(self):
        from ark.orchestrator import core
        with mock.patch.object(core, "_arm_exit_guard") as guard:
            with pytest.raises(SystemExit):
                core._on_stop_signal(signal.SIGTERM, None)
        assert ark.shutting_down.is_set()
        assert guard.call_args.kwargs["grace"] < 90  # before systemd's SIGKILL

    def test_a_second_sigterm_leaves_the_wrap_up_alone(self):
        from ark.orchestrator import core
        ark.shutting_down.set()
        with mock.patch.object(core, "_arm_exit_guard") as guard:
            assert core._on_stop_signal(signal.SIGTERM, None) is None
        guard.assert_not_called()


def test_a_stopped_process_with_busy_workers_exits_promptly(tmp_path):
    """End to end in a real process: the main thread waits on a pool, as
    _run_execute_phase does, while two workers sit in SLURM waits. A SIGTERM
    must end the process in seconds, not after the workers' next poll."""
    script = tmp_path / "stop_me.py"
    script.write_text(textwrap.dedent(f"""
        import os, signal, sys, threading
        sys.path.insert(0, {str(REPO)!r})
        from concurrent.futures import ThreadPoolExecutor
        from ark.orchestrator import core
        from ark.compute.slurm import SlurmBackend
        signal.signal(signal.SIGTERM, core._on_stop_signal)

        def wait():
            b = SlurmBackend({{}}, "p", ".", log_fn=lambda m, *a: print(m, flush=True))
            b._query_target_jobs = lambda: (["123"], 0)
            b.wait_for_completion(max_wait_hours=4)

        threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM)).start()
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                for f in [ex.submit(wait), ex.submit(wait)]:
                    f.result()
        finally:
            print("wrapped up", flush=True)
    """))
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, str(script)], capture_output=True,
                       text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(REPO)})
    assert "wrapped up" in p.stdout, p.stderr
    assert p.stdout.count("Run stopping") == 2, p.stdout + p.stderr  # both workers
    assert "Traceback" not in p.stderr, p.stderr
    assert time.monotonic() - t0 < 20


def test_an_interrupted_experiment_is_left_pending(tmp_path):
    """Not "completed" (which then generated figures mid-stop), not "failed"."""
    from ark.execution import ExecutionMixin
    o = ExecutionMixin.__new__(ExecutionMixin)
    events, saved = [], []
    noop = lambda *a, **k: None
    rec = lambda name, ret=None: (lambda *a, **k: events.append(name) or ret)
    o._compute_backend = SimpleNamespace(
        setup=rec("setup", {}), sync_to_backend=rec("push"),
        get_agent_instructions=rec("instr", ""), wait_for_completion=rec("wait", False),
        sync_from_backend=rec("pull"), teardown=rec("teardown"))

    def experimenter(agent_type, task, timeout=None, **kw):
        ark.shutting_down.set()           # the Stop lands mid-call
        return "partial summary"
    for name, val in dict(code_dir=tmp_path, log=noop, log_step=noop,
                          run_agent=experimenter,
                          _save_action_plan=lambda plan: saved.append(plan["issues"][0]["status"]),
                          _check_human_intervention=lambda **k: False,
                          _generate_figures_from_results=rec("figures")).items():
        setattr(o, name, val)
    issue = {"id": "E4", "title": "x", "description": "run it"}
    o._run_experiment_task(issue, {"issues": [issue]})
    assert issue["status"] == "pending" and saved[-1] == "pending"
    assert "figures" not in events and "teardown" in events


def test_an_empty_chat_answer_is_not_reported_as_done(tmp_path):
    """The owner's audit request hit Insufficient Balance on its first call and
    the chat answered "Done."."""
    import ark.chat_agent as ca
    from ark.pipeline import PipelineMixin
    said = []
    o = PipelineMixin.__new__(PipelineMixin)
    noop = lambda *a, **k: None
    for name, val in dict(start_telegram_listener=noop, _ensure_control_poller=noop,
                          _set_activity=noop, log=noop, config={}, model="m",
                          code_dir=tmp_path, _record_chat_cost=noop,
                          _chat=lambda role, text, kind: said.append(text)).items():
        setattr(o, name, val)
    with mock.patch.object(ca, "run_chat_turn", lambda **kw: ("", None, False)), \
            mock.patch.object(ca, "load_conversation_id", lambda ws: None):
        o.chat_turn("back up the project")
    assert said and "Done" not in said[-1] and "did not finish" in said[-1]
