"""Reap-terminal-clusters sweep tests.

`_reap_terminal_clusters` is the reliable path that `sky down`s a finished cloud
orchestrator's VM a grace period after it turns terminal — cloud orchestrators
self-report `done`/`failed` straight to the DB, so the poll-loop transition hook
never fires for them and nothing else reaps the VM until autostop.

These tests assert the gating: skypilot handles only, done/failed only (not
stopped), only after the grace period, only within the recency window, and
exactly once (marker).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jinja2")  # routes.py imports jinja2 at module load

import website.dashboard.app as app  # noqa: E402
import website.dashboard.db as db  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + projects_root, with launcher_from_handle stubbed to record the
    handles that get torn down."""
    monkeypatch.setattr(db, "_engine", None, raising=False)
    db_path = str(tmp_path / "webapp.db")
    projects_root = tmp_path / "projects"

    cancelled: list[str] = []

    class _FakeLauncher:
        def cancel(self, handle, pdir, on_complete=None):
            cancelled.append(handle)

    monkeypatch.setattr(
        app, "launcher_from_handle", lambda handle, log_fn=None: _FakeLauncher()
    )

    with db.get_session(db_path) as s:
        user, _ = db.get_or_create_user_by_email(s, "tester@example.com")
        uid = user.id

    settings = SimpleNamespace(projects_root=projects_root, db_path=db_path)
    return SimpleNamespace(
        db_path=db_path, projects_root=projects_root, uid=uid,
        settings=settings, cancelled=cancelled,
    )


def _mk(env, session, *, status, handle, age_minutes, mkdir=True):
    """Create a project row aged `age_minutes` past its terminal transition, plus
    its project dir (so the reap's `pdir.is_dir()` guard passes)."""
    updated = datetime.utcnow() - timedelta(minutes=age_minutes)
    p = db.Project(
        user_id=env.uid, name="p", status=status,
        slurm_job_id=handle, updated_at=updated,
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    if mkdir:
        (env.projects_root / env.uid / p.id).mkdir(parents=True, exist_ok=True)
    return p


def test_reaps_only_grace_elapsed_skypilot_terminal(env):
    grace = app.CLUSTER_REAP_GRACE_MINUTES
    with db.get_session(env.db_path) as s:
        reap_done = _mk(env, s, status="done", handle="skypilot:c-done", age_minutes=grace + 5)
        reap_failed = _mk(env, s, status="failed", handle="skypilot:c-failed", age_minutes=grace + 5)
        # excluded cases
        _mk(env, s, status="done", handle="skypilot:c-young", age_minutes=grace - 1)   # still in grace
        _mk(env, s, status="stopped", handle="skypilot:c-stopped", age_minutes=grace + 5)  # user Stop already tore down
        _mk(env, s, status="done", handle="local:4242", age_minutes=grace + 5)          # no VM
        _mk(env, s, status="done", handle="98765", age_minutes=grace + 5)               # bare slurm, no VM
        _mk(env, s, status="done", handle="skypilot:c-ancient", age_minutes=7 * 60)     # outside recency window

    with db.get_session(env.db_path) as s:
        app._reap_terminal_clusters(s, env.settings)

    assert sorted(env.cancelled) == ["skypilot:c-done", "skypilot:c-failed"]


def test_reap_is_do_once(env):
    grace = app.CLUSTER_REAP_GRACE_MINUTES
    with db.get_session(env.db_path) as s:
        p = _mk(env, s, status="done", handle="skypilot:c1", age_minutes=grace + 5)
        pid = p.id

    with db.get_session(env.db_path) as s:
        app._reap_terminal_clusters(s, env.settings)
    assert env.cancelled == ["skypilot:c1"]
    # marker written into the project dir
    assert (env.projects_root / env.uid / pid / ".ark_cluster_reaped").exists()

    # second sweep must not re-tear-down (marker guards it)
    with db.get_session(env.db_path) as s:
        app._reap_terminal_clusters(s, env.settings)
    assert env.cancelled == ["skypilot:c1"]


def test_reap_skips_missing_project_dir(env):
    grace = app.CLUSTER_REAP_GRACE_MINUTES
    with db.get_session(env.db_path) as s:
        _mk(env, s, status="done", handle="skypilot:c-nodir", age_minutes=grace + 5, mkdir=False)

    with db.get_session(env.db_path) as s:
        app._reap_terminal_clusters(s, env.settings)
    assert env.cancelled == []
