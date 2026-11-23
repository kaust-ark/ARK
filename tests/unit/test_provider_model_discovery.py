"""Provider catalogs use only the selected owner's key at the fixed listing endpoint."""
import asyncio

import httpx
import pytest

from website.dashboard import model_catalog as catalog


def discover(provider, handler, key="owner-secret"):
    return asyncio.run(catalog.discover_models(provider, key, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("provider,url,body,expected", [
    ("openrouter", "https://openrouter.ai/api/v1/models/user", {"data": [
        {"id": "vendor/general:free", "name": "A free model", "supported_parameters": ["tools"],
         "architecture": {"output_modalities": ["text"]}},
        {"id": "vendor/other", "supported_parameters": ["temperature"]},
        {"id": "vendor/paint", "architecture": {"output_modalities": ["image"]}},
    ]}, ("vendor/general:free", "A free model")),
    ("openai", "https://api.openai.com/v1/models", {"data": [
        {"id": "ft:gpt-next:my-project::abc"}, {"id": "whisper-1"}, {"id": "gpt-image-5"},
    ]}, ("ft:gpt-next:my-project::abc", "ft:gpt-next:my-project::abc")),
    ("deepseek", "https://api.deepseek.com/models", {"data": [{"id": "deepseek-future"}]},
     ("deepseek-future", "deepseek-future")),
    ("xai", "https://api.x.ai/v1/language-models", {"models": [
        {"id": "grok-testing", "output_modalities": ["text"], "input_modalities": ["image", "text"]},
        {"id": "unrelated", "output_modalities": ["video"]},
        {"id": "grok-4.20-multi-agent", "output_modalities": ["text"]},
    ]}, ("grok-testing", "grok-testing")),
    ("mistral", "https://api.mistral.ai/v1/models", {"data": [
        {"id": "mistral-next", "name": "Mistral", "capabilities": {"completion_chat": True, "function_calling": True}},
        {"id": "search-next", "capabilities": {"completion_chat": False}},
        {"id": "completion-only", "capabilities": {"function_calling": False}},
    ]}, ("mistral-next", "Mistral")),
    ("groq", "https://api.groq.com/openai/v1/models", {"data": [
        {"id": "org/open-lm-foo"}, {"id": "retired", "active": False}, {"id": "llama-guard-9"},
    ]}, ("org/open-lm-foo", "org/open-lm-foo")),
    ("moonshot", "https://api.moonshot.ai/v1/models", {"data": [{"id": "kimi-next"}]}, ("kimi-next", "kimi-next")),
])
def test_bearer_catalogs(provider, url, body, expected):
    def respond(req):
        assert req.method == "GET" and str(req.url) == url
        assert req.headers["authorization"] == "Bearer owner-secret"
        assert "x-goog-api-key" not in req.headers and "x-api-key" not in req.headers
        return httpx.Response(200, json=body)

    model, label = expected
    assert discover(provider, respond) == [{"id": f"{provider}/{model}", "name": label}]


@pytest.mark.parametrize("provider,host,field,cursor,continuation,row,expected", [
    ("gemini", "generativelanguage.googleapis.com", "models", "pageToken", {"nextPageToken": "page2"},
     {"name": "models/gemini-next", "displayName": "Gemini Next", "supportedGenerationMethods": ["generateContent"]},
     "gemini-next"),
    ("anthropic", "api.anthropic.com", "data", "after_id", {"has_more": True, "last_id": "page2"},
     {"id": "claude-next", "display_name": "Claude Next"}, "claude-next"),
    ("cohere_chat", "api.cohere.com", "models", "page_token", {"next_page_token": "page2"},
     {"name": "command-next", "endpoints": ["chat"]}, "command-next"),
])
def test_account_paginated_catalogs(provider, host, field, cursor, continuation, row, expected):
    seen = []
    def respond(req):
        assert req.url.host == host and req.method == "GET"
        assert "owner-secret" not in str(req.url)
        if provider == "gemini":
            assert req.headers["x-goog-api-key"] == "owner-secret" and "authorization" not in req.headers
            unrelated = {"name": "models/tiny", "supportedGenerationMethods": ["embedContent"]}
        elif provider == "anthropic":
            assert req.headers["x-api-key"] == "owner-secret" and "authorization" not in req.headers
            assert req.headers["anthropic-version"] == "2023-06-01"
            unrelated = {"id": "bad\nname"}
        else:
            assert req.url.params["endpoint"] == "chat" and req.headers["authorization"] == "Bearer owner-secret"
            unrelated = {"name": "reranker", "endpoints": ["rerank"]}
        seen.append(req.url.params.get(cursor))
        if len(seen) == 1:
            return httpx.Response(200, json={field: [unrelated, row], **continuation})
        return httpx.Response(200, json={field: [row]})
    assert [r["id"] for r in discover(provider, respond)] == [f"{provider}/{expected}"]
    assert seen == [None, "page2"]


def test_redirect_never_forwards_key_and_errors_never_echo_payload():
    calls = []
    def respond(req):
        calls.append(req.url.host)
        return httpx.Response(302, headers={"Location": "https://other.example/?key=owner-secret"})
    with pytest.raises(catalog.CatalogUnavailable) as result:
        discover("openai", respond)
    assert calls == ["api.openai.com"] and "owner-secret" not in str(result.value)


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_credential_refusal_is_distinct_from_outage(status):
    def respond(req):
        return httpx.Response(status, text="owner-secret")
    with pytest.raises(catalog.CatalogUnavailable) as result:
        discover("openai", respond)
    assert isinstance(result.value, catalog.CatalogCredentialsRejected) == (status in {401, 403})
    assert "owner-secret" not in str(result.value)


def test_empty_catalog_is_not_an_outage_and_old_data_is_not_mixed_with_bad_pages():
    assert discover("gemini", lambda _: httpx.Response(200, json={"models": []})) == []
    with pytest.raises(catalog.CatalogUnavailable):
        discover("openai", lambda _: httpx.Response(200, json={"unexpected": []}))
    with pytest.raises(catalog.CatalogUnavailable):
        discover("anthropic", lambda _: httpx.Response(200, json={"data": [], "has_more": True, "last_id": "same"}))


def test_google_bad_request_classifies_only_the_invalid_key_error_as_rejected():
    def error(reason):
        return httpx.Response(400, json={"error": {"details": [{"reason": reason}]}})
    with pytest.raises(catalog.CatalogCredentialsRejected):
        discover("gemini", lambda _: error("API_KEY_INVALID"))
    with pytest.raises(catalog.CatalogUnavailable) as result:
        discover("gemini", lambda _: error("INVALID_ARGUMENT"))
    assert not isinstance(result.value, catalog.CatalogCredentialsRejected)


def test_unknown_provider_and_missing_credentials_never_touch_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    def no_request(req):
        pytest.fail("sent a credential")
    for provider, key in [("https://evil.example", "owner-secret"), ("github_pat", "pat"), ("openai", ""),
                          ("openai", "test\r\nInjected: header")]:
        with pytest.raises(catalog.CatalogUnavailable):
            discover(provider, no_request, key)


def test_catalog_response_is_bounded(monkeypatch):
    monkeypatch.setattr(catalog, "_MAX_PAGE_BYTES", 50)
    with pytest.raises(catalog.CatalogUnavailable):
        discover("openai", lambda _: httpx.Response(200, json={"data": [{"id": "x" * 60}]}))


def test_provider_listing_reflects_the_calling_credential():
    def respond(req):
        key = req.headers["authorization"]
        return httpx.Response(200, json={"data": [{"id": "model-a" if key == "Bearer A" else "model-b"}]})
    assert discover("openrouter", respond, key="A")[0]["id"] == "openrouter/model-a"
    assert discover("openrouter", respond, key="B")[0]["id"] == "openrouter/model-b"


def test_malformed_capabilities_do_not_hide_valid_siblings():
    rows = [{"id": "ok"}, {"id": "bad", "architecture": ["unexpected"]},
            {"id": "bad-caps", "capabilities": [42]}, {"id": "bad-parameters", "supported_parameters": 52}]
    assert discover("openrouter", lambda _: httpx.Response(200, json={"data": rows})) == [
        {"id": "openrouter/ok", "name": "ok"}]


def test_openrouter_rolling_alias_round_trips_to_the_picker():
    alias = "~google/gemini-pro-latest"
    rows = discover("openrouter", lambda _: httpx.Response(200, json={"data": [{"id": alias}]}))
    assert rows == [{"id": "openrouter/" + alias, "name": alias}]
    assert not catalog.valid_launch_id("openai/" + alias)
