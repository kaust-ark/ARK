"""Unit + light integration tests for ark.intervention.wrappers.

Covers shim generation, the PATH/env shaping, the watcher→gate bridge, and a
real end-to-end shim round trip (allow execs the real binary; deny blocks it).
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ark.intervention.wrappers import (
    install_wrappers, sandbox_env, ApprovalWatcher, WRAPPED_EXES,
)
from ark.intervention.gate import Gate, ApprovalChannel, ApprovalReply, NullChannel
from ark.intervention.policy import InterventionPolicy


class FakeChannel(ApprovalChannel):
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
    def request(self, req):
        self.requests.append(req)
        return self.replies.pop(0) if self.replies else None
    def notify(self, text):
        pass


def _gate(channel, tmp_path):
    return Gate(InterventionPolicy(autonomy="standard"), channel, state_dir=tmp_path)


# ── shim generation / env ──────────────────────────────────

def test_install_creates_executable_shims(tmp_path):
    shim_dir = install_wrappers(tmp_path / "shims")
    for exe in WRAPPED_EXES:
        p = shim_dir / exe
        assert p.exists() and os.access(p, os.X_OK)
    content = (shim_dir / "rm").read_text()
    assert "'rm'" in content                       # EXE baked in
    assert str((tmp_path / "shims").resolve()) in content  # SHIM_DIR baked in


def test_sandbox_env_prepends_path_and_sets_vars(tmp_path):
    env = sandbox_env({"PATH": "/usr/bin"}, "/intv", "/shims",
                      agent_type="experimenter", timeout_seconds=42)
    assert env["PATH"].startswith("/shims:")
    assert env["ARK_INTERVENTION_DIR"] == "/intv"
    assert env["ARK_INTERVENTION_TIMEOUT"] == "42"
    assert env["ARK_AGENT_TYPE"] == "experimenter"


# ── watcher → gate bridge ──────────────────────────────────

def test_watcher_allows_benign_command(tmp_path):
    idir = tmp_path / "intv"
    w = ApprovalWatcher(idir, _gate(FakeChannel([]), tmp_path), work_dirs=[tmp_path])
    w.start()
    try:
        rid = "req1"
        (idir / "requests").mkdir(parents=True, exist_ok=True)
        (idir / "requests" / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "exe": "python", "argv": ["python", "x.py"], "cwd": str(tmp_path)}))
        resp = _wait_for(idir / "responses" / f"{rid}.json")
        assert json.loads(resp.read_text())["verdict"] == "allow"
    finally:
        w.stop()


def test_watcher_denies_risky_command(tmp_path):
    idir = tmp_path / "intv"
    ch = FakeChannel([ApprovalReply("deny")])
    w = ApprovalWatcher(idir, _gate(ch, tmp_path), work_dirs=[tmp_path / "work"])
    w.start()
    try:
        rid = "req2"
        (idir / "requests").mkdir(parents=True, exist_ok=True)
        (idir / "requests" / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "exe": "rm", "argv": ["rm", "-rf", "/etc/shadow"], "cwd": str(tmp_path)}))
        resp = _wait_for(idir / "responses" / f"{rid}.json")
        assert json.loads(resp.read_text())["verdict"] == "deny"
        assert ch.requests  # the human was asked
    finally:
        w.stop()


# ── real end-to-end shim ───────────────────────────────────

def test_shim_failopen_without_intervention_dir(tmp_path):
    shim_dir = install_wrappers(tmp_path / "shims", exes=["true"])
    env = dict(os.environ)
    env.pop("ARK_INTERVENTION_DIR", None)
    r = subprocess.run([str(shim_dir / "true")], env=env, timeout=10)
    assert r.returncode == 0  # real `true` ran


def test_shim_allows_and_execs_real_binary(tmp_path):
    shim_dir = tmp_path / "shims"
    idir = tmp_path / "intv"
    install_wrappers(shim_dir, exes=["true"])
    w = ApprovalWatcher(idir, _gate(NullChannel(), tmp_path), work_dirs=[tmp_path])
    w.start()
    try:
        env = sandbox_env(os.environ.copy(), idir, shim_dir, timeout_seconds=12)
        r = subprocess.run([str(shim_dir / "true")], env=env, timeout=15)
        assert r.returncode == 0  # gate allowed → real `true` exec'd
    finally:
        w.stop()


def test_shim_blocks_on_deny(tmp_path):
    shim_dir = tmp_path / "shims"
    idir = tmp_path / "intv"
    install_wrappers(shim_dir, exes=["rm"])
    ch = FakeChannel([ApprovalReply("deny")])
    w = ApprovalWatcher(idir, _gate(ch, tmp_path), work_dirs=[tmp_path / "work"])
    w.start()
    try:
        env = sandbox_env(os.environ.copy(), idir, shim_dir, timeout_seconds=12)
        target = tmp_path / "outside_target"  # absolute, outside work dir → ASK → deny
        r = subprocess.run([str(shim_dir / "rm"), str(target)], env=env, timeout=15)
        assert r.returncode == 13  # blocked, real rm never ran
    finally:
        w.stop()


# ── secret requests ────────────────────────────────────────

class _SecretChan(NullChannel):
    def __init__(self, value):
        super().__init__()
        self._value = value
    def request_secret(self, name, purpose, *, origin="agent", timeout=1800):
        return self._value


def test_watcher_handles_secret_request(tmp_path):
    idir = tmp_path / "intv"
    sink = []
    g = Gate(InterventionPolicy(), _SecretChan("VALUE-FOR-HF"), state_dir=tmp_path)
    w = ApprovalWatcher(idir, g, work_dirs=[tmp_path], secret_sink=sink.append)
    w.start()
    try:
        (idir / "requests").mkdir(parents=True, exist_ok=True)
        rid = "sec1"
        (idir / "requests" / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "type": "secret", "name": "HF_TOKEN", "purpose": "download"}))
        resp = _wait_for(idir / "responses" / f"{rid}.json")
        assert json.loads(resp.read_text())["value"] == "VALUE-FOR-HF"
        assert sink == ["VALUE-FOR-HF"]   # registered for redaction
    finally:
        w.stop()


def test_secret_shim_prints_value_only(tmp_path):
    shim_dir = tmp_path / "shims"
    idir = tmp_path / "intv"
    install_wrappers(shim_dir)
    g = Gate(InterventionPolicy(), _SecretChan("secretval123"), state_dir=tmp_path)
    w = ApprovalWatcher(idir, g, work_dirs=[tmp_path])
    w.start()
    try:
        env = sandbox_env(os.environ.copy(), idir, shim_dir, timeout_seconds=12)
        r = subprocess.run([str(shim_dir / "ark-request-secret"), "HF_TOKEN", "why"],
                           env=env, capture_output=True, text=True, timeout=15)
        assert r.returncode == 0
        assert r.stdout == "secretval123"   # value ONLY (no newline/label → safe for $(...))
    finally:
        w.stop()


def _wait_for(path: Path, timeout: float = 6.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return path
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")
