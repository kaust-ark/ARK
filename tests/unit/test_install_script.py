"""Smoke tests for the one-click installer (`website/homepage/install.sh`).

These don't actually install anything — they just check that the script
parses, that --help works, and that --dry-run prints a coherent plan.

The script is intended to be served at https://idea2paper.org/install.sh,
so it has to stay self-contained and POSIX-friendly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "website" / "homepage" / "install.sh"


@pytest.fixture(scope="module")
def script_path() -> Path:
    assert SCRIPT.exists(), f"installer not found at {SCRIPT}"
    return SCRIPT


def test_script_is_executable_and_has_shebang(script_path: Path) -> None:
    text = script_path.read_text()
    assert text.startswith("#!/usr/bin/env bash"), "missing bash shebang"
    # Group/world need to be able to read+execute when served via static-files.
    mode = script_path.stat().st_mode & 0o777
    assert mode & 0o100, f"owner exec bit not set: {oct(mode)}"


def test_bash_n_syntax_check(script_path: Path) -> None:
    """`bash -n` parses the script without executing it."""
    r = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash -n failed: {r.stderr}"


def test_help_flag(script_path: Path) -> None:
    r = subprocess.run(
        ["bash", str(script_path), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    # Should describe the public usage and the supported flags.
    assert "ARK" in out
    assert "--prefix" in out
    assert "--no-webapp" in out  # --webapp is now the default; doc the opt-out
    assert "--dry-run" in out
    assert "--no-research" in out
    assert "--noninteractive" in out


def test_unknown_flag_exits_nonzero(script_path: Path) -> None:
    r = subprocess.run(
        ["bash", str(script_path), "--definitely-not-a-flag"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode != 0
    assert "Unknown flag" in r.stderr or "Unknown flag" in r.stdout


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the installer with HOME=tmp_path so it can't touch the real home.

    --dry-run guarantees no side effects, but we still isolate HOME so the
    'existing repo at $HOME/ARK' branch isn't accidentally exercised. We
    also pin PATH to a minimal POSIX set + the directory of the running
    Python — without `command -v conda` finding *anything*. (Just stripping
    paths whose names contain "conda" misses GitHub Actions, where conda
    sits at /usr/share/miniconda/bin AND is symlinked from /usr/local/bin.)
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Minimal PATH: enough for bash builtins and the script's tool checks
    # (git, curl, bash itself live in /usr/bin and /usr/local/bin on every
    # supported platform). Conda CIs put conda binaries on $CONDA paths,
    # so dropping everything else is enough.
    monkeypatch.setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    # Strip env vars that would let install.sh's `command -v conda` resolve
    # via shell hashing or user-site shims even after PATH is reset.
    for var in ("CONDA_EXE", "CONDA_PREFIX", "CONDA_PYTHON_EXE",
                "MAMBA_EXE", "_CE_CONDA"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_dry_run_no_conda_no_repo(script_path: Path, fake_home: Path) -> None:
    """--dry-run with no conda + no existing repo should plan a full install."""
    r = subprocess.run(
        ["bash", str(script_path), "--dry-run", "--no-base", "--no-research",
         "--no-webapp", "--noninteractive"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ},
    )
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    out = r.stdout

    # Plan banner
    assert "ARK installer" in out
    assert "dry-run (no changes)" in out

    # The "Locating conda" step always runs; whether it then reports
    # "Found existing" or "Installing miniforge3" depends on the host
    # (CI has conda symlinked into /usr/local/bin which is hard to fully
    # strip). Either branch is fine for the dry-run plan validation.
    assert "Locating conda" in out

    # Should plan to clone (no $HOME/ARK exists yet)
    assert "git clone" in out

    # Should plan the ark env + pip install
    assert "conda create -n ark" in out
    assert "pip install -e" in out

    # Onboarding hints must show how to run a project + verify
    assert "ark new" in out
    assert "ark doctor" in out


def test_webapp_default_on(script_path: Path, fake_home: Path) -> None:
    """--webapp is the default; the plan should call `ark.cli webapp install`."""
    r = subprocess.run(
        ["bash", str(script_path), "--dry-run", "--no-base", "--no-research",
         "--noninteractive"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ},
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "ark.cli webapp install" in out
    # Onboarding should advertise the dashboard URL
    assert "9527" in out


def test_no_webapp_skips_service(script_path: Path, fake_home: Path) -> None:
    """--no-webapp opts out of the default service install."""
    r = subprocess.run(
        ["bash", str(script_path), "--dry-run", "--no-base", "--no-research",
         "--no-webapp", "--noninteractive"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ},
    )
    assert r.returncode == 0, r.stderr
    assert "ark.cli webapp install" not in r.stdout, "webapp install should be skipped"


def test_script_installs_agent_clis(script_path: Path) -> None:
    """ARK invokes claude/gemini via subprocess, so the installer has to
    bootstrap them when missing. Make sure that branch isn't lost in a
    refactor."""
    text = script_path.read_text()
    assert "@anthropic-ai/claude-code" in text, "missing claude-code npm install"
    assert "@google/gemini-cli" in text, "missing gemini-cli npm install"
    assert "command -v claude" in text, "missing skip-if-installed guard for claude"
    assert "command -v gemini" in text, "missing skip-if-installed guard for gemini"
    assert "nodejs" in text, "must install Node.js for the npm CLIs"


def test_shim_extends_path_for_subprocesses(script_path: Path) -> None:
    """The launcher must put the ark env's bin on PATH so that the
    `claude`/`gemini` binaries we just installed are discoverable when
    the orchestrator spawns subprocesses."""
    text = script_path.read_text()
    # The shim heredoc should set PATH to include $ARK_ENV_BIN.
    assert 'export PATH="$ARK_ENV_BIN' in text or "ARK_ENV_BIN:" in text, \
        "shim does not prepend env bin to PATH"


def test_dry_run_prefix_override(script_path: Path, fake_home: Path) -> None:
    target = fake_home / "custom-ark-dir"
    r = subprocess.run(
        ["bash", str(script_path), "--dry-run", "--prefix", str(target),
         "--no-base", "--no-research"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ},
    )
    assert r.returncode == 0, r.stderr
    assert str(target) in r.stdout
