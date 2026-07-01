"""Tests for launcher transport selection + SLURM template rendering (Phase 1, step 5).

Verifies that launchers pick the HTTP /v1 transport when a control-plane URL is
configured (minting a valid project-scoped token) and otherwise fall back to the
legacy --db-path path — and that the SLURM template renders the right invocation
for each, keeping the token in an env export (never on argv).
"""

import shlex
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

def _render(cp_url, cp_token, db_path):
    return Template(_SLURM_TEMPLATE.read_text()).render(
        project_id="p1", project_dir="/tmp/p", log_dir="/tmp/l", mode="paper",
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
