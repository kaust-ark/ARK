"""The orchestrator process must end when its run ends.

a247437e (2026-09-17) finished its paper, then hung for three hours inside
interpreter shutdown behind a forked PaperBanana pool worker that had
deadlocked after fork. The stuck-run watchdog killed it and filed the finished
paper as failed. The exit guard bounds that: after a grace period it names
what is still alive, kills our children, and exits with the run's code.
"""
import signal
import subprocess
import sys
import threading

import pytest

from ark.orchestrator import core


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_child_pids_sees_a_direct_child():
    child = _sleeper()
    try:
        assert child.pid in core._child_pids()
    finally:
        child.kill()
        child.wait()
    assert child.pid not in core._child_pids()


def test_guard_names_culprits_kills_children_and_exits_with_the_run_code():
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, name="stuck-pool-manager", daemon=False)
    stuck.start()
    child = _sleeper()
    fired, seen, killed, logs = threading.Event(), {}, [], []

    def fake_exit(code):
        seen["code"] = code
        fired.set()

    try:
        core._arm_exit_guard(1, grace=0.2, log=logs.append, _exit=fake_exit,
                             _kill=lambda pid, sig: killed.append((pid, sig)))
        assert fired.wait(5), "exit guard never fired"
        assert seen["code"] == 1
        assert "stuck-pool-manager" in logs[0], logs
        assert str(child.pid) in logs[0], logs
        assert (child.pid, signal.SIGKILL) in killed
    finally:
        release.set()
        stuck.join(1)
        child.kill()
        child.wait()


def test_guard_is_a_daemon_timer_that_a_normal_exit_can_cancel():
    t = core._arm_exit_guard(0, grace=30, _exit=lambda c: pytest.fail("fired after cancel"))
    try:
        assert t.daemon
        assert t.name == "ark-exit-guard"
    finally:
        t.cancel()
        t.join(1)
    assert not t.is_alive()


def test_grace_comes_from_the_environment_by_default(monkeypatch):
    monkeypatch.setenv("ARK_EXIT_GRACE_SECONDS", "7")
    t = core._arm_exit_guard(0, _exit=lambda c: pytest.fail("fired"))
    try:
        assert t.interval == 7
    finally:
        t.cancel()
        t.join(1)


def test_every_exit_path_of_main_arms_the_guard():
    """Chat turn, apply instruction and the full run all end the process, so
    all three arm the guard. A source-level check: main() needs a real project
    to execute, and the arming lines are the contract worth pinning."""
    import inspect
    src = inspect.getsource(core.main)
    assert src.count("_arm_exit_guard(") == 3
    assert "_arm_exit_guard(1 if final_status == \"failed\" else 0" in src
