"""Tests for the control-plane boundary (Phase 1 scaffold).

The high-value test round-trips ``LocalDbControlPlaneClient`` through the real
``website.dashboard.db`` helpers, proving the adapter is behavior-identical to the
old inline DB access the orchestrator used to do. Also covers the Null client and
the ``build_client`` selection logic.
"""

import importlib

import pytest

from ark.controlplane import (
    NullControlPlaneClient,
    LocalDbControlPlaneClient,
    HttpControlPlaneClient,
    build_client,
)
from ark.controlplane.types import Command, DecisionView


@pytest.fixture
def db_and_project(tmp_path, monkeypatch):
    """Fresh SQLite DB with one user + project; returns (db module, db_path, project_id)."""
    import website.dashboard.db as db
    # get_engine caches a module-global engine; reset so we bind to our tmp path.
    monkeypatch.setattr(db, "_engine", None, raising=False)
    db_path = str(tmp_path / "webapp.db")
    with db.get_session(db_path) as s:
        user, _ = db.get_or_create_user_by_email(s, "tester@example.com")
        project = db.create_project(s, user_id=user.id, name="test-proj")
        project_id = project.id
    return db, db_path, project_id


# ── LocalDb client: full round-trip against the real db.py helpers ──────────────

def test_localdb_available(db_and_project):
    db, db_path, pid = db_and_project
    cp = LocalDbControlPlaneClient(db_path, pid)
    assert cp.available is True


def test_localdb_report_status_roundtrip(db_and_project):
    db, db_path, pid = db_and_project
    cp = LocalDbControlPlaneClient(db_path, pid)
    cp.report_status(status="running", pid=4242, score=7.5, phase="review")
    with db.get_session(db_path) as s:
        p = db.get_project(s, pid)
    assert p.status == "running"
    assert p.pid == 4242
    assert p.score == 7.5
    assert p.phase == "review"


def test_localdb_activity_control_autonomy(db_and_project):
    db, db_path, pid = db_and_project
    cp = LocalDbControlPlaneClient(db_path, pid)
    cp.set_activity("compiling latex")
    cp.set_control_state("paused")
    cp.set_autonomy("hands_on")
    assert cp.get_autonomy() == "hands_on"
    with db.get_session(db_path) as s:
        p = db.get_project(s, pid)
    assert p.activity == "compiling latex"
    assert p.control_state == "paused"
    assert p.autonomy_level == "hands_on"


def test_localdb_fetch_project(db_and_project):
    db, db_path, pid = db_and_project
    cp = LocalDbControlPlaneClient(db_path, pid)
    view = cp.fetch_project()
    assert view is not None
    assert view.id == pid
    assert view.name == "test-proj"


def test_localdb_messages(db_and_project):
    db, db_path, pid = db_and_project
    cp = LocalDbControlPlaneClient(db_path, pid)
    cp.append_message("agent", "hello world", kind="message")
    with db.get_session(db_path) as s:
        msgs = db.list_messages(s, pid)
    assert len(msgs) == 1
    assert msgs[0].role == "agent"
    assert msgs[0].text == "hello world"


def test_localdb_commands_consume_on_read(db_and_project):
    db, db_path, pid = db_and_project
    with db.get_session(db_path) as s:
        db.enqueue_command(s, pid, "pause")
        db.enqueue_command(s, pid, "steer", payload="use pytorch")
    cp = LocalDbControlPlaneClient(db_path, pid)
    cmds = cp.poll_commands()
    assert [c.kind for c in cmds] == ["pause", "steer"]
    assert cmds[1].payload == "use pytorch"
    assert all(isinstance(c, Command) for c in cmds)
    # consumed on read → second poll is empty (legacy semantics)
    assert cp.poll_commands() == []
    cp.ack_command(cmds[0].id)  # no-op, must not raise


def test_localdb_decision_answer_flow(db_and_project):
    db, db_path, pid = db_and_project
    cp = LocalDbControlPlaneClient(db_path, pid)
    did = cp.open_decision("Proceed?", ["Yes", "No"], default_index=1)
    assert did
    dv = cp.get_decision(did)
    assert isinstance(dv, DecisionView) and dv.status == "pending"

    cp.answer_decision(did, index=0, by="telegram", source="telegram")
    dv = cp.get_decision(did)
    assert dv.status == "answered"
    assert dv.answer_index == 0


def test_localdb_decision_expire_flow(db_and_project):
    db, db_path, pid = db_and_project
    cp = LocalDbControlPlaneClient(db_path, pid)
    did = cp.open_decision("Proceed?", ["Yes", "No"])
    cp.expire_decision(did)
    dv = cp.get_decision(did)
    assert dv.status == "timed_out"


def test_localdb_events_and_artifacts_are_noops(db_and_project):
    db, db_path, pid = db_and_project
    cp = LocalDbControlPlaneClient(db_path, pid)
    # Must not raise; single-node reads these from the shared FS.
    cp.append_events([{"type": "bash", "cmd": "ls"}])
    cp.register_artifact(kind="pdf", path="/tmp/x.pdf")


# ── Null client ──────────────────────────────────────────────────────────────

def test_null_client_is_inert():
    cp = NullControlPlaneClient()
    assert cp.available is False
    assert cp.fetch_project() is None
    assert cp.get_autonomy() is None
    assert cp.poll_commands() == []
    assert cp.open_decision("q", ["a", "b"]) is None
    assert cp.get_decision("nope") is None
    # writers must be safe no-ops
    cp.report_status(status="running")
    cp.set_activity("x")
    cp.set_control_state("paused")
    cp.set_autonomy("hands_on")
    cp.append_message("agent", "hi")
    cp.answer_decision("id")
    cp.expire_decision("id")
    cp.ack_command("id")
    cp.append_events([])
    cp.register_artifact()


# ── build_client selection ────────────────────────────────────────────────────

def test_build_client_null_when_no_target():
    cp = build_client(db_path=None, project_id=None)
    assert isinstance(cp, NullControlPlaneClient)


def test_build_client_localdb_when_db_and_project(db_and_project):
    db, db_path, pid = db_and_project
    cp = build_client(db_path=db_path, project_id=pid)
    assert isinstance(cp, LocalDbControlPlaneClient)


def test_build_client_http_when_url():
    cp = build_client(control_plane_url="https://cp.example.com/v1",
                      token="t", project_id="p1")
    assert isinstance(cp, HttpControlPlaneClient)
    assert cp.available is True


def test_build_client_http_requires_project_id():
    with pytest.raises(ValueError):
        build_client(control_plane_url="https://cp.example.com/v1", token="t")
