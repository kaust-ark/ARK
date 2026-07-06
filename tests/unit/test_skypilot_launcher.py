"""Layer-2 SkyPilotVmJobLauncher lifecycle (folded Phases 5+6, ADR-0010, PR3).

Exercises launch/poll/cancel against a *mocked* ``sky`` SDK and mocked webapp
transport helpers — no real cloud, so these run in CI (unmarked). The real
multi-cloud provisioning path is covered by the PR5 acceptance run.
"""

from __future__ import annotations

import os
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2")  # website.dashboard.jobs imports jinja2 at module load

import website.dashboard.jobs as jobs  # noqa: E402
from ark.launcher import (  # noqa: E402
    SkyPilotVmJobLauncher, LaunchSpec, RUNNING, GONE, UNKNOWN,
)


# --------------------------------------------------------------------------- #
# A minimal fake `sky` SDK that records calls.
# --------------------------------------------------------------------------- #

class _FakeTask:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.resources = None
        self.file_mounts = None

    def set_resources(self, resources):
        self.resources = resources

    def set_file_mounts(self, mounts):
        self.file_mounts = mounts


def make_fake_sky(*, status_records=None, launch_returns="req-launch-1"):
    sky = types.SimpleNamespace()
    sky.calls = []
    sky.tasks = []

    def _task(**kw):
        t = _FakeTask(**kw)
        sky.tasks.append(t)
        sky.calls.append(("Task", kw))
        return t
    sky.Task = _task
    sky.Resources = lambda **kw: (sky.calls.append(("Resources", kw)) or dict(kw))
    for name in ("AWS", "GCP", "Azure", "Kubernetes"):
        setattr(sky, name, (lambda n: (lambda: SimpleNamespace(name=n)))(name))

    def _launch(task, cluster_name=None, retry_until_up=None, **kwargs):
        # Mirror the real SkyPilot 0.7+ signature: detach_run was removed, so
        # passing it must blow up here the way the real SDK does (a TypeError) —
        # otherwise a stale kwarg slips through mocked tests and only fails on a
        # real launch. Guard explicitly since **kwargs would otherwise swallow it.
        assert "detach_run" not in kwargs, "sky.launch (0.7+) does not accept detach_run"
        sky.calls.append(("launch", cluster_name, retry_until_up, kwargs))
        return launch_returns
    sky.launch = _launch

    def _down(cluster_name):
        sky.calls.append(("down", cluster_name))
        return "req-down-1"
    sky.down = _down

    def _status(cluster_names=None):
        sky.calls.append(("status", cluster_names))
        return status_records if status_records is not None else []
    sky.status = _status

    def _stream_and_get(rid):
        sky.calls.append(("stream_and_get", rid))
        return None
    sky.stream_and_get = _stream_and_get

    return sky


@pytest.fixture
def spec(tmp_path):
    pdir = tmp_path / "proj-42"
    (pdir / "auto_research" / "state").mkdir(parents=True)
    return LaunchSpec(
        project_id="proj-42", mode="research", max_iterations=7,
        project_dir=pdir, log_dir=pdir / "logs",
        settings=SimpleNamespace(control_plane_url="https://cp.example", secret_key="s"),
        api_keys={"anthropic_api_key": "ak"},
        config={
            "orchestrator_compute_backend": {"type": "skypilot", "cloud": "aws"},
            "experiment_compute_backend": {"type": "local"},
            "max_days": 2,
        },
    )


def _patch(monkeypatch, sky, *, cp=("https://cp.example", "tok-xyz")):
    monkeypatch.setattr("ark.compute._sky.load_sky", lambda: sky)
    monkeypatch.setattr(jobs, "control_plane_transport", lambda pid, settings: cp)
    # Leave api_keys_to_env as the real implementation (shared mapping).


# --------------------------------------------------------------------------- #
# launch()
# --------------------------------------------------------------------------- #

def test_launch_builds_orchestrator_task_and_returns_handle(spec, monkeypatch):
    sky = make_fake_sky()
    _patch(monkeypatch, sky)

    handle = SkyPilotVmJobLauncher().launch(spec)

    assert handle == "skypilot:ark-orch-proj-42"
    kinds = [c[0] for c in sky.calls]
    assert "Task" in kinds and "launch" in kinds
    # launch rides out capacity errors (retry_until_up) and detaches from the
    # long-lived run via the async request model (no detach_run kwarg in 0.7+),
    # with a default autostop-DOWN crash safety-net (fires only after the queued
    # orchestrator job exits, so a live run is never reaped).
    launch_call = next(c for c in sky.calls if c[0] == "launch")
    assert launch_call == (
        "launch", "ark-orch-proj-42", True,
        {"idle_minutes_to_autostop": 60, "down": True},
    )
    # Async request id is blocked on.
    assert ("stream_and_get", "req-launch-1") in sky.calls

    task = sky.tasks[0]
    run = task.kwargs["run"]
    assert "python -m ark.orchestrator" in run
    assert "--control-plane-url https://cp.example" in run
    assert "--project-id proj-42" in run
    assert "--iterations 7" in run and "--max-days 2.0" in run
    # Remote paths use a fixed dirname, not the (user-derived) project id.
    assert "$HOME/ark_project" in run and "proj-42" not in run.replace("--project-id proj-42", "").replace("--project proj-42", "")
    # API keys ride as task envs (shared mapping → ANTHROPIC_API_KEY).
    assert task.kwargs["envs"]["ANTHROPIC_API_KEY"] == "ak"
    # Resources carried the configured cloud.
    assert task.resources["cloud"].name == "AWS"


def test_launch_autostop_can_be_disabled(spec, monkeypatch):
    # Unlike experiment clusters, the orchestrator cluster CAN be reaped by
    # cancel(), so its autostop safety-net is opt-out.
    sky = make_fake_sky()
    _patch(monkeypatch, sky)
    spec.config["orchestrator_compute_backend"]["idle_minutes_to_autostop"] = "off"
    SkyPilotVmJobLauncher().launch(spec)
    launch_call = next(c for c in sky.calls if c[0] == "launch")
    assert launch_call[3] == {}  # no autostop kwargs passed to sky.launch


def test_launch_autostop_down_false_stops_instead(spec, monkeypatch):
    sky = make_fake_sky()
    _patch(monkeypatch, sky)
    cc = spec.config["orchestrator_compute_backend"]
    cc["idle_minutes_to_autostop"] = 20
    cc["autostop_down"] = False
    SkyPilotVmJobLauncher().launch(spec)
    launch_call = next(c for c in sky.calls if c[0] == "launch")
    assert launch_call[3] == {"idle_minutes_to_autostop": 20, "down": False}


def test_launch_mounts_project_dir_and_token_secret(spec, monkeypatch):
    sky = make_fake_sky()
    _patch(monkeypatch, sky, cp=("https://cp.example", "tok-xyz"))

    # Snapshot the file_mounts + token file while it still exists (launch wipes it
    # in a finally), by spying on sky.launch — that's the only moment both are live.
    captured = {}
    real_launch = sky.launch
    def _spy_launch(task, **kw):
        captured["mounts"] = dict(task.file_mounts)
        tok = task.file_mounts.get("~/.ark_cp_token")
        captured["token_path"] = tok
        captured["token_exists_during_launch"] = bool(tok) and os.path.exists(tok)
        captured["token_contents"] = Path(tok).read_text() if tok else None
        return real_launch(task, **kw)
    monkeypatch.setattr(sky, "launch", _spy_launch)

    SkyPilotVmJobLauncher().launch(spec)

    # Project dir mounts to a FIXED remote dirname (not the project id → no
    # quoting/injection surface in the remote path).
    assert captured["mounts"]["~/ark_project"] == str(spec.project_dir)
    assert captured["token_exists_during_launch"] is True
    assert captured["token_contents"] == "tok-xyz"       # token uploaded as a secret
    # The local temp token file is wiped after launch (never lingers on disk).
    assert not os.path.exists(captured["token_path"])


def test_launch_without_control_plane_runs_blind(spec, monkeypatch):
    """No control-plane URL → no --control-plane-url args, no token mount, warn."""
    sky = make_fake_sky()
    _patch(monkeypatch, sky, cp=("", ""))

    SkyPilotVmJobLauncher().launch(spec)

    task = sky.tasks[0]
    assert "--control-plane-url" not in task.kwargs["run"]
    assert "~/.ark_cp_token" not in (task.file_mounts or {})


def test_launch_requires_config(tmp_path):
    spec = LaunchSpec(
        project_id="p", mode="research", max_iterations=1,
        project_dir=tmp_path, log_dir=tmp_path, settings=SimpleNamespace(),
        config=None,
    )
    with pytest.raises(RuntimeError, match="requires spec.config"):
        SkyPilotVmJobLauncher().launch(spec)


def test_launch_honours_explicit_cluster_name(spec, monkeypatch):
    spec.config["orchestrator_compute_backend"]["cluster_name"] = "my-cluster"
    sky = make_fake_sky()
    _patch(monkeypatch, sky)
    assert SkyPilotVmJobLauncher().launch(spec) == "skypilot:my-cluster"


def test_launch_warns_on_gemini_oauth_without_api_key(spec, monkeypatch, caplog):
    """OAuth-session creds aren't provisioned onto the cluster yet (PR4); surface
    it as a warning instead of a silent auth failure on the remote run."""
    import logging
    spec.api_keys = {"gemini_oauth_json": "{}"}  # OAuth only — no Gemini API key
    sky = make_fake_sky()
    _patch(monkeypatch, sky)
    with caplog.at_level(logging.WARNING, logger="website.dashboard"):
        SkyPilotVmJobLauncher().launch(spec)
    assert any("Gemini OAuth" in r.message for r in caplog.records)


def test_launch_no_oauth_warning_when_api_key_present(spec, monkeypatch, caplog):
    import logging
    spec.api_keys = {"gemini_oauth_json": "{}", "gemini": "gk"}  # has API key fallback
    sky = make_fake_sky()
    _patch(monkeypatch, sky)
    with caplog.at_level(logging.WARNING, logger="website.dashboard"):
        SkyPilotVmJobLauncher().launch(spec)
    assert not any("Gemini OAuth" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# poll() — sky status → normalized states
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status_name,expected", [
    ("UP", RUNNING),
    ("STOPPED", GONE),
    ("INIT", UNKNOWN),
])
def test_poll_normalizes_status(tmp_path, monkeypatch, status_name, expected):
    sky = make_fake_sky(status_records=[{"status": SimpleNamespace(name=status_name)}])
    monkeypatch.setattr("ark.compute._sky.load_sky", lambda: sky)
    res = SkyPilotVmJobLauncher().poll("skypilot:ark-orch-x", tmp_path)
    assert res.state == expected
    assert ("status", ["ark-orch-x"]) in sky.calls


def test_poll_no_cluster_is_gone(tmp_path, monkeypatch):
    """Cluster unknown to SkyPilot → GONE (crash-safety-net; DB authoritative)."""
    sky = make_fake_sky(status_records=[])
    monkeypatch.setattr("ark.compute._sky.load_sky", lambda: sky)
    assert SkyPilotVmJobLauncher().poll("skypilot:gone", tmp_path).state == GONE


def test_poll_sdk_error_is_unknown(tmp_path, monkeypatch):
    def _boom():
        raise RuntimeError("sky server down")
    monkeypatch.setattr("ark.compute._sky.load_sky", _boom)
    assert SkyPilotVmJobLauncher().poll("skypilot:x", tmp_path).state == UNKNOWN


def test_poll_swallowed_status_error_is_unknown_not_gone(tmp_path, monkeypatch):
    """A flaky async status-request (get() raises) is swallowed to None by
    resolve_request_value — poll must map that to UNKNOWN (retry), NOT GONE, so a
    transient blip can't trip the crash-safety-net and kill a live run."""
    sky = make_fake_sky()
    # status returns an async request id (a str, not a list); get() then raises,
    # so resolve_request_value returns None.
    monkeypatch.setattr(sky, "status", lambda cluster_names=None: "req-status-1")
    def _boom_get(rid):
        raise RuntimeError("status request failed")
    sky.get = _boom_get
    monkeypatch.setattr("ark.compute._sky.load_sky", lambda: sky)
    res = SkyPilotVmJobLauncher().poll("skypilot:ark-orch-x", tmp_path)
    assert res.state == UNKNOWN and res.raw == "status-unavailable"


# --------------------------------------------------------------------------- #
# cancel() — sky down + on_complete ordering
# --------------------------------------------------------------------------- #

def test_cancel_downs_cluster_then_runs_on_complete(tmp_path, monkeypatch):
    sky = make_fake_sky()
    monkeypatch.setattr("ark.compute._sky.load_sky", lambda: sky)
    import ark.launcher.skypilot as sp_mod
    threads = []
    real_thread = sp_mod.threading.Thread
    monkeypatch.setattr(sp_mod.threading, "Thread",
                        lambda *a, **k: threads.append(real_thread(*a, **k)) or threads[-1])

    order = []
    monkeypatch.setattr(sky, "down", lambda c: order.append(("down", c)) or "req-down-1")
    SkyPilotVmJobLauncher().cancel("skypilot:ark-orch-p", tmp_path,
                                   on_complete=lambda: order.append("cleanup"))
    threads[0].join(timeout=5)

    assert order == [("down", "ark-orch-p"), "cleanup"]  # cleanup strictly last


def test_cancel_runs_on_complete_even_if_down_fails(tmp_path, monkeypatch):
    sky = make_fake_sky()
    monkeypatch.setattr("ark.compute._sky.load_sky", lambda: sky)
    import ark.launcher.skypilot as sp_mod
    threads = []
    real_thread = sp_mod.threading.Thread
    monkeypatch.setattr(sp_mod.threading, "Thread",
                        lambda *a, **k: threads.append(real_thread(*a, **k)) or threads[-1])

    def _boom(c):
        raise RuntimeError("down failed")
    monkeypatch.setattr(sky, "down", _boom)
    ran = []
    SkyPilotVmJobLauncher().cancel("skypilot:c", tmp_path, on_complete=lambda: ran.append(1))
    threads[0].join(timeout=5)
    assert ran == [1]  # teardown failure must not strand the delete-endpoint cleanup
