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


def replace_keys(settings, owner, keys):
    with db.get_session(settings.db_path) as session:
        user = session.get(db.User, owner)
        user.encrypted_keys = routes.encrypt_text(json.dumps(keys), owner)
        session.add(user)
        session.commit()


def test_private_catalog_serves_only_signed_in_owner_and_keeps_saved_default(dashboard, monkeypatch):
    settings, client = dashboard
    replace_keys(settings, "first", {"deepseek": "owner-direct", "openrouter": "owner-proxy", "github_pat": "unrelated"})
    replace_keys(settings, "second", {"groq": "other-direct"})

    async def discover(provider, key):
        return [{"id": f"{provider}/future", "name": "Future model"}]

    monkeypatch.setattr(routes.model_catalog, "discover_models", discover)
    routes.account_catalog._snapshot.cache_clear()
    endpoint = "/api/user/model-catalog"
    assert client.get(endpoint, headers={"x-test-owner": "missing"}).status_code == 401
    assert client.put("/api/user/default-launch-model", json={"model":"deepseek/pinned"}).is_success
    response = client.get(endpoint)
    assert response.headers["cache-control"] == "private, no-store"
    data = response.json()
    assert data["automatic_model"] == data["default_launch_model"] == "deepseek/pinned"
    assert {g["id"] for g in data["providers"]} == {"deepseek", "openrouter"}
    assert "owner-direct" not in response.text and "unrelated" not in response.text
    other = client.get(endpoint, headers={"x-test-owner":"second"}).json()
    assert [g["id"] for g in other["providers"]] == ["groq"]
    assert other["automatic_model"] == "groq/future"
    project = client.post("/api/projects", data={"idea":"test"}, headers={"x-test-owner":"second"})
    assert project.status_code == 201, project.text
    with db.get_session(settings.db_path) as session:
        assert session.get(db.Project, project.json()["id"]).model_variant == other["automatic_model"]


def test_explicit_direct_route_and_manual_validation_apply_before_creation(dashboard):
    settings, client = dashboard
    replace_keys(settings, "first", {"openrouter": "owner-proxy"})
    for unavailable in ("deepseek/foo", "invalid", "groq/a/../b", "openrouter/org//model"):
        response = client.post("/api/projects", data={"idea":"test", "model":unavailable})
        assert response.status_code == 400, response.text
        assert client.put("/api/user/default-launch-model", json={"model":unavailable}).status_code == 400
    assert not (settings.projects_root / "first").exists()


@pytest.mark.parametrize("operation", ["continue", "restart"])
def test_resume_preflights_and_persists_new_route_before_modifying_workspace(dashboard, monkeypatch, operation):
    settings, client = dashboard
    replace_keys(settings, "first", {"deepseek":"owner-direct"})
    monkeypatch.setattr(routes, "_restart_project_async", AsyncMock())
    new = client.post("/api/projects", data={"idea":"test", "model":"deepseek/first"})
    assert new.status_code == 201, new.text
    ident = new.json()["id"]
    url = f"/api/projects/{ident}/{operation}"
    workspace = settings.projects_root / "first" / ident
    launch_file = workspace / "config.yaml"
    original = launch_file.read_text()
    with db.get_session(settings.db_path) as session:
        project = session.get(db.Project, ident)
        project.status = "stopped"
        session.add(project)
        session.commit()
    replace_keys(settings, "first", {"groq":"other-provider", "openrouter":"owner-proxy"})
    assert client.post(url, json={}).status_code == 400
    assert client.post(url, json={"model":"cohere_chat/absent"}).status_code == 400
    assert launch_file.read_text() == original
    with db.get_session(settings.db_path) as session:
        assert session.get(db.Project, ident).status == "stopped"
    probe = Mock(side_effect=AssertionError("Direct launches must not probe the relay balance"))
    monkeypatch.setattr("urllib.request.urlopen", probe)
    result = client.post(url, json={"model":"groq/replacement"})
    assert result.is_success, result.text
    probe.assert_not_called()
    assert yaml.safe_load(launch_file.read_text())["model"] == "groq/replacement"
    with db.get_session(settings.db_path) as session:
        project = session.get(db.Project, ident)
        assert (project.model, project.model_variant) == ("groq","groq/replacement")


def test_legacy_bare_project_displays_the_original_billing_route(tmp_path):
    project = SimpleNamespace(model="anthropic", model_variant="claude-sonnet-5-5")
    (tmp_path / "config.yaml").write_text("model: openrouter/anthropic/claude-sonnet-5.5\n")
    assert routes._read_project_model(tmp_path, project) == "openrouter/anthropic/claude-sonnet-5.5"
