"""Phase 3 PR5 — the cloud orchestrator reports over the /v1 control-plane API
instead of the (removed) rsync-back bridge.

These tests pin the two behaviors that replace the bridge:
  * ``run_orchestrator`` wires the control-plane URL/project-id onto the remote
    launch command and carries the bearer token only in the RAM-disk ``.env``.
  * ``poll_orchestrator`` is a pure liveness probe (RUNNING/STOPPED/UNKNOWN) and
    never rsyncs the VM's disk back.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ark.compute.cloud.orchestrator import OrchestratorCloudBackend


def _backend(tmp_path):
    config = {"orchestrator_compute_backend": {
        "provider": "gcp", "ssh_user": "ark", "conda_env": "ark-base",
    }}
    b = OrchestratorCloudBackend(config, "proj-123", tmp_path)
    b._instance_id = "vm-1"
    b._instance_ip = "10.0.0.1"
    return b


def _mock_launch(monkeypatch, b):
    """Stub the SSH/rsync I/O of run_orchestrator; capture the launch command and
    the contents of the .env rsynced to /dev/shm."""
    captured = {"ssh": [], "env": ""}

    def fake_ssh_exec(command, timeout=600):
        captured["ssh"].append(command)
        return "12345"  # PID reads
    monkeypatch.setattr(b, "_ssh_exec", fake_ssh_exec)

    def fake_run(cmd, *a, **k):
        # The merged env is rsynced to a temp file → :/dev/shm/.env; read it before
        # run_orchestrator unlinks the temp source.
        if isinstance(cmd, list) and cmd and str(cmd[-1]).endswith(":/dev/shm/.env"):
            captured["env"] = Path(cmd[-2]).read_text()
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m
    monkeypatch.setattr("ark.compute.cloud.orchestrator.subprocess.run", fake_run)
    monkeypatch.setattr("ark.compute.cloud.orchestrator.time.sleep", lambda *a, **k: None)
    return captured


def _start_cmd(captured):
    hits = [c for c in captured["ssh"] if "python -m ark.orchestrator" in c]
    assert hits, "remote orchestrator start command was never issued"
    return hits[0]


def test_run_orchestrator_wires_control_plane(tmp_path, monkeypatch):
    b = _backend(tmp_path)
    captured = _mock_launch(monkeypatch, b)

    pid = b.run_orchestrator(
        control_plane_url="https://cp.example.com/v1",
        control_plane_token="tok-abc",
    )
    assert pid == "12345"

    start = _start_cmd(captured)
    # URL + project-id ride on argv (project_id required: no by-name resolution off-box).
    assert "--control-plane-url https://cp.example.com/v1" in start
    assert "--project-id proj-123" in start
    # The token is forwarded into the fresh `conda run env`, sourced from the .env...
    assert 'ARK_CONTROL_PLANE_TOKEN="${ARK_CONTROL_PLANE_TOKEN}"' in start
    # ...and never appears on argv — only in the RAM-disk .env.
    assert "tok-abc" not in start
    assert "ARK_CONTROL_PLANE_TOKEN=tok-abc" in captured["env"]


def test_run_orchestrator_without_control_plane_omits_flags(tmp_path, monkeypatch):
    b = _backend(tmp_path)
    captured = _mock_launch(monkeypatch, b)

    pid = b.run_orchestrator()  # no control plane configured
    assert pid == "12345"

    start = _start_cmd(captured)
    assert "--control-plane-url" not in start
    assert "--project-id" not in start
    # No token minted → no token line in the synced env.
    assert "ARK_CONTROL_PLANE_TOKEN=" not in captured["env"]


def _write_state(b, pid=999):
    b._state_file.parent.mkdir(parents=True, exist_ok=True)
    b._state_file.write_text(
        f"instance_id: vm-1\npublic_ip: 10.0.0.1\norchestrator_pid: {pid}\n"
    )


def test_poll_orchestrator_unknown_without_state(tmp_path):
    b = _backend(tmp_path)
    assert b.poll_orchestrator() == "UNKNOWN"


@pytest.mark.parametrize("returncode,expected", [(0, "RUNNING"), (1, "STOPPED")])
def test_poll_orchestrator_liveness(tmp_path, monkeypatch, returncode, expected):
    b = _backend(tmp_path)
    _write_state(b)
    b.sync_from_backend = MagicMock()  # must never be called post-PR5
    monkeypatch.setattr(
        "ark.compute.cloud.orchestrator.subprocess.run",
        lambda *a, **k: MagicMock(returncode=returncode),
    )
    assert b.poll_orchestrator() == expected
    b.sync_from_backend.assert_not_called()


def test_poll_orchestrator_probe_error_is_unknown(tmp_path, monkeypatch):
    """A transient SSH/network failure must not be mistaken for a dead process."""
    b = _backend(tmp_path)
    _write_state(b)

    def boom(*a, **k):
        raise OSError("ssh unreachable")
    monkeypatch.setattr("ark.compute.cloud.orchestrator.subprocess.run", boom)
    assert b.poll_orchestrator() == "UNKNOWN"
