"""Phase 4 — JobLauncher seam tests.

Covers launch/poll/cancel dispatch by config type + handle, the normalized poll
states, the SLURM auto-restart hook and cascade cancel, and the Layer-2 × Layer-1
config matrix. The SLURM launcher is a straight pass-through to ``submit_job`` —
the golden test here asserts the *submission path* (the exact args handed to
``submit_job``) is unchanged; ``test_job_transport.py`` guards the rendered
``sbatch`` script itself.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2")  # website.dashboard.jobs imports jinja2 at module load

from ark.launcher import (  # noqa: E402
    LaunchSpec, LocalJobLauncher, PollResult, RestartResult,
    SkyPilotVmJobLauncher, SlurmJobLauncher, launcher_from_handle, select_launcher,
    RUNNING, QUEUED, DONE, FAILED, STOPPED, GONE, UNKNOWN,
)
import website.dashboard.jobs as jobs  # noqa: E402


def _spec(tmp_path, **kw):
    defaults = dict(
        project_id="proj-1", mode="research", max_iterations=3,
        project_dir=tmp_path, log_dir=tmp_path / "logs",
        settings=SimpleNamespace(control_plane_url="", secret_key="s"),
    )
    defaults.update(kw)
    return LaunchSpec(**defaults)


# ── dispatch by persisted handle ─────────────────────────────────────────────

def test_launcher_from_handle():
    assert isinstance(launcher_from_handle("local:123"), LocalJobLauncher)
    assert isinstance(launcher_from_handle("skypilot:ark-orch-p1"), SkyPilotVmJobLauncher)
    assert isinstance(launcher_from_handle("98765"), SlurmJobLauncher)


# ── dispatch by config type (launch selection) ───────────────────────────────

@pytest.mark.parametrize("backend,slurm_ok,expected", [
    ("slurm", True, SlurmJobLauncher),
    ("slurm", False, LocalJobLauncher),     # slurm requested but unavailable → local
    ("local", True, LocalJobLauncher),
    ("local", False, LocalJobLauncher),
    (None, True, LocalJobLauncher),
])
def test_select_launcher(backend, slurm_ok, expected):
    # SkyPilot is dispatched by orchestrator_launcher_for, not select_launcher.
    assert isinstance(select_launcher(backend, slurm_ok=slurm_ok), expected)


@pytest.mark.parametrize("backend", ["skypilot", "skypilot:gcp", "bogus"])
def test_select_launcher_rejects_non_local_slurm(backend):
    # SkyPilot/unknown must raise, never silently fall back to a local launcher
    # (which would run the orchestrator on the control-plane host).
    with pytest.raises(ValueError, match="cannot dispatch"):
        select_launcher(backend, slurm_ok=True)


def test_initial_status():
    assert LocalJobLauncher.initial_status == RUNNING
    assert SlurmJobLauncher.initial_status == QUEUED
    # SkyPilot: launch() blocks until the cluster is UP + run started → RUNNING.
    assert SkyPilotVmJobLauncher.initial_status == RUNNING


# ── launch delegation (thin-adapter guarantee) ───────────────────────────────

def test_local_launch_delegates(tmp_path, monkeypatch):
    seen = {}
    def fake(*a, **k):
        seen["args"], seen["kw"] = a, k
        return "local:999"
    monkeypatch.setattr(jobs, "launch_local_job", fake)
    spec = _spec(tmp_path, api_keys={"k": "v"}, apply_instruction="fix",
                 apply_scope="edit", chat_message="hi")
    assert LocalJobLauncher().launch(spec) == "local:999"
    assert seen["args"] == ("proj-1", "research", 3, tmp_path, tmp_path / "logs", spec.settings)
    assert seen["kw"] == dict(api_keys={"k": "v"}, apply_instruction="fix",
                              apply_scope="edit", chat_message="hi")


def test_slurm_launch_delegates_unchanged(tmp_path, monkeypatch):
    """Golden: SlurmJobLauncher forwards the exact submit_job arg list, so the
    rendered sbatch script and submission path are identical to pre-Phase-4."""
    seen = {}
    def fake(*a, **k):
        seen["args"], seen["kw"] = a, k
        return "424242"
    monkeypatch.setattr(jobs, "submit_job", fake)
    spec = _spec(tmp_path, api_keys={"anthropic": "x"})
    assert SlurmJobLauncher().launch(spec) == "424242"
    assert seen["args"] == ("proj-1", "research", 3, tmp_path, tmp_path / "logs", spec.settings)
    assert seen["kw"] == dict(api_keys={"anthropic": "x"})


# ── poll normalization ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("RUNNING", RUNNING), ("COMPLETED", DONE), ("FAILED", FAILED),
])
def test_local_poll_normalizes(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setattr(jobs, "poll_local_job", lambda pid, log_dir: raw)
    res = LocalJobLauncher().poll("local:321", tmp_path)
    assert res == PollResult(expected, raw)


def test_local_poll_bad_handle(tmp_path):
    assert LocalJobLauncher().poll("local:notapid", tmp_path).state == UNKNOWN


@pytest.mark.parametrize("raw,expected", [
    ("PENDING", QUEUED), ("RUNNING", RUNNING), ("COMPLETED", DONE),
    ("CANCELLED", STOPPED), ("FAILED", FAILED),
    ("UNKNOWN", FAILED),  # pre-Phase-4: an unresolvable squeue/sacct maps to failed
])
def test_slurm_poll_normalizes(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setattr(jobs, "poll_job", lambda h: raw)
    assert SlurmJobLauncher().poll("777", tmp_path).state == expected


# ── auto-restart hook (SLURM only) ───────────────────────────────────────────

def test_local_and_skypilot_never_restart(tmp_path):
    assert LocalJobLauncher().maybe_restart("local:1", _spec(tmp_path)) is None
    assert SkyPilotVmJobLauncher().maybe_restart("skypilot:c", _spec(tmp_path)) is None


def test_slurm_restart_under_limit(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "slurm_1.out").write_text("")
    (logs / "slurm_2.out").write_text("")
    monkeypatch.setattr(jobs, "submit_job", lambda *a, **k: "555")
    r = SlurmJobLauncher().maybe_restart("111", _spec(tmp_path))
    assert isinstance(r, RestartResult) and r.handle == "555" and r.attempt == 2


def test_slurm_restart_at_limit(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    for i in range(5):
        (logs / f"slurm_{i}.out").write_text("")
    monkeypatch.setattr(jobs, "submit_job", lambda *a, **k: pytest.fail("should not resubmit"))
    assert SlurmJobLauncher().maybe_restart("111", _spec(tmp_path)) is None


# ── cancel dispatch ──────────────────────────────────────────────────────────

def test_local_cancel(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(jobs, "cancel_local_job", lambda pid: calls.append(pid))
    LocalJobLauncher().cancel("local:246", tmp_path)
    assert calls == [246]


def test_local_cancel_runs_on_complete_synchronously(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "cancel_local_job", lambda pid: None)
    ran = []
    LocalJobLauncher().cancel("local:246", tmp_path, on_complete=lambda: ran.append(1))
    assert ran == [1]  # sync: rmtree-style cleanup happens before cancel() returns


def test_slurm_cancel_cascades(monkeypatch, tmp_path):
    calls = {}
    def fake_cancel_job(h):
        calls["job"] = h
    def fake_cascade(d):
        calls["cascade"] = d
        return ["s1", "s2"]
    monkeypatch.setattr(jobs, "cancel_job", fake_cancel_job)
    monkeypatch.setattr(jobs, "cancel_project_sub_jobs", fake_cascade)
    SlurmJobLauncher().cancel("888", tmp_path)
    assert calls == {"job": "888", "cascade": tmp_path}


# ── stuck-watchdog log source ────────────────────────────────────────────────

def test_latest_log_mtime(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "local_1.out").write_text("x")
    (logs / "slurm_1.out").write_text("y")
    assert LocalJobLauncher().latest_log_mtime(tmp_path) is not None
    assert SlurmJobLauncher().latest_log_mtime(tmp_path) is not None
    # skypilot has no local log to watch (remote run reports over /v1)
    assert SkyPilotVmJobLauncher().latest_log_mtime(tmp_path) is None


def test_latest_log_mtime_empty(tmp_path):
    (tmp_path / "logs").mkdir()
    assert LocalJobLauncher().latest_log_mtime(tmp_path) is None


# ── read_error: local sets error_message (even ""), slurm/cloud leave it alone ──

def test_local_read_error_returns_tail(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "local_1.out").write_text("boot\n\nTraceback\nValueError: boom\n")
    err = LocalJobLauncher().read_error(tmp_path)
    assert err is not None and "ValueError: boom" in err


def test_local_read_error_empty_string_when_no_log(tmp_path):
    (tmp_path / "logs").mkdir()
    # Empty string (not None) so a local failure overwrites any stale message.
    assert LocalJobLauncher().read_error(tmp_path) == ""


def test_slurm_and_skypilot_read_error_none(tmp_path):
    # None → poller leaves the existing error_message untouched (pre-Phase-4 parity).
    assert SlurmJobLauncher().read_error(tmp_path) is None
    assert SkyPilotVmJobLauncher().read_error(tmp_path) is None


# ── shared credential mapping (single source of truth for both launch paths) ────

def test_api_keys_to_env_shared_subset():
    env = jobs.api_keys_to_env({
        "claude_oauth_token": "tok", "anthropic_api_key": "ak", "gemini": "g",
        "openrouter": "or", "aws_access_key_id": "id", "azure_foo": "z",
        # local-only extras must NOT appear in the shared mapping:
        "github_pat": "ghp", "github_org": "org",
        "gcp_service_account_json": "{}", "gcp_project": "proj",
        # SkyPilot launcher-routing metadata — NOT run creds, must be skipped
        # (else the long-tail branch mis-injects AWS_ACCOUNT_ID_API_KEY etc.).
        "aws_account_id": "123456789012", "aws_region": "us-east-1",
    })
    assert env == {
        "CLAUDE_CODE_OAUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "ak",
        "GEMINI_API_KEY": "g", "OPENROUTER_API_KEY": "or",
        "AWS_ACCESS_KEY_ID": "id", "AZURE_FOO": "z",
    }
