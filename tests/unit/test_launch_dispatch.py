"""Phase 4 review fix — single launch dispatch (orchestrator_launcher_for).

Verifies the shared dispatch honours the configured backend (so the pending-queue
and template paths no longer silently run skypilot projects locally) and rejects
unknown backend types instead of running them locally."""

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

import website.dashboard.routes as routes  # noqa: E402
from ark.launcher import (  # noqa: E402
    LaunchSpec, LocalJobLauncher, SkyPilotVmJobLauncher, SlurmJobLauncher,
)


def _proj(backend):
    return SimpleNamespace(id="p1", orchestrator_compute_backend=backend,
                           user_id="u1",
                           mode="research", max_iterations=3)


def _spec(tmp_path, **kwargs):
    return LaunchSpec(project_id="p1", mode="research", max_iterations=3,
                      project_dir=tmp_path, log_dir=tmp_path / "logs",
                      settings=SimpleNamespace(), **kwargs)


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


@pytest.mark.parametrize("backend", ["slurm", "skypilot:gcp"])
@pytest.mark.parametrize("turn", [{"chat_message": "Summarize the results"},
                                  {"apply_instruction": "Shorten the abstract"}])
def test_project_turn_uses_the_local_helper(tmp_path, monkeypatch, backend, turn):
    def unexpected_probe():
        pytest.fail("A project turn tried to select a batch launcher")

    monkeypatch.setattr(routes, "slurm_available", unexpected_probe)
    spec = _spec(tmp_path, **turn)
    launcher = routes.orchestrator_launcher_for(_proj(backend), spec, None, spec.settings)
    assert isinstance(launcher, LocalJobLauncher)


def test_skypilot_selects_launcher_and_loads_config(tmp_path):
    """SkyPilot dispatch loads the project's config.yaml into the spec (the
    launcher reads its cluster/resources from there) — no separate probe."""
    (tmp_path / "config.yaml").write_text("orchestrator_compute_backend:\n  type: skypilot\n")
    spec = _spec(tmp_path)
    launcher = routes.orchestrator_launcher_for(_proj("skypilot:gcp"), spec, None, SimpleNamespace())
    assert isinstance(launcher, SkyPilotVmJobLauncher)
    assert spec.config is not None  # config.yaml loaded into the spec for the launcher
