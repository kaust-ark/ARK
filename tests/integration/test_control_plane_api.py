"""End-to-end test of the /v1 control-plane API + HttpControlPlaneClient.

Runs the real FastAPI router under uvicorn on an ephemeral port and drives it
with the stdlib-based HttpControlPlaneClient — exercising client, server, token
auth, and the db.py helpers together over a real socket.

All DB writes happen in the server thread: the fixture seeds rows in the main
thread, then disposes the engine so the server lazily builds its own (avoids
SQLite's cross-thread connection guard). Assertions round-trip via the client.
"""

import threading
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from ark.controlplane import HttpControlPlaneClient  # noqa: E402


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """Seed a temp DB + project, serve /v1 on an ephemeral port; yields (base_url, project_id, secret)."""
    secret = "test-secret-key"
    db_path = str(tmp_path / "webapp.db")
    monkeypatch.setenv("ARK_WEBAPP_DB_PATH", db_path)
    monkeypatch.setenv("SECRET_KEY", secret)
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path / "projects"))

    import website.dashboard.config as cfg
    monkeypatch.setattr(cfg, "_settings", None, raising=False)

    import website.dashboard.db as db
    monkeypatch.setattr(db, "_engine", None, raising=False)

    # Seed a user + project + one pending command in the main thread.
    with db.get_session(db_path) as s:
        user, _ = db.get_or_create_user_by_email(s, "cp@example.com")
        project = db.create_project(s, user_id=user.id, name="cp-proj")
        project_id = project.id
        db.enqueue_command(s, project_id, "pause")
    # Drop main-thread connections so the server thread owns its own engine.
    db._engine.dispose()
    monkeypatch.setattr(db, "_engine", None, raising=False)

    import uvicorn
    from fastapi import FastAPI
    from website.dashboard.api import router

    app = FastAPI()
    app.include_router(router)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]

    yield f"http://127.0.0.1:{port}/v1", project_id, secret

    server.should_exit = True
    thread.join(timeout=5)


def _token(project_id, secret, ttl=3600):
    from website.dashboard.auth import make_job_token
    return make_job_token(project_id, secret, ttl_seconds=ttl)


# ── Happy path: client round-trips through the live server ──────────────────────

def test_http_client_full_roundtrip(live_server):
    base_url, project_id, secret = live_server
    cp = HttpControlPlaneClient(base_url, _token(project_id, secret), project_id)
    assert cp.available is True

    # bootstrap
    view = cp.fetch_project()
    assert view is not None and view.id == project_id and view.name == "cp-proj"

    # status + read-back
    cp.report_status(status="running", pid=999, score=6.5, phase="dev")
    view = cp.fetch_project()
    assert view.status == "running"
    assert view.raw["phase"] == "dev"

    # autonomy
    cp.set_autonomy("hands_on")
    assert cp.get_autonomy() == "hands_on"

    # activity / control-state must not raise
    cp.set_activity("compiling")
    cp.set_control_state("paused")

    # commands: peek → ack → gone (D2)
    cmds = cp.poll_commands()
    assert [c.kind for c in cmds] == ["pause"]
    cp.ack_command(cmds[0].id)
    assert cp.poll_commands() == []

    # messages + events + artifacts accepted
    cp.append_message("agent", "hello over http")
    cp.append_events([{"type": "bash", "cmd": "ls"}])
    cp.register_artifact(kind="pdf", path="/tmp/x.pdf")

    # decision: open → pending (answering is owned by the CP HITL engine, not
    # exposed to the orchestrator over /v1 — see test_controlplane_hitl.py).
    did = cp.open_decision("Proceed?", ["Yes", "No"], default_index=1)
    assert did
    assert cp.get_decision(did).status == "pending"


# ── Auth enforcement ────────────────────────────────────────────────────────────

def _raw_get(base_url, project_id, token=None):
    req = urllib.request.Request(f"{base_url}/projects/{project_id}", method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _raw_post(base_url, path, project_id, token, body):
    import json
    req = urllib.request.Request(
        f"{base_url}/projects/{project_id}{path}",
        data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_events_endpoint_stores(live_server):
    base_url, project_id, secret = live_server
    status, out = _raw_post(base_url, "/events", project_id, _token(project_id, secret),
                            {"lines": [{"ts": "t", "line": "hello"}, {"line": "world"}]})
    assert status == 200
    assert out["stored"] == 2


def test_missing_token_is_401(live_server):
    base_url, project_id, secret = live_server
    assert _raw_get(base_url, project_id, token=None) == 401


def test_wrong_project_token_is_403(live_server):
    base_url, project_id, secret = live_server
    other = _token("some-other-project-id", secret)
    assert _raw_get(base_url, project_id, token=other) == 403


def test_bad_signature_is_401(live_server):
    base_url, project_id, secret = live_server
    bad = _token(project_id, "the-wrong-secret")
    assert _raw_get(base_url, project_id, token=bad) == 401


def test_valid_token_is_200(live_server):
    base_url, project_id, secret = live_server
    assert _raw_get(base_url, project_id, token=_token(project_id, secret)) == 200
