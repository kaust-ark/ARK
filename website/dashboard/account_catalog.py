"""Assemble the model picker from the credentials configured by one account."""
import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

import httpx

from ark.engines.dsh_driver import SDK_VERSION
from . import model_catalog

# DSH adapter provider names that differ from Idea2Paper routing prefixes.
_PI_NAMES = {"gemini": "google", "moonshot": "moonshotai"}
_REFRESH = 60 * 60
_RETRY = 5 * 60
_MAX_STALE = 24 * 60 * 60
_PUBLIC_REFRESH = 24 * 60 * 60
_PUBLIC_MAX_STALE = 7 * _PUBLIC_REFRESH
_PUBLIC_TIMEOUT = 8.0
_PUBLIC_MAX_BYTES = 4 * 1024 * 1024


@dataclass
class _Snapshot:
    rows: list = field(default_factory=list)
    source: str = "unavailable"
    retry_at: float = 0
    live_at: float | None = None
    rejected: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@lru_cache(maxsize=256)
def _snapshot(owner_id, provider, credential_fingerprint):
    # A rotation creates a fresh entry. The account cache retains no API key.
    return _Snapshot()


@lru_cache(maxsize=16)
def _public_snapshot(provider):
    return _Snapshot()


async def _public_json(client, url):
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > _PUBLIC_MAX_BYTES:
                raise ValueError("Catalog exceeds size limit")
            body.extend(chunk)
    obj = json.loads(body)
    if not isinstance(obj, dict):
        raise ValueError("Invalid catalog object")
    return obj


def _parse_dsh_rows(provider, groups):
    rows = {}
    valid_protocol = False
    chat_entries = 0
    readable_entries = 0
    for protocol in groups.values():
        if not isinstance(protocol, dict):
            continue
        valid_protocol = True
        for kind, row in protocol.items():
            if not kind.startswith("chat:"):
                continue
            chat_entries += 1
            if not isinstance(row, dict):
                continue
            model_id = row.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            readable_entries += 1
            if (row.get("type", "chat") != "chat" or not model_catalog.is_agent_model_id(model_id, provider)
                    or (provider == "xai" and "multi-agent" in model_id.lower())):
                continue
            label = row.get("name")
            name = " ".join(label.split())[:200] if isinstance(label, str) else ""
            rows[model_id] = {"id": f"{provider}/{model_id}", "name": name or model_id}
    if (groups and not valid_protocol) or (chat_entries and not readable_entries):
        raise ValueError("Invalid pi-ai chat catalog")
    return list(rows.values())


async def _fetch_dsh_rows(provider):
    npm_version = re.sub(r"rc(\d+)$", r"-rc.\1", SDK_VERSION)
    manifest_url = f"https://registry.npmjs.org/@deepseek-ai%2fdsh-llm-pi-ai/{npm_version}"
    # Public metadata is independent of all account, environment and proxy credentials.
    async with httpx.AsyncClient(timeout=4, follow_redirects=False, trust_env=False) as client:
        manifest = await _public_json(client, manifest_url)
        dependencies = manifest.get("dependencies")
        dependency = dependencies.get("@earendil-works/pi-ai") if isinstance(dependencies, dict) else None
        if not isinstance(dependency, str) or not re.fullmatch(r"[~^]?\d+\.\d+\.\d+", dependency):
            raise ValueError("Unsupported pi-ai dependency range")
        route = _PI_NAMES.get(provider, provider)
        catalog_url = (f"https://cdn.jsdelivr.net/npm/@earendil-works/pi-ai@{dependency}"
                       f"/dist/providers/data/{route}.json")
        groups = await _public_json(client, catalog_url)
    return _parse_dsh_rows(provider, groups)


async def _dsh_rows(provider):
    # Cohere is served by its native catalog. It has no built-in pi-ai provider.
    if provider not in model_catalog.PROVIDER_LABELS or provider == "cohere_chat":
        return []
    saved = _public_snapshot(provider)
    async with saved.lock:
        if time.monotonic() >= saved.retry_at:
            try:
                saved.rows = await asyncio.wait_for(_fetch_dsh_rows(provider), timeout=_PUBLIC_TIMEOUT)
                saved.live_at = time.monotonic()
                saved.retry_at = saved.live_at + _PUBLIC_REFRESH
            except (httpx.HTTPError, asyncio.TimeoutError, OSError, ValueError, RecursionError):
                saved.retry_at = time.monotonic() + _RETRY
        if saved.live_at is not None and time.monotonic() - saved.live_at < _PUBLIC_MAX_STALE:
            return saved.rows
        return []


async def _group(owner_id, provider, key):
    saved = _snapshot(owner_id, provider, hashlib.sha256(key.encode()).digest())
    async with saved.lock:
        now = time.monotonic()
        if now >= saved.retry_at:
            try:
                saved.rows = await model_catalog.discover_models(provider, key)
                saved.source = "provider"
                saved.live_at = time.monotonic()
                saved.rejected = False
                saved.retry_at = saved.live_at + _REFRESH
            except model_catalog.CatalogCredentialsRejected:
                saved.rows, saved.source, saved.live_at = [], "unavailable", None
                saved.rejected = True
                saved.retry_at = time.monotonic() + _RETRY
            except model_catalog.CatalogUnavailable:
                now = time.monotonic()
                if saved.live_at is not None and now - saved.live_at < _MAX_STALE:
                    saved.source = "cache"
                elif saved.rejected or (saved.live_at is not None and not saved.rows):
                    # No evidence the account gained access since a denied key or
                    # an empty provider catalog. Only a new live result can establish it.
                    saved.source = "unavailable"
                else:
                    saved.rows = await _dsh_rows(provider)
                    saved.source = "dsh" if saved.rows else "unavailable"
                    saved.live_at = None
                saved.retry_at = time.monotonic() + _RETRY
        return {"id": provider, "label": model_catalog.PROVIDER_LABELS[provider],
                "models": saved.rows, "source": saved.source}


async def groups_for(owner_id, keys):
    return await asyncio.gather(*(
        _group(owner_id, provider, keys[provider].strip())
        for provider in model_catalog.PROVIDER_LABELS
        if isinstance(keys.get(provider), str) and keys[provider].strip()
    ))
