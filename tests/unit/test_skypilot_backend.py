"""Layer-1 SkyPilotBackend lifecycle (folded Phases 5+6, ADR-0010, PR2).

Exercises the backend against a *mocked* ``sky`` SDK and mocked ``ssh``/``rsync``
subprocess calls — no real cloud, so these run in CI (unmarked). The real
multi-cloud provisioning path is covered by the PR5 acceptance run.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from ark.compute.skypilot import SkyPilotBackend, _REMOTE_WORKDIR


# --------------------------------------------------------------------------- #
# A minimal fake `sky` SDK that records calls.
# --------------------------------------------------------------------------- #

class _FakeTask:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.resources = None

    def set_resources(self, resources):
        self.resources = resources


class _FakeCloud:
    def __init__(self, name):
        self.name = name


def make_fake_sky(*, status_records=None, launch_returns="req-launch-1"):
    """Build a fake `sky` module. `launch_returns` drives the async-request path
    (a request id) vs the legacy synchronous tuple."""
    sky = types.SimpleNamespace()
    sky.calls = []

    sky.Task = lambda **kw: (sky.calls.append(("Task", kw)) or _FakeTask(**kw))
    sky.Resources = lambda **kw: (sky.calls.append(("Resources", kw)) or dict(kw))
    for name in ("AWS", "GCP", "Azure", "Kubernetes"):
        setattr(sky, name, (lambda n: (lambda: _FakeCloud(n)))(name))

    def _launch(task, cluster_name=None, retry_until_up=None):
        sky.calls.append(("launch", cluster_name, retry_until_up))
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
def project_dir(tmp_path):
    code = tmp_path / "proj"
    (code / "auto_research" / "state").mkdir(parents=True)
    return code


def _backend(project_dir, monkeypatch, sky, **cc):
    cfg = {"experiment_compute_backend": {"type": "skypilot", **cc}}
    b = SkyPilotBackend(cfg, "My Project", project_dir)
    monkeypatch.setattr("ark.compute.skypilot.load_sky", lambda: sky)
    return b


# --------------------------------------------------------------------------- #
# Construction / config
# --------------------------------------------------------------------------- #

def test_default_cluster_name_is_dns_safe(project_dir):
    b = SkyPilotBackend({"experiment_compute_backend": {"type": "skypilot"}},
                        "My Project!!", project_dir)
    assert b.cluster_name == "ark-my-project"


def test_explicit_cluster_name_wins(project_dir):
    b = SkyPilotBackend(
        {"experiment_compute_backend": {"type": "skypilot", "cluster_name": "custom-x"}},
        "proj", project_dir)
    assert b.cluster_name == "custom-x"


def test_resolve_cloud_maps_known_and_rejects_unknown(project_dir):
    sky = make_fake_sky()
    b = SkyPilotBackend(
        {"experiment_compute_backend": {"type": "skypilot", "cloud": "k8s"}},
        "proj", project_dir)
    assert b._resolve_cloud(sky).name == "Kubernetes"
    b.cloud = "nope"
    with pytest.raises(ValueError, match="Unknown SkyPilot cloud"):
        b._resolve_cloud(sky)


# --------------------------------------------------------------------------- #
# setup() — build + launch
# --------------------------------------------------------------------------- #

def test_setup_builds_task_and_launches(project_dir, monkeypatch):
    sky = make_fake_sky()
    b = _backend(project_dir, monkeypatch, sky,
                 cloud="aws", accelerators="A100:1", use_spot=True,
                 setup_commands=["pip install foo"])
    ctx = b.setup()

    assert ctx["cluster_name"] == "ark-my-project"
    assert ctx["work_dir"] == _REMOTE_WORKDIR
    kinds = [c[0] for c in sky.calls]
    assert "Task" in kinds and "launch" in kinds
    # Async request id from launch() is blocked on.
    assert ("stream_and_get", "req-launch-1") in sky.calls
    # Resources carried cloud/accelerators/spot.
    res = next(c[1] for c in sky.calls if c[0] == "Resources")
    assert res["accelerators"] == "A100:1" and res["use_spot"] is True
    assert res["cloud"].name == "AWS"
    # State persisted for crash recovery.
    assert b._state_file.exists()
    b.teardown()  # clear the atexit-registered handler's work (keeps exit quiet)


def test_setup_reuses_up_cluster_without_launch(project_dir, monkeypatch):
    sky = make_fake_sky(status_records=[{"status": "UP"}])
    b = _backend(project_dir, monkeypatch, sky, cloud="gcp")
    b.setup()
    assert "launch" not in [c[0] for c in sky.calls]
    assert b._launched is True
    b.teardown()


def test_setup_blocked_by_intervention(project_dir, monkeypatch):
    sky = make_fake_sky()
    b = _backend(project_dir, monkeypatch, sky, cloud="aws")
    b._intervention_check = lambda action, **kw: False
    with pytest.raises(RuntimeError, match="denied by intervention"):
        b.setup()
    assert "launch" not in [c[0] for c in sky.calls]


# --------------------------------------------------------------------------- #
# wait_for_completion() — marker polling over ssh
# --------------------------------------------------------------------------- #

def _stub_ssh(monkeypatch, outputs):
    seq = iter(outputs)

    def _run(cmd, capture_output=True, text=True, timeout=None, check=False):
        return types.SimpleNamespace(stdout=next(seq), returncode=0)
    monkeypatch.setattr(subprocess, "run", _run)


def test_wait_returns_true_on_marker(project_dir, monkeypatch):
    b = _backend(project_dir, monkeypatch, make_fake_sky())
    _stub_ssh(monkeypatch, ["DONE\n"])
    assert b.wait_for_completion(max_wait_hours=1) is True


def test_wait_returns_false_on_crash(project_dir, monkeypatch):
    b = _backend(project_dir, monkeypatch, make_fake_sky())
    monkeypatch.setattr("ark.compute.skypilot.time.sleep", lambda *_: None)
    # marker RUNNING → no processes → recheck CRASHED
    _stub_ssh(monkeypatch, ["RUNNING\n", "", "CRASHED\n"])
    assert b.wait_for_completion(max_wait_hours=1) is False


# --------------------------------------------------------------------------- #
# sync + teardown
# --------------------------------------------------------------------------- #

def test_sync_from_backend_rsyncs_over_alias(project_dir, monkeypatch):
    b = _backend(project_dir, monkeypatch, make_fake_sky())
    b._launched = True
    seen = {}

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(stdout="", returncode=0)
    monkeypatch.setattr(subprocess, "run", _run)

    ok = b.sync_from_backend("sky_workdir/results", str(project_dir / "results"))
    assert ok is True
    assert seen["cmd"][0] == "rsync"
    assert seen["cmd"][-1].endswith("/results/")
    assert f"{b.cluster_name}:sky_workdir/results/" in seen["cmd"]


def test_sync_noop_before_launch(project_dir, monkeypatch):
    b = _backend(project_dir, monkeypatch, make_fake_sky())
    assert b.sync_from_backend("x", str(project_dir)) is False
    assert b.sync_to_backend(str(project_dir), "x") is False


def test_teardown_calls_sky_down_and_clears_state(project_dir, monkeypatch):
    sky = make_fake_sky()
    b = _backend(project_dir, monkeypatch, sky)
    b.setup()
    assert b._state_file.exists()
    b.teardown()
    assert ("down", "ark-my-project") in sky.calls
    assert ("stream_and_get", "req-down-1") in sky.calls
    assert not b._state_file.exists()
    assert b._launched is False


def test_teardown_noop_when_never_launched(project_dir, monkeypatch):
    sky = make_fake_sky()
    b = _backend(project_dir, monkeypatch, sky)
    b.teardown()
    assert "down" not in [c[0] for c in sky.calls]


# --------------------------------------------------------------------------- #
# agent-facing instructions
# --------------------------------------------------------------------------- #

def test_agent_instructions_reference_ssh_alias_and_marker(project_dir, monkeypatch):
    b = _backend(project_dir, monkeypatch, make_fake_sky())
    text = b.get_agent_instructions()
    assert f"ssh {b.cluster_name}" in text
    assert b._MARKER_FILE in text
    assert _REMOTE_WORKDIR in text
