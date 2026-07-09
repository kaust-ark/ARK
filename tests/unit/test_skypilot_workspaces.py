"""Per-user SkyPilot workspace isolation (multi-tenant GCP, SKYPILOT_PLAN.md).

Each user gets a ``ws-<id>`` workspace pinning their GCP project; one central
launcher SA (with cross-project IAM grants) provisions into it. Two pieces are
locked here:

- ``ark.compute._sky.active_workspace`` — the per-launch selector. On a SkyPilot
  that supports workspaces it sets the ``active_workspace`` config key via
  ``override_skypilot_config`` inside the ``with`` block (so the selection is
  UPLOADED to the API server with the request — a thread-local alone does not
  cross the client/server boundary) and restores it after; a falsy workspace is a
  no-op. It must NOT raise when the installed SkyPilot predates workspaces
  (graceful degrade).
- ``website.dashboard.skyworkspaces`` — renders the ARK-managed ``ws-*`` slice of
  the host's ``~/.sky/config.yaml`` from all users' projects, preserving every
  key it does not own.
"""

import types

import pytest

from ark.compute._sky import active_workspace


class _FakeCtx:
    """Records enter/exit so a test can assert the workspace was selected."""

    def __init__(self, log):
        self._log = log

    def __call__(self, workspace):
        self._log.append(("enter", workspace))
        parent = self

        class _CM:
            def __enter__(self_inner):
                return None

            def __exit__(self_inner, *exc):
                parent._log.append(("exit", workspace))
                return False

        return _CM()


def test_active_workspace_selects_and_restores(monkeypatch):
    """With a supported SkyPilot, the helper sets the ``active_workspace`` config
    key via ``override_skypilot_config`` for the given workspace and unwinds it on
    block exit. It is the override (uploaded with the request), NOT the client-only
    thread-local ``local_active_workspace_ctx``, that routes the launch to the
    user's project across the client/server boundary. Patches the REAL
    skypilot_config module (the helper imports it internally)."""
    skypilot_config = pytest.importorskip("sky.skypilot_config")
    log = []
    monkeypatch.setattr(skypilot_config, "override_skypilot_config", _FakeCtx(log))
    # local_active_workspace_ctx must exist for the workspace-capability gate; the
    # real module has it, so no need to patch — just guard the assumption.
    assert getattr(skypilot_config, "local_active_workspace_ctx", None) is not None
    with active_workspace(object(), "ws-alice"):
        pass
    assert log == [("enter", {"active_workspace": "ws-alice"}),
                   ("exit", {"active_workspace": "ws-alice"})]


def test_active_workspace_propagates_key_into_uploaded_config(monkeypatch, tmp_path):
    """Regression for the silent wrong-project launch: inside ``active_workspace``
    the ``active_workspace`` key must be visible in ``skypilot_config.to_dict()`` —
    that dict is what the client uploads to the API server as
    ``override_skypilot_config``, and the server reads the key to pick the GCP
    project. The old thread-local mechanism left ``to_dict()`` unchanged, so the
    server fell back to the 'default' workspace (the central project)."""
    import os
    import yaml
    skypilot_config = pytest.importorskip("sky.skypilot_config")
    # A real config that DEFINES the workspace (else override_skypilot_config
    # rejects an unknown workspace — itself a desirable hard-fail, tested below).
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(
        {"workspaces": {"ws-alice": {"gcp": {"project_id": "proj-a"}}}}))
    monkeypatch.setenv("SKYPILOT_GLOBAL_CONFIG", str(cfg))
    skypilot_config.reload_config()

    assert skypilot_config.to_dict().get("active_workspace") is None
    with active_workspace(object(), "ws-alice"):
        assert skypilot_config.to_dict().get("active_workspace") == "ws-alice"
    assert skypilot_config.to_dict().get("active_workspace") is None


def test_active_workspace_unknown_workspace_raises(monkeypatch, tmp_path):
    """A workspace not defined in the loaded config is a hard error, not a silent
    fall-through to the central project — the launch must surface the
    misconfiguration rather than provision in the wrong GCP project."""
    import yaml
    skypilot_config = pytest.importorskip("sky.skypilot_config")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"workspaces": {"ws-alice": {}}}))
    monkeypatch.setenv("SKYPILOT_GLOBAL_CONFIG", str(cfg))
    skypilot_config.reload_config()

    with pytest.raises(ValueError, match="ws-bob"):
        with active_workspace(object(), "ws-bob"):
            pass


def test_active_workspace_empty_is_noop():
    """A falsy workspace must not touch SkyPilot at all (default/host creds)."""
    sentinel = object()
    with active_workspace(sentinel, ""):
        pass  # no attribute access on `sentinel` ⇒ no crash


def test_active_workspace_unsupported_sky_degrades(monkeypatch):
    """An older SkyPilot without local_active_workspace_ctx must degrade to a
    no-op (launch against host creds), not raise. Remove just that one attribute
    so the real module (get_nested, …) stays intact."""
    skypilot_config = pytest.importorskip("sky.skypilot_config")
    monkeypatch.delattr(skypilot_config, "local_active_workspace_ctx", raising=False)
    import ark.compute._sky as _sky
    monkeypatch.setattr(_sky, "_WORKSPACE_UNSUPPORTED_WARNED", False, raising=False)

    with active_workspace(object(), "ws-bob"):
        pass  # must not raise


# ── registry rendering ───────────────────────────────────────────────────────
def _user(uid, project):
    return types.SimpleNamespace(id=uid, _project=project)


def _keys_for(user):
    return {"gcp_project": user._project} if user._project else {}


def test_build_workspaces_maps_projects_and_omits_empty():
    from website.dashboard.skyworkspaces import build_workspaces, workspace_name_for
    users = [_user("alice", "proj-a"), _user("bob", "proj-b"), _user("carol", None)]
    ws = build_workspaces(users, _keys_for)
    assert ws == {
        workspace_name_for("alice"): {"gcp": {"project_id": "proj-a"}},
        workspace_name_for("bob"): {"gcp": {"project_id": "proj-b"}},
    }
    assert workspace_name_for("carol") not in ws  # no project ⇒ omitted


def test_render_preserves_unmanaged_and_drops_stale(tmp_path, monkeypatch):
    import yaml
    from website.dashboard import skyworkspaces as sw

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "workspaces": {"default": {}, "ws-OLD": {"gcp": {"project_id": "gone"}}},
        "gcp": {"vpc_name": "keepme"},
    }))
    monkeypatch.setenv("SKYPILOT_CONFIG", str(cfg))

    users = [_user("alice", "proj-a"), _user("carol", None)]
    # get_session is only used to fetch users; stub it out of the code path.
    monkeypatch.setattr("website.dashboard.db.get_session",
                        lambda *a, **k: __import__("contextlib").nullcontext(None))

    n = sw.render_sky_workspaces("unused", get_user_keys=_keys_for,
                                 list_users=lambda _s: users)
    assert n == 1
    out = yaml.safe_load(cfg.read_text())
    assert out["gcp"]["vpc_name"] == "keepme"          # unmanaged key preserved
    assert "default" in out["workspaces"]              # hand-authored preserved
    assert "ws-OLD" not in out["workspaces"]           # stale managed dropped
    assert out["workspaces"]["ws-alice"]["gcp"]["project_id"] == "proj-a"
    assert "ws-carol" not in out["workspaces"]         # project-less omitted


# ── orchestrator boot image: central-project, cross-tenant ───────────────────
# The baked ARK image lives ONCE in the central launcher project. The launcher SA
# reads it there and boots the VM into the tenant's project (cross-project image).
# So config.yaml's orchestrator image_id must point at the CENTRAL project, never
# the tenant's own project (which is empty for a fresh user ⇒ 404 at launch).
def _project(orch_backend):
    return types.SimpleNamespace(
        name="proj", title="T", idea="an idea", venue="NeurIPS",
        venue_format="conference", venue_pages=9, layout_mode="balanced",
        mode="paper", max_iterations=3, max_dev_iterations=3,
        figure_generation="nano_banana", orchestrator_compute_backend=orch_backend,
        experiment_compute_backend="local", compute_backend="local",
        telegram_token=None, telegram_chat_id=None, skip_deep_research=False,
    )


def _write_and_read(tmp_path, monkeypatch, *, central_project, user_gcp_project):
    """Drive _write_config_yaml for a skypilot:gcp orchestrator and return the
    parsed orchestrator_compute_backend block."""
    import yaml
    from website.dashboard import routes

    user = types.SimpleNamespace(id="alice")
    monkeypatch.setattr(routes, "_get_user_keys",
                        lambda u: {"gcp_project": user_gcp_project} if user_gcp_project else {})
    settings = types.SimpleNamespace(
        cloud_gcp_project=central_project, cloud_conda_env="ark-base")

    routes._write_config_yaml(tmp_path, _project("skypilot:gcp"), user, settings)
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    return cfg["orchestrator_compute_backend"]


def test_orchestrator_image_id_uses_central_not_tenant_project(tmp_path, monkeypatch):
    """image_id is pinned to the CENTRAL project even when the tenant has their own
    project — and the workspace still routes the VM into the tenant's project."""
    occ = _write_and_read(tmp_path, monkeypatch,
                          central_project="kaust-pf2023-marco", user_gcp_project="tenant-proj")
    assert occ["image_id"] == (
        "projects/kaust-pf2023-marco/global/images/ark-debian-base-v7")
    # Boot image is central, but the launch still lands in the tenant's project.
    from website.dashboard.skyworkspaces import workspace_name_for
    assert occ["workspace"] == workspace_name_for("alice")


def test_orchestrator_image_id_omitted_without_central_project(tmp_path, monkeypatch):
    """No central project configured ⇒ omit image_id (boot stock public Debian);
    the setup_commands block then does the full install from scratch."""
    occ = _write_and_read(tmp_path, monkeypatch,
                          central_project="", user_gcp_project="tenant-proj")
    assert "image_id" not in occ
    assert occ["setup_commands"]  # fallback path still installs everything
