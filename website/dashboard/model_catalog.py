"""Read model metadata with the project owner's credential at its issuing provider."""
from __future__ import annotations

import asyncio
import json
import re

import httpx


# Hosts are application configuration. Neither account fields nor pagination
# tokens may select a destination for a credential.
_ENDPOINTS = {
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1/models/user"),
    "anthropic": ("Anthropic", "https://api.anthropic.com/v1/models"),
    "openai": ("OpenAI", "https://api.openai.com/v1/models"),
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/models"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/models"),
    "xai": ("xAI", "https://api.x.ai/v1/language-models"),
    "mistral": ("Mistral", "https://api.mistral.ai/v1/models"),
    "groq": ("Groq", "https://api.groq.com/openai/v1/models"),
    "moonshot": ("Moonshot", "https://api.moonshot.ai/v1/models"),
    "cohere_chat": ("Cohere", "https://api.cohere.com/v1/models"),
}
PROVIDER_LABELS = {provider: label for provider, (label, _) in _ENDPOINTS.items()}

_MAX_PAGES = 12
_MAX_PAGE_BYTES = 8 * 1024 * 1024
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
# Several providers return only an ID. Exclude recognizable specialist
# products without pinning the names or version numbers of general models.
_SPECIALIST = re.compile(
    r"(?:^|[-_./:])(?:embeddings?|embed(?:ding)?|rerank(?:er)?|moderation|guard|safeguard|"
    r"whisper|tts|transcri(?:be|ption)|realtime|audio|speech|dall-e|image(?:gen)?|imagen|"
    r"veo|sora|video|ocr|deep-research|computer-use)(?:$|[-_./:])", re.I,
)


class CatalogUnavailable(RuntimeError):
    """A complete discovery result is unavailable. Messages never include credentials."""


class CatalogCredentialsRejected(CatalogUnavailable):
    """The credential cannot be used to list models at the issuing provider."""


def valid_launch_id(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 200:
        return False
    provider, _, model = value.partition("/")
    # OpenRouter also publishes rolling aliases like ~google/gemini-pro-latest.
    if provider == "openrouter":
        model = model.removeprefix("~")
    return bool(re.fullmatch(r"[a-z0-9_]+", provider) and _MODEL_ID.fullmatch(model)
                and all(part not in {"", ".", ".."} for part in model.split("/")))


def is_agent_model_id(model_id: str, provider: str) -> bool:
    return (isinstance(model_id, str) and valid_launch_id(f"{provider}/{model_id}")
            and not _SPECIALIST.search(model_id))


def _request_fields(provider, key):
    if provider == "gemini":
        return {"x-goog-api-key": key}, {"pageSize": 1000}
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}, {"limit": 1000}
    params = {"endpoint": "chat", "page_size": 1000} if provider == "cohere_chat" else {}
    return {"Authorization": f"Bearer {key}"}, params


def _model_row(provider, item):
    if not isinstance(item, dict) or item.get("active") is False or item.get("is_deprecated"):
        return None
    if provider == "gemini":
        methods = item.get("supportedGenerationMethods")
        if not isinstance(methods, list) or "generateContent" not in methods:
            return None
        model_id = item.get("name")
        if not isinstance(model_id, str) or not model_id.startswith("models/"):
            return None
        model_id = model_id.removeprefix("models/")
        label = item.get("displayName")
    else:
        model_id = item.get("name") if provider == "cohere_chat" else item.get("id")
        label = item.get("display_name") or item.get("name")
    if not is_agent_model_id(model_id, provider):
        return None

    # Prefer explicit capability statements to guesses about model families.
    if provider == "xai" and "multi-agent" in model_id.lower():
        # xAI's multi-agent route uses server tools, not client function calls.
        return None
    endpoints = item.get("endpoints", ["chat"])
    if provider == "cohere_chat" and (not isinstance(endpoints, list) or "chat" not in endpoints):
        return None
    capabilities = item.get("capabilities") or {}
    if (not isinstance(capabilities, dict) or capabilities.get("completion_chat") is False
            or capabilities.get("function_calling") is False):
        return None
    parameters = item.get("supported_parameters", ["tools"])
    if provider == "openrouter" and (not isinstance(parameters, list) or "tools" not in parameters):
        return None
    architecture = item.get("architecture") or {}
    if not isinstance(architecture, dict):
        return None
    modalities = (item.get("output_modalities") or item.get("supportedOutputModalities")
                  or architecture.get("output_modalities"))
    if modalities and (not isinstance(modalities, list) or "text" not in {str(v).lower() for v in modalities}):
        return None
    if not isinstance(label, str) or not label.strip():
        label = model_id
    return {"id": f"{provider}/{model_id}", "name": " ".join(label.split())[:200]}


def _next_page(provider, page):
    if provider == "gemini":
        return "pageToken", page.get("nextPageToken")
    if provider == "cohere_chat":
        return "page_token", page.get("next_page_token")
    if provider == "anthropic" and page.get("has_more"):
        if not page.get("last_id"):
            raise CatalogUnavailable("Missing catalog cursor.")
        return "after_id", page["last_id"]
    return None, None


async def _fetch(provider, key, transport):
    headers, params = _request_fields(provider, key)
    rows_field = "models" if provider in {"gemini", "cohere_chat", "xai"} else "data"
    rows = {}
    seen_tokens = set()
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=6.0,
                                 transport=transport) as client:
        for _ in range(_MAX_PAGES):
            async with client.stream("GET", _ENDPOINTS[provider][1], headers=headers, params=params) as response:
                if response.status_code in {401, 403}:
                    raise CatalogCredentialsRejected("Provider rejected the catalog credential.")
                # Google reports an invalid API key as HTTP 400 with a
                # structured ErrorInfo, unlike a malformed listing request.
                google_bad_request = provider == "gemini" and response.status_code == 400
                if not google_bad_request:
                    response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(raw) + len(chunk) > _MAX_PAGE_BYTES:
                        raise CatalogUnavailable("Provider model catalog exceeds the size limit.")
                    raw.extend(chunk)
            page = json.loads(raw)
            if google_bad_request:
                details = page.get("error", {}).get("details", []) if isinstance(page, dict) else []
                if any(isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details):
                    raise CatalogCredentialsRejected("Provider rejected the catalog credential.")
                raise CatalogUnavailable("Provider rejected the catalog request.")
            if not isinstance(page, dict) or not isinstance(page.get(rows_field), list):
                raise CatalogUnavailable("Provider returned an invalid model catalog.")
            for item in page[rows_field]:
                row = _model_row(provider, item)
                if row:
                    rows[row["id"]] = row
            param, token = _next_page(provider, page)
            if not token:
                return list(rows.values())
            if not isinstance(token, str) or len(token) > 4096 or token in seen_tokens:
                raise CatalogUnavailable("Provider returned an invalid catalog cursor.")
            seen_tokens.add(token)
            params[param] = token
    raise CatalogUnavailable("Provider model catalog exceeds the pagination limit.")


async def discover_models(provider: str, api_key: str, *, transport=None) -> list[dict[str, str]]:
    """Return canonical launch IDs and display names. Never generate tokens or read environment keys."""
    if provider not in _ENDPOINTS:
        raise CatalogUnavailable("Provider has no configured model-list endpoint.")
    key = api_key.strip() if isinstance(api_key, str) else ""
    if not key or not key.isascii() or not key.isprintable():
        raise CatalogCredentialsRejected("Invalid provider credential.")
    try:
        return await asyncio.wait_for(_fetch(provider, key, transport), timeout=15.0)
    except (httpx.HTTPError, OSError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
        # A provider can echo the submitted credential in its response; errors
        # from HTTP clients and parsers must not escape into user-facing logs.
        raise CatalogUnavailable("Could not read the provider model catalog.") from None
