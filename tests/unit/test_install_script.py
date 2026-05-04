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
    assert "--webapp" in out
    assert "--dry-run" in out
    assert "--no-research" in out


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
    'existing repo at $HOME/ARK' branch isn't accidentally exercised.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Drop conda from PATH so the script reports "would install miniforge".
    clean_path = ":".join(
        p for p in os.environ.get("PATH", "").split(":")
        if "conda" not in p and "miniforge" not in p and "anaconda" not in p
    )
    monkeypatch.setenv("PATH", clean_path)
    return tmp_path


def test_dry_run_no_conda_no_repo(script_path: Path, fake_home: Path) -> None:
    """--dry-run with no conda + no existing repo should plan a full install."""
    r = subprocess.run(
        ["bash", str(script_path), "--dry-run", "--no-base", "--no-research"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ},
    )
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    out = r.stdout

    # Plan banner
    assert "ARK installer" in out
    assert "dry-run (no changes)" in out

    # Should plan to install miniforge (no conda on the cleaned PATH)
    assert "Installing miniforge3" in out

    # Should plan to clone (no $HOME/ARK exists yet)
    assert "git clone" in out

    # Should plan the ark env + pip install
    assert "conda create -n ark" in out
    assert "pip install -e" in out

    # Onboarding hints must show how to set keys + run a project
    assert "ANTHROPIC_API_KEY" in out
    assert "ark new" in out
    assert "ark doctor" in out


def test_dry_run_webapp_flag(script_path: Path, fake_home: Path) -> None:
    """--webapp should plan to invoke `ark.cli webapp install`."""
    r = subprocess.run(
        ["bash", str(script_path), "--dry-run", "--webapp", "--no-base", "--no-research"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ},
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "webapp" in out.lower()
    assert "ark.cli webapp install" in out
    # Onboarding should advertise the dashboard URL
    assert "9527" in out


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
