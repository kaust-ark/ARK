"""Conda provisioning is bounded and visibly alive.

2026-08-03: a `conda create --clone` wedged on a sick NFS mount and hung for
FIVE DAYS with zero output — `subprocess.run(cmd)` had no timeout, so the
project sat at "running" indefinitely. No unbounded external call belongs in
the launch path, and multi-minute silence is its own defect.
"""

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from website.dashboard import jobs


# Bind the real Popen before any patching: a side_effect that calls
# subprocess.Popen would otherwise re-enter the mock and recurse.
_REAL_POPEN = subprocess.Popen


def _hang_cmd(tmp_path) -> list:
    """A stand-in 'conda' that just sleeps forever (the wedge we hit)."""
    script = tmp_path / "hangs.py"
    script.write_text("import time\nwhile True: time.sleep(1)\n")
    return [sys.executable, str(script)]


def _patched_popen(hang_cmd):
    """Run our hanging stand-in instead of whatever command was requested."""
    return lambda cmd, **kw: _REAL_POPEN(hang_cmd, **kw)


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


def test_wedged_clone_times_out_instead_of_hanging(project, tmp_path):
    hang_cmd = _hang_cmd(tmp_path)

    with patch.object(jobs, "find_conda_binary", return_value=hang_cmd[0]), \
         patch.object(jobs, "_accept_conda_tos", return_value=None), \
         patch.object(subprocess, "Popen", side_effect=_patched_popen(hang_cmd)):
        started = time.time()
        ok, msg = jobs.provision_project_env(project, "ark-base", timeout=3)
        elapsed = time.time() - started

    assert ok is False
    assert "timed out" in msg
    # It must actually give up near the deadline, not run to completion.
    assert elapsed < 30, f"took {elapsed:.0f}s — the timeout did not fire"


def test_timeout_wipes_the_partial_env(project, tmp_path):
    hang_cmd = _hang_cmd(tmp_path)
    target = jobs.project_env_prefix(project)
    target.mkdir(parents=True)
    (target / "half-copied.txt").write_text("x")

    with patch.object(jobs, "find_conda_binary", return_value=hang_cmd[0]), \
         patch.object(jobs, "_accept_conda_tos", return_value=None), \
         patch.object(subprocess, "Popen", side_effect=_patched_popen(hang_cmd)):
        ok, _ = jobs.provision_project_env(project, "ark-base", timeout=3)

    assert ok is False
    assert not target.exists(), "a half-cloned env must not be left behind"


def test_heartbeat_reports_progress_while_waiting(project, tmp_path):
    hang_cmd = _hang_cmd(tmp_path)
    beats = []

    with patch.object(jobs, "find_conda_binary", return_value=hang_cmd[0]), \
         patch.object(jobs, "_accept_conda_tos", return_value=None), \
         patch.object(jobs, "_PROVISION_HEARTBEAT_SECONDS", 2), \
         patch.object(subprocess, "Popen", side_effect=_patched_popen(hang_cmd)):
        jobs.provision_project_env(project, "ark-base", timeout=5,
                                   log_fn=beats.append)

    assert beats, "a multi-minute step must emit progress, not sit silent"
    assert "still provisioning" in beats[0]


def test_existing_env_short_circuits(project):
    (jobs.project_env_prefix(project) / "conda-meta").mkdir(parents=True)
    ok, msg = jobs.provision_project_env(project, "ark-base")
    assert ok and "already exists" in msg
