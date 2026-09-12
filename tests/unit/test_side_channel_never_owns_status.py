"""A chat turn or one-shot instruction must never touch the run's status.

Root cause of a three-day chase (2026-09-10 → 09-12). Sending a chat message
to a project launches a short-lived helper orchestrator. That helper went
through the same submit path as a real run, so it (1) overwrote the project's
job handle with its own pid — the poller then found "its" pid gone seconds
later and marked the healthy run FAILED — and (2) wrote status=done, pid=0 on
exit, so the terminal-notify sweep mailed the owner a completion notice for a
paper that did not exist yet. Twice, on f028f3ba, while the real run was
generating figures. Only the main orchestrator may write status, pid and the
job handle.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from website.dashboard import routes


@pytest.fixture
def db_project(tmp_path, monkeypatch):
    import website.dashboard.db as db
    monkeypatch.setattr(db, "_engine", None, raising=False)
    db_path = str(tmp_path / "webapp.db")
    with db.get_session(db_path) as s:
        user, _ = db.get_or_create_user_by_email(s, "owner@example.com")
        project = db.create_project(s, user_id=user.id, name="side-channel-proj")
        # A real run is in flight: the main orchestrator owns these fields.
        db.update_project(s, project, status="running", slurm_job_id="local:2510356", pid=2510356)
        pid = project.id
    return db, db_path, pid


class _StubLauncher:
    initial_status = "running"

    def __init__(self, handle="local:999999", boom=False):
        self.handle, self.boom = handle, boom

    def launch(self, spec):
        if self.boom:
            raise RuntimeError("helper could not start")
        return self.handle


def _submit(db, db_path, pid, tmp_path, launcher, **kw):
    settings = SimpleNamespace()
    with db.get_session(db_path) as s:
        project = db.get_project(s, pid)
        with patch.object(routes, "orchestrator_launcher_for", return_value=launcher), \
             patch.object(routes, "_get_user_keys", return_value={}), \
             patch.object(routes, "_admin_user_ids", return_value=set()):
            result = routes._try_submit_or_pending(project, tmp_path, s, settings, **kw)
        project = db.get_project(s, pid)
        return result, project.status, project.slurm_job_id, project.pid


class TestChatTurnLeavesTheRunAlone:
    def test_chat_does_not_overwrite_status_or_handle(self, db_project, tmp_path):
        db, db_path, pid = db_project
        result, status, handle, run_pid = _submit(
            db, db_path, pid, tmp_path, _StubLauncher(), chat_message="how is it going?")
        assert status == "running"
        assert handle == "local:2510356", "chat helper stole the run's job handle"
        assert run_pid == 2510356
        assert result == "running"

    def test_apply_instruction_does_not_overwrite_status_or_handle(self, db_project, tmp_path):
        db, db_path, pid = db_project
        _, status, handle, _ = _submit(
            db, db_path, pid, tmp_path, _StubLauncher(), apply_instruction="tighten the abstract")
        assert (status, handle) == ("running", "local:2510356")

    def test_failed_chat_helper_does_not_fail_the_run(self, db_project, tmp_path):
        db, db_path, pid = db_project
        result, status, handle, _ = _submit(
            db, db_path, pid, tmp_path, _StubLauncher(boom=True), chat_message="hi")
        assert status == "running", "a broken chat helper marked the healthy run failed"
        assert handle == "local:2510356"
        assert result == "running"

    def test_a_real_run_still_records_status_and_handle(self, db_project, tmp_path):
        """The guard is for side channels only; real launches are unchanged."""
        db, db_path, pid = db_project
        _, status, handle, _ = _submit(db, db_path, pid, tmp_path, _StubLauncher("local:424242"))
        assert (status, handle) == ("running", "local:424242")


class TestHelperOrchestratorWritesNoTerminalStatus:
    def test_chat_and_apply_paths_do_not_stamp_done(self):
        from ark.orchestrator import core
        src = inspect.getsource(core.main)
        assert 'status="done", pid=0' not in src, (
            "a side-channel helper still writes status=done on exit")
        # The one legitimate 'running' stamp is guarded for side channels.
        assert "not side_channel" in src
