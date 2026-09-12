"""A failed clone's debris is set ASIDE atomically, never deleted in place.

Three projects in three days (b5fedacd 09-10, 5c0e44f1 09-11, f028f3ba 09-12)
died at conda provisioning with the NFS "Could not remove or rename ... free
file handles" race — and every one of them died on BOTH attempts. The retry
never worked because `shutil.rmtree(ignore_errors=True)` cannot remove files
that still have handles open over NFS: the server silly-renames them and the
unlink is silently skipped, so the retry cloned into a directory that was not
empty and hit the same race replacing the previous attempt's leftovers.

`os.rename` of the whole directory is atomic (NFS included) and needs no
unlink of anything inside. The debris is simply no longer at the path conda
writes to. The clone must still target the canonical prefix — conda bakes the
prefix into shebangs and activation scripts — so only FAILED debris is moved.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from website.dashboard import jobs

_REAL_POPEN = subprocess.Popen


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


def _script(tmp_path, name: str, body: str) -> list:
    f = tmp_path / f"{name}.py"
    f.write_text(body)
    return [sys.executable, str(f)]


class TestSetAsideIsAtomicNotADelete:
    def test_partial_env_is_renamed_aside_with_its_contents_intact(self, project):
        target = jobs.project_env_prefix(project)
        target.mkdir(parents=True)
        (target / "junk").write_text("leftover from a dead clone")

        aside = jobs._set_aside_partial_env(target)

        assert not target.exists(), "target must be EMPTY for the next attempt"
        assert aside is not None and aside.is_dir()
        assert aside.name.startswith(f"{target.name}.stale-")
        assert (aside / "junk").read_text() == "leftover from a dead clone"

    def test_missing_target_is_a_no_op(self, project):
        target = jobs.project_env_prefix(project)
        assert not target.exists()
        assert jobs._set_aside_partial_env(target) is None

    def test_sweep_reclaims_stale_dirs(self, project):
        target = jobs.project_env_prefix(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        stale = target.with_name(f"{target.name}.stale-deadbeef")
        (stale / "x").mkdir(parents=True)
        assert jobs._sweep_stale_envs(target) == 1
        assert not stale.exists()


class TestRetryAlwaysGetsAnEmptyTarget:
    def test_retry_never_clones_into_a_polluted_dir(self, project, tmp_path):
        """The exact shape of all three September failures, made impossible."""
        target = jobs.project_env_prefix(project)
        # Attempt 1: conda leaves a half-built env behind and dies (rc=1).
        leaves_debris = _script(tmp_path, "leaves_debris", (
            "import pathlib, sys\n"
            f"p = pathlib.Path({str(target)!r})\n"
            "p.mkdir(parents=True, exist_ok=True)\n"
            "(p / 'junk').write_text('half-built')\n"
            "sys.exit(1)\n"))
        succeeds = _script(tmp_path, "succeeds", "import sys\nsys.exit(0)\n")

        seen_at_attempt2 = {}

        def popen(cmd, **kw):
            n = popen.calls = getattr(popen, "calls", 0) + 1
            if n == 2:
                # What does the retry actually see when it starts?
                seen_at_attempt2["target_exists"] = target.exists()
                seen_at_attempt2["stale_dirs"] = sorted(
                    p.name for p in target.parent.glob(f"{target.name}.stale-*"))
            return _REAL_POPEN(leaves_debris if n == 1 else succeeds, **kw)

        # Probed once up front (nothing there), then after the good clone.
        ready = iter([False, True])
        with patch.object(jobs, "find_conda_binary", return_value=succeeds[0]), \
             patch.object(jobs, "_accept_conda_tos", return_value=None), \
             patch.object(jobs, "_PROVISION_RETRY_PAUSE_SECONDS", 0), \
             patch.object(jobs, "project_env_ready", side_effect=lambda *_: next(ready)), \
             patch.object(subprocess, "Popen", side_effect=popen):
            ok, msg = jobs.provision_project_env(project, "ark-base", timeout=30)

        assert ok is True, msg
        assert popen.calls == 2
        assert seen_at_attempt2["target_exists"] is False, (
            "the retry started on a NON-empty target — attempt 1's debris was "
            "left in place instead of being set aside")
        assert len(seen_at_attempt2["stale_dirs"]) == 1, seen_at_attempt2
        # After success the debris is reclaimed, best-effort.
        assert not list(target.parent.glob(f"{target.name}.stale-*"))

    def test_clone_always_targets_the_canonical_prefix(self, project, tmp_path):
        """Clone-to-temp-then-rename would bake the wrong prefix into the env."""
        target = jobs.project_env_prefix(project)
        succeeds = _script(tmp_path, "ok", "import sys\nsys.exit(0)\n")
        prefixes = []

        def popen(cmd, **kw):
            if "--prefix" in cmd:
                prefixes.append(cmd[cmd.index("--prefix") + 1])
            return _REAL_POPEN(succeeds, **kw)

        ready = iter([False, True])
        with patch.object(jobs, "find_conda_binary", return_value=succeeds[0]), \
             patch.object(jobs, "_accept_conda_tos", return_value=None), \
             patch.object(jobs, "project_env_ready", side_effect=lambda *_: next(ready)), \
             patch.object(subprocess, "Popen", side_effect=popen):
            ok, _ = jobs.provision_project_env(project, "ark-base", timeout=30)

        assert ok is True
        assert prefixes == [str(target)], prefixes
        assert ".stale" not in prefixes[0] and ".tmp" not in prefixes[0]

    def test_three_attempts_by_default(self):
        """With a truly empty target each time, retries are worth having."""
        assert jobs._PROVISION_ATTEMPTS >= 3
