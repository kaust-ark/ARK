"""A side turn on a finished project hands the project back as it found it.

954d7885, 2026-10-26: the owner stopped the run, then sent a chat message.
The webapp showed the turn as "initializing", and within a minute the poll
loop judged the project by its PREVIOUS run's handle. A stopped run leaves no
exit code, so the project was filed as failed. A Continue had the same window:
the project showed failed while its launch was still being set up.
"""
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ark.launcher.local import local_handle
from website.dashboard import app as webapp
from website.dashboard import routes


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    """A project its owner stopped. The stopped run left no local_exit.txt."""
    import website.dashboard.db as db
    monkeypatch.setattr(db, "_engine", None, raising=False)
    db_path = str(tmp_path / "webapp.db")
    with db.get_session(db_path) as s:
        user, _ = db.get_or_create_user_by_email(s, "owner@example.com")
        project = db.create_project(s, user_id=user.id, name="stopped-proj")
        db.update_project(s, project, status="stopped",
                          slurm_job_id=local_handle(_dead_pid()))
        pid, uid = project.id, user.id
    pdir = tmp_path / "projects" / uid / pid
    (pdir / "logs").mkdir(parents=True)
    settings = SimpleNamespace(db_path=db_path, projects_root=tmp_path / "projects",
                               base_url="http://test")
    return db, db_path, pid, pdir, settings


def _status(db, db_path, pid):
    with db.get_session(db_path) as s:
        return db.get_project(s, pid).status


def _begin_turn(db, db_path, pid, pdir, helper=None):
    with db.get_session(db_path) as s:
        project = db.get_project(s, pid)
        routes._begin_side_turn(project, pdir)
        s.add(project)
        s.commit()
    if helper:
        rec = routes._read_side_turn(pdir)
        rec["helper"] = helper
        routes._side_turn_file(pdir).write_text(json.dumps(rec))


def _one_poll_cycle(settings, monkeypatch):
    """Run the real poll loop for exactly one cycle."""
    calls = {"n": 0}

    async def sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError

    async def no_links(_settings):
        return None
    noop = lambda *a, **k: None
    monkeypatch.setattr(webapp, "get_settings", lambda: settings)
    monkeypatch.setattr(webapp.asyncio, "sleep", sleep)
    for name in ("_advance_pending_queue", "_gc_terminal_envs", "_notify_terminal_sweep",
                 "_reap_terminal_clusters", "_stuck_watchdog"):
        monkeypatch.setattr(webapp, name, noop)
    monkeypatch.setattr(webapp, "_poll_template_links", no_links)
    asyncio.run(webapp._poll_jobs(None))


class TestAChatOnAStoppedProject:
    def test_stays_stopped_while_the_helper_works(self, stopped, monkeypatch):
        db, db_path, pid, pdir, settings = stopped
        _begin_turn(db, db_path, pid, pdir, helper=local_handle(os.getpid()))
        _one_poll_cycle(settings, monkeypatch)
        assert _status(db, db_path, pid) == "initializing", \
            "the poll loop judged the turn by the stopped run's handle"

    def test_is_stopped_again_once_the_helper_exits(self, stopped, monkeypatch):
        db, db_path, pid, pdir, settings = stopped
        _begin_turn(db, db_path, pid, pdir, helper=local_handle(_dead_pid()))
        _one_poll_cycle(settings, monkeypatch)
        assert _status(db, db_path, pid) == "stopped"
        assert routes._read_side_turn(pdir) is None

    def test_a_helper_still_starting_is_waited_for(self, stopped, monkeypatch):
        db, db_path, pid, pdir, settings = stopped
        _begin_turn(db, db_path, pid, pdir)          # no helper handle yet
        _one_poll_cycle(settings, monkeypatch)
        assert _status(db, db_path, pid) == "initializing"

    def test_a_helper_that_never_started_ends_the_turn_after_the_grace(self, stopped, monkeypatch):
        db, db_path, pid, pdir, settings = stopped
        _begin_turn(db, db_path, pid, pdir)
        monkeypatch.setattr(webapp, "_INIT_GRACE_S", 0)
        _one_poll_cycle(settings, monkeypatch)
        assert _status(db, db_path, pid) == "stopped"


class TestAContinueBeingLaunched:
    def _continue(self, db, db_path, pid):
        with db.get_session(db_path) as s:
            db.update_project(s, db.get_project(s, pid), status="initializing")

    def test_is_not_judged_by_the_previous_run(self, stopped, monkeypatch):
        db, db_path, pid, pdir, settings = stopped
        self._continue(db, db_path, pid)
        _one_poll_cycle(settings, monkeypatch)
        assert _status(db, db_path, pid) == "initializing"

    def test_a_launch_that_died_with_its_webapp_does_not_hang(self, stopped, monkeypatch):
        """After the grace it gets the old handle's verdict, as before."""
        db, db_path, pid, pdir, settings = stopped
        self._continue(db, db_path, pid)
        monkeypatch.setattr(webapp, "_INIT_GRACE_S", 0)
        _one_poll_cycle(settings, monkeypatch)
        assert _status(db, db_path, pid) == "failed"


class _Launcher:
    initial_status = "running"

    def __init__(self, handle="local:x:1", boom=False):
        self.handle, self.boom = handle, boom

    def launch(self, spec):
        if self.boom:
            raise RuntimeError("no conda")
        return self.handle


def _submit(db, db_path, pid, pdir, launcher, **kw):
    with db.get_session(db_path) as s:
        project = db.get_project(s, pid)
        with patch.object(routes, "orchestrator_launcher_for", return_value=launcher), \
             patch.object(routes, "_get_user_keys", return_value={}), \
             patch.object(routes, "_admin_user_ids", return_value=set()):
            routes._try_submit_or_pending(project, pdir, s, SimpleNamespace(), **kw)


class TestTheSubmitPath:
    def test_a_started_helper_is_recorded_not_written_to_the_project(self, stopped):
        db, db_path, pid, pdir, _ = stopped
        _begin_turn(db, db_path, pid, pdir)
        _submit(db, db_path, pid, pdir, _Launcher("local:h:4242"), chat_message="hi")
        assert routes._read_side_turn(pdir) == {"prior": "stopped", "helper": "local:h:4242"}
        assert _status(db, db_path, pid) == "initializing"

    def test_a_helper_that_cannot_start_gives_the_status_back_and_says_so(self, stopped):
        db, db_path, pid, pdir, _ = stopped
        _begin_turn(db, db_path, pid, pdir)
        _submit(db, db_path, pid, pdir, _Launcher(boom=True), chat_message="hi")
        assert _status(db, db_path, pid) == "stopped"
        with db.get_session(db_path) as s:
            said = [m.text for m in db.list_messages(s, pid)]
        assert any("Could not start" in t for t in said)

    def test_a_real_launch_drops_a_leftover_turn_record(self, stopped):
        """A Stop mid-turn leaves the record behind. It must not later put
        "stopped" back over the run a Continue launched."""
        db, db_path, pid, pdir, _ = stopped
        _begin_turn(db, db_path, pid, pdir, helper="local:h:1")
        _submit(db, db_path, pid, pdir, _Launcher("local:h:777"))
        assert routes._read_side_turn(pdir) is None
        assert _status(db, db_path, pid) == "running"


def test_a_launch_still_running_here_outlasts_the_grace(stopped, monkeypatch):
    """A Continue whose launch blocks for longer than the grace (env
    provisioning, a cloud VM) is still this process's launch."""
    db, db_path, pid, pdir, settings = stopped
    with db.get_session(db_path) as s:
        db.update_project(s, db.get_project(s, pid), status="initializing")
    monkeypatch.setattr(webapp, "_INIT_GRACE_S", 0)
    routes._launches_in_flight.add(pid)
    try:
        _one_poll_cycle(settings, monkeypatch)
    finally:
        routes._launches_in_flight.discard(pid)
    assert _status(db, db_path, pid) == "initializing"


def test_a_stop_during_a_side_turn_stops_the_helper_and_keeps_done(stopped, monkeypatch):
    db, db_path, pid, pdir, settings = stopped
    with db.get_session(db_path) as s:
        db.update_project(s, db.get_project(s, pid), status="done")
    _begin_turn(db, db_path, pid, pdir, helper="local:h:4242")
    cancelled = []

    class _L:
        def cancel(self, handle, _pdir):
            cancelled.append(handle)
    monkeypatch.setattr(routes, "_require_user", lambda r: SimpleNamespace(email="o@x"))
    monkeypatch.setattr(routes, "_can_access_project", lambda u, p: True)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "launcher_from_handle", lambda h, log_fn=None: _L())
    asyncio.run(routes.api_stop_project(pid, None))
    assert cancelled == ["local:h:4242"], "Stop cancelled the finished run, not the helper"
    assert _status(db, db_path, pid) == "done"
