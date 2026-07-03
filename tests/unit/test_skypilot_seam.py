"""PR1 scaffolding for the folded Phases 5+6 SkyPilot work (ADR-0010).

`type: skypilot` is a *reserved and validated* compute type. The Layer-1
experiment backend (PR2) is a `ComputeBackend`; the Layer-2 orchestrator (PR3)
is a `JobLauncher` (`SkyPilotVmJobLauncher`), built by the webapp's
`orchestrator_launcher_for`, not this factory. This locks the scaffolding contract:

- the compute factory builds a `SkyPilotBackend` for the experiment path, and
  still rejects the orchestrator path loudly (NotImplementedError — the
  orchestrator is a launcher, not a ComputeBackend) — never a silent wrong backend;
- the lazy SDK seam imports `sky` only on demand and raises a helpful install hint
  when the optional `skypilot` extra is absent, so `import ark.compute` never
  needs SkyPilot installed.
"""

import builtins

import pytest

from ark.compute import from_config
from ark.compute._sky import load_sky


def _skypilot_cfg(is_orchestrator):
    key = "orchestrator_compute_backend" if is_orchestrator else "experiment_compute_backend"
    return {key: {"type": "skypilot"}}


def test_factory_builds_experiment_backend(tmp_path):
    """PR2: the experiment (Layer-1) path constructs a SkyPilotBackend without
    importing the `sky` SDK (construction is lazy — only launch needs it)."""
    from ark.compute.skypilot import SkyPilotBackend

    backend = from_config(
        _skypilot_cfg(is_orchestrator=False),
        project_name="demo",
        code_dir=str(tmp_path),
        is_orchestrator=False,
    )
    assert isinstance(backend, SkyPilotBackend)


def test_factory_rejects_skypilot_orchestrator_loudly(tmp_path):
    """The Layer-2 orchestrator is a JobLauncher (SkyPilotVmJobLauncher), never
    built through the ComputeBackend factory — this path must fail loudly."""
    with pytest.raises(NotImplementedError, match="skypilot"):
        from_config(
            _skypilot_cfg(is_orchestrator=True),
            project_name="demo",
            code_dir=str(tmp_path),
            is_orchestrator=True,
        )


def test_load_sky_returns_module_when_importable():
    """When something named `sky` is importable, load_sky hands it back as-is."""
    sky = pytest.importorskip("sky", reason="SkyPilot extra not installed")
    assert load_sky() is sky


def test_load_sky_raises_install_hint_when_missing(monkeypatch):
    """With the extra absent, the caller gets an actionable RuntimeError, not a
    bare ImportError."""
    real_import = builtins.__import__

    def _no_sky(name, *args, **kwargs):
        if name == "sky" or name.startswith("sky."):
            raise ImportError("No module named 'sky'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_sky)
    with pytest.raises(RuntimeError, match="ark\\[skypilot\\]"):
        load_sky()


# --------------------------------------------------------------------------- #
# resolve_autostop — cost-safety policy shaping (PR4)
# --------------------------------------------------------------------------- #

def test_autostop_default_is_down_after_default_window():
    from ark.compute._sky import resolve_autostop, DEFAULT_AUTOSTOP_IDLE_MINUTES
    assert resolve_autostop({}) == {
        "idle_minutes_to_autostop": DEFAULT_AUTOSTOP_IDLE_MINUTES, "down": True}


def test_autostop_optional_disable_returns_no_kwargs():
    from ark.compute._sky import resolve_autostop
    for val in ("off", "none", "disabled", 0, -1):
        assert resolve_autostop({"idle_minutes_to_autostop": val}) == {}


def test_autostop_required_ignores_disable_and_forces_down():
    from ark.compute._sky import resolve_autostop, DEFAULT_AUTOSTOP_IDLE_MINUTES
    # required=True (experiment clusters): a disable/invalid value falls back to
    # the default window with down=True — never off, never stop-only.
    for val in ("off", 0, "garbage"):
        assert resolve_autostop({"idle_minutes_to_autostop": val}, required=True) == {
            "idle_minutes_to_autostop": DEFAULT_AUTOSTOP_IDLE_MINUTES, "down": True}
    # autostop_down:false is also overridden under required.
    assert resolve_autostop(
        {"idle_minutes_to_autostop": 10, "autostop_down": False}, required=True) == {
        "idle_minutes_to_autostop": 10, "down": True}


def test_autostop_invalid_value_falls_back_to_default():
    from ark.compute._sky import resolve_autostop, DEFAULT_AUTOSTOP_IDLE_MINUTES
    # Non-numeric junk (optional path) fails closed to the default, not off.
    assert resolve_autostop({"idle_minutes_to_autostop": "garbage"}) == {
        "idle_minutes_to_autostop": DEFAULT_AUTOSTOP_IDLE_MINUTES, "down": True}


def test_autostop_stop_only_when_down_false():
    from ark.compute._sky import resolve_autostop
    assert resolve_autostop(
        {"idle_minutes_to_autostop": 30, "autostop_down": False}) == {
        "idle_minutes_to_autostop": 30, "down": False}
