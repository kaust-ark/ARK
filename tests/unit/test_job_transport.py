"""Tests for launcher transport selection + SLURM template rendering (Phase 1, step 5).

Verifies that launchers pick the HTTP /v1 transport when a control-plane URL is
configured (minting a valid project-scoped token) and otherwise fall back to the
legacy --db-path path — and that the SLURM template renders the right invocation
for each, keeping the token in an env export (never on argv).
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2")
from jinja2 import Template  # noqa: E402

from website.dashboard.jobs import control_plane_transport, _SLURM_TEMPLATE  # noqa: E402
from website.dashboard.auth import verify_job_token  # noqa: E402


def _settings(url="", secret="s3cr3t"):
    return SimpleNamespace(control_plane_url=url, secret_key=secret)


# ── transport selection ─────────────────────────────────────────────────────────

def test_transport_legacy_when_unset():
    url, token = control_plane_transport("p1", _settings(url=""))
    assert url == "" and token == ""


def test_transport_http_when_set_mints_scoped_token():
    s = _settings(url="https://cp.example.com/v1")
    url, token = control_plane_transport("proj-123", s)
    assert url == "https://cp.example.com/v1"
    # token is valid and scoped to exactly this project
    assert verify_job_token(token, s.secret_key) == "proj-123"
    assert verify_job_token(token, "wrong-secret") is None


# ── SLURM template rendering ──────────────────────────────────────────────────────

def _render(cp_url, cp_token, db_path, project_dir="/tmp/p"):
    return Template(_SLURM_TEMPLATE.read_text()).render(
        project_id="p1", project_dir=project_dir, log_dir="/tmp/l", mode="paper",
        max_iterations=3, partition="", account="", gres="", cpus_per_task=4,
        conda_env="ark-base", api_keys={}, db_path=db_path,
        control_plane_url=shlex.quote(cp_url) if cp_url else "",
        control_plane_token=shlex.quote(cp_token) if cp_token else "",
        ark_code_root="/opt/ark",
    )


def test_slurm_template_legacy_uses_db_path():
    out = _render("", "", "/data/webapp.db")
    assert "--db-path /data/webapp.db" in out
    assert "--control-plane-url" not in out
    assert "ARK_CONTROL_PLANE_TOKEN" not in out


def test_slurm_template_http_uses_url_and_token_env():
    out = _render("https://cp.example.com/v1", "tok.en-value", "")
    assert "--control-plane-url https://cp.example.com/v1" in out
    assert "export ARK_CONTROL_PLANE_TOKEN=tok.en-value" in out
    assert "--db-path" not in out


@pytest.mark.parametrize("override", [False, True])
def test_slurm_worker_inherits_an_external_cache(tmp_path, override):
    project = tmp_path / "account" / "project"
    project.mkdir(parents=True)
    link = tmp_path / "linked-workspace"
    link.symlink_to(project, target_is_directory=True)
    # The batch script activates conda then calls python in the project. Record
    # the effective child environment without a scheduler or model provider.
    (tmp_path / ".bashrc").write_text(
        'conda() { :; }\npython() { printf "%s\\n" "$HOME" "$XDG_CACHE_HOME" > "$PWD/worker-env"; }\n'
    )
    env = {"HOME": str(tmp_path), "PATH": os.defpath}
    if override:
        env["XDG_CACHE_HOME"] = str(tmp_path / "configured cache")
    subprocess.run(["bash", "-c", _render("", "", "/tmp/mock-db", link)],
                   check=True, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert (project / "worker-env").read_text().splitlines() == [
        str(link), env.get("XDG_CACHE_HOME", str(project.parent / ".cache"))
    ]


@pytest.mark.parametrize("configured_account", ["ark", ""])
def test_slurm_submission_uses_the_webapp_python_for_dsh(tmp_path, monkeypatch, configured_account):
    from website.dashboard import jobs

    monkeypatch.setattr(sys, "executable", "/shared/ark prod/bin/python")
    monkeypatch.setattr("website.dashboard.db.resolve_db_path", lambda: "/data/webapp.db")
    monkeypatch.setattr(jobs.getpass, "getuser", lambda: "submitter")

    def run(args, **kwargs):
        if args[0] == "sacctmgr":
            assert configured_account == ""
            assert args == ["sacctmgr", "-nP", "show", "user", "where",
                            "name=submitter", "format=DefaultAccount"]
            return SimpleNamespace(returncode=0, stdout="submitter-account\n")
        assert args[0] == "sbatch"
        script = Path(args[1]).read_text()
        assert f"#SBATCH --account={configured_account or 'submitter-account'}" in script
        assert 'if [ -z "${ARK_DSH_PYTHON:-}" ]; then' in script
        assert "export ARK_DSH_PYTHON='/shared/ark prod/bin/python'" in script
        assert "python -m ark.orchestrator" in script
        return SimpleNamespace(returncode=0, stdout="Submitted batch job 123")

    monkeypatch.setattr(jobs, "_run", run)
    settings = SimpleNamespace(
        slurm_partition="cpu", slurm_account=configured_account, slurm_gres="",
        slurm_cpus_per_task=1, slurm_conda_env="ark-base",
    )
    assert jobs.submit_job("p1", "paper", 1, tmp_path, tmp_path / "logs", settings) == "123"


@pytest.mark.parametrize("owner_providers", [
    (),
    ("xai", "mistral", "groq", "moonshot", "cohere_chat"),
    ("anthropic", "deepseek", "openai", "openrouter", "gemini"),
])
def test_slurm_worker_only_receives_owner_credentials(tmp_path, monkeypatch, owner_providers):
    from website.dashboard import jobs
    from ark.llm_lite import provider_key_env

    providers = ("openrouter", "openai", "anthropic", "gemini", "deepseek",
                 "xai", "mistral", "groq", "moonshot", "cohere_chat")
    names = [provider_key_env(p) for p in providers] + ["GOOGLE_API_KEY", "LLM_API_KEY",
                                                       "ARK_GITHUB_PAT", "AWS_SESSION_TOKEN"]
    for name in names:
        monkeypatch.setenv(name, "operator-key")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.delenv("ARK_CONDA_ROOT", raising=False)
    monkeypatch.setattr("website.dashboard.db.resolve_db_path", lambda: "/tmp/test.db")
    # Host startup files can add keys again after sbatch has exported its environment.
    (tmp_path / ".bashrc").write_text(
        "".join(f"export {name}=login-key\n" for name in names)
        + 'conda() { export GOOGLE_API_KEY=activation-key; }\n'
        + 'python() { for n in ' + " ".join(names) + '; do '
          'if [[ -v $n ]]; then printf "%s=%s\\n" "$n" "${!n}"; fi; done > worker-keys; }\n'
    )
    # Accounts can store either a bare provider or a suffixed credential field.
    owner_keys = {p if i % 2 else p + "_api_key": f"owner {p}" for i, p in enumerate(owner_providers)}
    expected = {provider_key_env(p): f"owner {p}" for p in owner_providers}
    project = tmp_path / "project"
    project.mkdir()

    def run_batch(args, *, env):
        assert args[0] == "sbatch"
        assert all(name not in env for name in names)
        assert env["PATH"] == os.defpath
        subprocess.run(["bash", args[1]], cwd=tmp_path, env=env, check=True, capture_output=True)
        return SimpleNamespace(returncode=0, stdout="Submitted batch job 123")

    monkeypatch.setattr(jobs, "_run", run_batch)
    settings = SimpleNamespace(slurm_partition="cpu", slurm_account="fixture", slurm_gres="",
                               slurm_cpus_per_task=1, slurm_conda_env="ark-base")
    assert jobs.submit_job("p1", "paper", 1, project, project / "logs", settings, owner_keys) == "123"
    assert dict(row.split("=", 1) for row in (project / "worker-keys").read_text().splitlines()) == expected
