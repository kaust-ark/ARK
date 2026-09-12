"""A local pid is only meaningful on the node that spawned it.

On 2026-04-16 and again on 2026-09-12 a second webapp came up on another node
(the systemd user units live on an NFS-shared home) and, seeing "no such
process" for every run spawned here, marked healthy runs done/failed, emailed
their owners, and deleted the env one of them was executing out of. These tests
pin what makes that impossible: handles carry their node, a control plane never
judges, signals or GCs a pid from another one, generated units are pinned to the
installing host, and a chat/apply helper never touches the run's exit sentinel.
"""
import os
import socket
from types import SimpleNamespace

import pytest

from ark.launcher.base import RUNNING, UNKNOWN
from ark.launcher.local import (
    LocalJobLauncher, handle_host, handle_is_local, handle_pid, local_handle,
)

HOST = socket.gethostname()


def test_local_handle_carries_this_node():
    assert local_handle(4242) == f"local:{HOST}:4242"


@pytest.mark.parametrize("handle,host,pid", [
    (f"local:{HOST}:123", HOST, 123),
    ("local:othernode:123", "othernode", 123),
    ("local:123", None, 123),                 # legacy, pre-host handle
    ("local:notapid", None, None),
    ("local:othernode:notapid", None, None),
    ("77", None, 77),                         # bare body, as _handle_pid accepted
])
def test_handle_parsing(handle, host, pid):
    assert handle_host(handle) == host
    assert handle_pid(handle) == pid


def test_own_and_legacy_handles_are_local_foreign_are_not():
    assert handle_is_local("local:123")
    assert handle_is_local(f"local:{HOST}:123")
    assert not handle_is_local("local:othernode:123")


def test_poll_never_judges_another_nodes_pid(tmp_path, monkeypatch):
    from website.dashboard import jobs
    monkeypatch.setattr(jobs, "poll_local_job",
                        lambda pid, log_dir: pytest.fail("polled a foreign pid"))
    res = LocalJobLauncher().poll("local:othernode:321", tmp_path)
    assert res.state == UNKNOWN
    assert res.raw == "foreign-host:othernode"


def test_poll_judges_its_own_qualified_handle(tmp_path, monkeypatch):
    from website.dashboard import jobs
    seen = []
    monkeypatch.setattr(jobs, "poll_local_job",
                        lambda pid, log_dir: seen.append(pid) or "RUNNING")
    res = LocalJobLauncher().poll(f"local:{HOST}:321", tmp_path)
    assert res.state == RUNNING
    assert seen == [321]


def test_cancel_never_signals_another_nodes_pid(tmp_path, monkeypatch):
    from website.dashboard import jobs
    monkeypatch.setattr(jobs, "cancel_local_job",
                        lambda pid: pytest.fail("signalled a foreign pid"))
    ran = []
    LocalJobLauncher().cancel("local:othernode:246", tmp_path,
                              on_complete=lambda: ran.append(1))
    assert ran == [1]


def test_gc_liveness_guard_assumes_another_nodes_pid_alive():
    from website.dashboard import app
    assert app._handle_process_alive("local:othernode:1") is True
    assert app._handle_process_alive(f"local:{HOST}:{os.getpid()}") is True
    assert app._handle_process_alive(f"local:{HOST}:4194304") is False
    assert app._handle_process_alive(f"local:{os.getpid()}") is True   # legacy


def test_generated_unit_is_pinned_to_the_installing_host(tmp_path):
    from ark.cli import _generate_service_unit
    unit = _generate_service_unit("0.0.0.0", 9527, tmp_path, "ARK test")
    assert f"\nConditionHost={HOST}\n" in unit
    assert unit.index("ConditionHost=") < unit.index("[Service]")


def _launch(tmp_path, monkeypatch, **kw):
    from website.dashboard import db, jobs
    captured = {}

    def fake_detached(wrapper, env, log_file, project_dir, project_id):
        captured["wrapper"] = wrapper
        return 4242

    monkeypatch.setattr(jobs, "find_conda_binary", lambda: None)
    monkeypatch.setattr(jobs, "control_plane_transport", lambda pid, s: ("", ""))
    monkeypatch.setattr(jobs, "build_subprocess_path", lambda: "/usr/bin")
    monkeypatch.setattr(jobs, "_launch_detached_orchestrator", fake_detached)
    monkeypatch.setattr(db, "resolve_db_path", lambda: str(tmp_path / "x.db"))
    pdir = tmp_path / "proj"
    pdir.mkdir(exist_ok=True)
    handle = jobs.launch_local_job("pid-1", "auto", 1, pdir, pdir / "logs",
                                   SimpleNamespace(slurm_conda_env=""), **kw)
    return handle, captured["wrapper"]


def test_launch_returns_a_host_qualified_handle(tmp_path, monkeypatch):
    handle, wrapper = _launch(tmp_path, monkeypatch)
    assert handle == f"local:{HOST}:4242"
    assert "local_exit.txt" in wrapper          # the run records its own outcome


def test_side_channel_leaves_the_runs_exit_sentinel_alone(tmp_path, monkeypatch):
    sentinel = tmp_path / "proj" / "logs" / "local_exit.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("1")
    handle, wrapper = _launch(tmp_path, monkeypatch, chat_message="hi")
    assert handle == f"local:{HOST}:4242"
    assert sentinel.read_text() == "1", "chat helper erased the run's exit sentinel"
    assert "local_exit.txt" not in wrapper, "chat helper would record its exit as the run's"
