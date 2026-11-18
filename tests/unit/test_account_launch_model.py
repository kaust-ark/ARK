"""An account default applies only to new projects and never enters the key vault."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from website.dashboard import config, db, routes


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        db_path=str(tmp_path / "test.db"), projects_root=tmp_path,
        admin_emails=[], secret_key="local-test", cloud_conda_env="", cloud_aws_region="",
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(routes, "_start_project_async", AsyncMock())
    monkeypatch.setattr(routes, "_check_webapp_enabled", lambda: None)
    monkeypatch.setattr(routes, "has_venue_template", lambda venue: False)
    with db.get_session(settings.db_path) as session:
        for owner in ("first", "second"):
            user = db.User(id=owner, email=f"{owner}@example.test")
            keys = {"gemini": "test-google", "anthropic": "test-anthropic", "openrouter": "test-proxy"}
            user.encrypted_keys = routes.encrypt_text(json.dumps(keys), owner)
            session.add(user)
        session.commit()

    app = FastAPI()
    app.include_router(routes.router)

    @app.middleware("http")
    async def signed_in_user(request, call_next):
        request.scope["session"] = {"user_id": request.headers.get("x-test-owner", "first")}
        return await call_next(request)

    with TestClient(app) as client:
        yield settings, client


def test_default_persists_for_owner_without_rewriting_credentials(dashboard):
    settings, client = dashboard
    with db.get_session(settings.db_path) as session:
        before = session.get(db.User, "first").encrypted_keys

    endpoint = "/api/user/default-launch-model"
    canonical = "gemini/gemini-3.8-flash"
    response = client.put(endpoint, json={"model": "gemini-3.8-flash"})
    assert response.status_code == 200
    assert response.json() == {"default_launch_model": canonical}
    assert client.get("/api/user/settings").json()["default_launch_model"] == canonical
    assert client.get("/api/user/settings", headers={"x-test-owner": "second"}).json()["default_launch_model"] is None
    assert client.put(endpoint, json={"model": "not-a-model"}).status_code == 400
    assert client.put(endpoint, json={"model": ""}).json() == {"default_launch_model": None}
    with db.get_session(settings.db_path) as session:
        first = session.get(db.User, "first")
        assert first.encrypted_keys == before
        assert first.default_launch_model is None


def test_new_project_respects_owner_default_and_manual_selection(dashboard, monkeypatch):
    settings, client = dashboard
    canonical = "gemini/gemini-3.8-flash"
    assert client.put("/api/user/default-launch-model", json={"model": canonical}).status_code == 200

    def create(owner, **form):
        response = client.post("/api/projects", data={"idea": "Test", **form}, headers={"x-test-owner": owner})
        assert response.status_code == 201, response.text
        with db.get_session(settings.db_path) as session:
            project = session.get(db.Project, response.json()["id"])
            launch = settings.projects_root / owner / project.id / "config.yaml"
            assert routes._to_litellm_model(project.model_variant) == yaml.safe_load(launch.read_text())["model"]
            return project.model_variant

    # A mixed-key account must be able to use its native chip even with an
    # exhausted OpenRouter balance. An explicitly routed proxy chip still bills there.
    from io import BytesIO
    credit_probe = Mock(side_effect=lambda *a, **kw: BytesIO(b'{"data":{"total_credits":1,"total_usage":1}}'))
    monkeypatch.setattr("urllib.request.urlopen", credit_probe)
    assert create("first") == canonical
    assert create("first", model="gemini-2.5-flash") == "gemini-2.5-flash"
    credit_probe.assert_not_called()
    blocked = client.post("/api/projects", data={"model": "openrouter/google/gemini-3.8-flash"})
    assert blocked.status_code == 400 and "balance" in blocked.text

    # Removing the saved model's key restores the original omitted-model
    # behavior for this account. A second owner has always had that behavior.
    for owner in ("first", "second"):
        with db.get_session(settings.db_path) as session:
            user = session.get(db.User, owner)
            user.encrypted_keys = routes.encrypt_text('{"anthropic":"test-anthropic"}', owner)
            session.add(user)
            session.commit()
        assert create(owner) == routes.DEFAULT_MODEL
