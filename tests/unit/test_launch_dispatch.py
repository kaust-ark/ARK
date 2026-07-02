"""Phase 4 review fix — single launch dispatch (orchestrator_launcher_for).

Verifies the shared dispatch honours the configured backend (so the pending-queue
and template paths no longer silently run cloud projects locally), falls back to
local when cloud is unconfigured, and rejects unknown backend types instead of
running them locally."""

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

import website.dashboard.routes as routes  # noqa: E402
from ark.launcher import (  # noqa: E402
    CloudVmJobLauncher, LaunchSpec, LocalJobLauncher, SlurmJobLauncher,
)


def _proj(backend):
    return SimpleNamespace(id="p1", orchestrator_compute_backend=backend,
                           cloud_overrides=None, user_id="u1",
                           mode="research", max_iterations=3)


def _spec(tmp_path):
    return LaunchSpec(project_id="p1", mode="research", max_iterations=3,
                      project_dir=tmp_path, log_dir=tmp_path / "logs",
                      settings=SimpleNamespace())


def test_unknown_backend_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown orchestrator backend"):
        routes.orchestrator_launcher_for(_proj("k8s"), _spec(tmp_path), None, SimpleNamespace())


def test_slurm_and_local_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "slurm_available", lambda: True)
    assert isinstance(
        routes.orchestrator_launcher_for(_proj("slurm"), _spec(tmp_path), None, SimpleNamespace()),
        SlurmJobLauncher)
    monkeypatch.setattr(routes, "slurm_available", lambda: False)
    assert isinstance(
        routes.orchestrator_launcher_for(_proj("slurm"), _spec(tmp_path), None, SimpleNamespace()),
        LocalJobLauncher)
    assert isinstance(
        routes.orchestrator_launcher_for(_proj("local"), _spec(tmp_path), None, SimpleNamespace()),
        LocalJobLauncher)


def test_cloud_unconfigured_falls_back_to_local(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "get_user", lambda s, uid: None)
    monkeypatch.setattr(routes, "_build_cloud_config", lambda *a, **k: None)
    assert isinstance(
        routes.orchestrator_launcher_for(_proj("cloud"), _spec(tmp_path), None, SimpleNamespace()),
        LocalJobLauncher)


def test_cloud_configured_selects_cloud_and_loads_config(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "get_user", lambda s, uid: object())
    monkeypatch.setattr(routes, "_build_cloud_config", lambda *a, **k: {"provider": "gcp"})
    (tmp_path / "config.yaml").write_text("orchestrator_compute_backend:\n  type: cloud\n")
    spec = _spec(tmp_path)
    launcher = routes.orchestrator_launcher_for(_proj("cloud:gcp"), spec, None, SimpleNamespace())
    assert isinstance(launcher, CloudVmJobLauncher)
    assert spec.config is not None  # config.yaml loaded into the spec for the launcher
