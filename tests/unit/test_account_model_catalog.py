"""Account-scoped discovery and the credential-free DSH catalog fallback."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from website.dashboard import account_catalog as catalog
from website.dashboard.model_catalog import CatalogCredentialsRejected, CatalogUnavailable


@pytest.fixture(autouse=True)
def cache_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(catalog, "time", SimpleNamespace(monotonic=lambda: now[0]))
    catalog._snapshot.cache_clear()
    catalog._public_snapshot.cache_clear()
    yield now
    catalog._snapshot.cache_clear()
    catalog._public_snapshot.cache_clear()


def groups(owner, **keys):
    return asyncio.run(catalog.groups_for(owner, keys))


def public_client(monkeypatch, handler):
    original_client = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(catalog.httpx, "AsyncClient", client)


def test_unconfigured_accounts_and_non_model_secrets_never_trigger_requests(monkeypatch):
    discover, fallback = AsyncMock(), AsyncMock()
    monkeypatch.setattr(catalog.model_catalog, "discover_models", discover)
    monkeypatch.setattr(catalog, "_dsh_rows", fallback)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient")
    assert groups("A", openai=" ", github_pat="token", ssh_private_key="secret", gemini=None) == []
    discover.assert_not_awaited()
    fallback.assert_not_awaited()


def test_cache_is_scoped_by_account_provider_and_rotation(monkeypatch):
    discover = AsyncMock(side_effect=[[{"id": str(i)}] for i in range(4)])
    monkeypatch.setattr(catalog.model_catalog, "discover_models", discover)
    session_a, = groups("A", openai=" key ")
    assert session_a["models"] == [{"id": "0"}] and session_a["source"] == "provider"
    assert groups("A", openai="key") == [session_a]
    assert groups("B", openai="key")[0]["models"] == [{"id": "1"}]
    assert groups("A", openai="new-key")[0]["models"] == [{"id": "2"}]
    assert groups("A", groq="key")[0]["models"] == [{"id": "3"}]
    assert discover.await_count == 4


def test_concurrent_requests_for_same_account_share_one_refresh(monkeypatch):
    calls = []
    async def lookup(provider, key):
        calls.append((provider, key))
        await asyncio.sleep(0)
        return []
    monkeypatch.setattr(catalog.model_catalog, "discover_models", lookup)
    async def run():
        a, b = await asyncio.gather(*(catalog.groups_for("A", {"openai": "key"}) for _ in range(2)))
        assert a == b
    asyncio.run(run())
    assert calls == [("openai", "key")]


@pytest.mark.parametrize("outcome", [CatalogCredentialsRejected(), []])
def test_rejection_and_valid_empty_catalog_clear_old_models(monkeypatch, cache_clock, outcome):
    discover = AsyncMock(side_effect=[[{"id": "old"}], outcome, CatalogUnavailable(), [{"id": "new"}]])
    fallback = AsyncMock()
    monkeypatch.setattr(catalog.model_catalog, "discover_models", discover)
    monkeypatch.setattr(catalog, "_dsh_rows", fallback)
    assert groups("A", openai="key")[0]["models"] == [{"id": "old"}]
    cache_clock[0] += catalog._REFRESH
    entry, = groups("A", openai="key")
    assert entry["models"] == []
    assert entry["source"] == ("provider" if outcome == [] else "unavailable")
    cache_clock[0] += catalog._MAX_STALE + 1
    entry, = groups("A", openai="key")
    assert entry["source"] == "unavailable" and entry["models"] == []
    cache_clock[0] += catalog._RETRY
    assert groups("A", openai="key")[0]["models"] == [{"id": "new"}]
    fallback.assert_not_awaited()


def test_outage_uses_recent_private_rows_then_public_metadata(monkeypatch, cache_clock):
    discover = AsyncMock(side_effect=[[{"id": "private"}], CatalogUnavailable(), CatalogUnavailable()])
    fallback = AsyncMock(return_value=[{"id": "public"}])
    monkeypatch.setattr(catalog.model_catalog, "discover_models", discover)
    monkeypatch.setattr(catalog, "_dsh_rows", fallback)
    groups("A", openai="key")
    cache_clock[0] += catalog._REFRESH
    entry, = groups("A", openai="key")
    assert entry["models"] == [{"id": "private"}] and entry["source"] == "cache"
    assert groups("A", openai="key") == [entry]
    cache_clock[0] += catalog._MAX_STALE
    entry, = groups("A", openai="key")
    assert entry["models"] == [{"id": "public"}] and entry["source"] == "dsh"
    fallback.assert_awaited_once_with("openai")


def test_public_fallback_is_shared_across_accounts_and_sends_no_credentials(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.url)
        assert request.method == "GET" and "secret" not in str(request.url)
        assert not {"authorization", "cookie", "x-api-key", "x-goog-api-key"} & request.headers.keys()
        if request.url.host == "registry.npmjs.org":
            return httpx.Response(200, json={"dependencies": {"@earendil-works/pi-ai": "^1.1.0"}})
        assert request.url.host == "cdn.jsdelivr.net" and request.url.path.endswith("/data/google.json")
        return httpx.Response(200, json={"brokenProtocol": "?", "google-generative-ai": {
            "chat:a": {"type": "chat", "id": "a", "name": " Label\n A "}, "chat:bad": None,
            "image:graphic": {"id": "graphic"}, "chat:b": {"id": "b", "name": []},
            "chat:deep-research-x": {"id": "deep-research-x"}, "chat:wrong": {"type": "image", "id": "wrong"},
        }})
    public_client(monkeypatch, handler)
    monkeypatch.setattr(catalog.model_catalog, "discover_models", AsyncMock(side_effect=CatalogUnavailable))
    entry, = groups("A", gemini="secret-A")
    assert entry["source"] == "dsh"
    assert entry["models"] == [{"id": "gemini/a", "name": "Label A"}, {"id": "gemini/b", "name": "b"}]
    assert groups("B", gemini="secret-B") == [entry]
    assert len(calls) == 2


def test_public_failure_is_throttled_and_old_public_data_expires(monkeypatch, cache_clock):
    fetch = AsyncMock(side_effect=[[{"id": "m"}], ValueError(), OSError()])
    monkeypatch.setattr(catalog, "_fetch_dsh_rows", fetch)
    assert asyncio.run(catalog._dsh_rows("openai")) == [{"id": "m"}]
    cache_clock[0] += catalog._PUBLIC_REFRESH
    assert asyncio.run(catalog._dsh_rows("openai")) == [{"id": "m"}]
    assert asyncio.run(catalog._dsh_rows("openai")) == [{"id": "m"}]
    cache_clock[0] += catalog._PUBLIC_MAX_STALE
    assert asyncio.run(catalog._dsh_rows("openai")) == []
    assert asyncio.run(catalog._dsh_rows("cohere_chat")) == []
    assert asyncio.run(catalog._dsh_rows("unknown")) == []
    assert fetch.await_count == 3


@pytest.mark.parametrize("manifest", [
    [], {"dependencies": {}}, {"dependencies": []}, {"dependencies": {"@earendil-works/pi-ai": "//foreign.invalid/"}},
])
def test_untrusted_or_unreadable_manifest_never_supplies_a_destination(monkeypatch, manifest):
    calls = []
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=manifest)
    public_client(monkeypatch, handler)
    assert asyncio.run(catalog._dsh_rows("openai")) == []
    assert calls == ["registry.npmjs.org"]


def test_public_catalog_has_end_to_end_deadline(monkeypatch):
    async def slow(request):
        await asyncio.sleep(60)
    public_client(monkeypatch, slow)
    monkeypatch.setattr(catalog, "_PUBLIC_TIMEOUT", 0.01)
    assert asyncio.run(asyncio.wait_for(catalog._dsh_rows("openai"), timeout=0.5)) == []


def test_public_json_caps_size_before_parsing(monkeypatch):
    async def oversized():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b'{"a":{}}'))
        async with httpx.AsyncClient(transport=transport) as client:
            await catalog._public_json(client, "https://cdn.jsdelivr.net/")
    monkeypatch.setattr(catalog, "_PUBLIC_MAX_BYTES", 3)
    with pytest.raises(ValueError, match="size limit"):
        asyncio.run(oversized())
