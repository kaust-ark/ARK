"""Tests for the `ark doctor` self-host diagnostic command.

The command shells out to `systemctl`, `conda`, etc. We don't mock those —
just invoke `ark doctor` end-to-end and assert the expected sections appear
and the exit code matches the WARN/FAIL count.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _run_doctor(extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "ark.cli", "doctor"],
        capture_output=True, text=True, timeout=30, env=env, cwd=REPO_ROOT,
    )


def test_doctor_runs_and_emits_known_sections() -> None:
    r = _run_doctor()
    assert r.returncode in (0, 1), f"unexpected exit {r.returncode}: {r.stderr}"
    out = _strip_ansi(r.stdout)
    # Header
    assert "ARK doctor" in out
    # Each check should appear by its label.
    expected_labels = [
        "python",
        "ark package importable",
        "conda found",
        "agent CLI",
        "API key",
        "LaTeX",
        "webapp service",
    ]
    for label in expected_labels:
        assert label in out, f"missing doctor section: {label!r}\n--- output ---\n{out}"


def test_doctor_exit_code_matches_failures() -> None:
    """Exit code is 1 iff at least one check failed."""
    r = _run_doctor()
    out = _strip_ansi(r.stdout)
    has_fail = "[fail]" in out
    if has_fail:
        assert r.returncode == 1, "fail present but exit code != 1"
    else:
        assert r.returncode == 0, f"no [fail] but exit code {r.returncode}: {r.stderr}"


def test_doctor_warns_on_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no API keys in the env, the API-key check must be a WARN (not PASS)."""
    env = {
        # Force-clear any inherited keys.
        "ANTHROPIC_API_KEY": "",
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
    }
    r = _run_doctor(extra_env=env)
    out = _strip_ansi(r.stdout)
    # Find the API key line and confirm it's tagged warn.
    for line in out.splitlines():
        if "API key" in line:
            assert "warn" in line, f"expected WARN on cleared keys, got: {line!r}"
            return
    pytest.fail(f"API key line not found in doctor output:\n{out}")


def test_webapp_login_prints_magic_link(tmp_path) -> None:
    """`ark webapp login me@example.com` should print a clickable magic link
    without sending an email — the self-host SMTP-free login path. Settings
    merges file values with os.environ (env wins), so we pin BASE_URL +
    SECRET_KEY via env to make the test independent of the host's webapp.env.
    """
    env = os.environ.copy()
    env["BASE_URL"] = "http://localhost:9527"
    env["SECRET_KEY"] = "test-secret-fixture-please-do-not-reuse"
    env["DB_PATH"] = str(tmp_path / "webapp.db")

    r = subprocess.run(
        [sys.executable, "-m", "ark.cli", "webapp", "login", "me@example.com"],
        capture_output=True, text=True, timeout=15, env=env, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stderr or r.stdout
    out = _strip_ansi(r.stdout)
    assert "me@example.com" in out
    # The URL we print must include /auth/verify?token= and the BASE_URL.
    assert "http://localhost:9527" in out
    assert "/auth/verify?token=" in out


def test_webapp_login_rejects_invalid_email() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "ark.cli", "webapp", "login", "not-an-email"],
        capture_output=True, text=True, timeout=10, cwd=REPO_ROOT,
    )
    assert r.returncode != 0


def test_doctor_passes_on_present_api_key() -> None:
    """With at least one API key set, the API-key check should PASS."""
    env = {"ANTHROPIC_API_KEY": "sk-test-doctor-fake-key"}
    r = _run_doctor(extra_env=env)
    out = _strip_ansi(r.stdout)
    for line in out.splitlines():
        if "API key" in line:
            assert "ok" in line, f"expected PASS with key set, got: {line!r}"
            assert "ANTHROPIC_API_KEY" in line
            return
    pytest.fail(f"API key line not found in doctor output:\n{out}")
