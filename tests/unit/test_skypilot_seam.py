"""PR1 scaffolding for the folded Phases 5+6 SkyPilot work (ADR-0010).

`type: skypilot` is a *reserved and validated* compute type. As of PR2 the
Layer-1 experiment backend is implemented; the Layer-2 orchestrator launcher
lands in PR3. This locks the scaffolding contract:

- the compute factory builds a `SkyPilotBackend` for the experiment path, and
  still rejects the orchestrator path loudly (NotImplementedError) — never a
  silent wrong backend;
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
    """The Layer-2 orchestrator launcher is PR3 — still fail loudly until then."""
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
