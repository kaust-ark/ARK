"""Every provider the picker offers resolves to the vendor's real endpoint.

`route()` returns the wire protocol and base URL that DSH hands to pi-ai, and
unit tests pin the strings. They cannot catch a base URL that is merely wrong
— Gemini needs its OpenAI-compatible path (`/v1beta/openai`), not the bare
host, and Anthropic's base carries no `/v1` because its client appends
`/v1/messages` itself.

This sends an unauthenticated POST to the path each protocol's client really
uses and asserts the vendor refuses it for credentials (400/401/403) rather
than 404-ing a path that does not exist. No key, no cost, no tokens.

Marked `network`; the offline suite deselects it.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from ark.engines.dsh_runtime import route

# What the DSH runtime binary's clients append to the configured base URL.
CLIENT_PATH = {
    "anthropic-messages": "/v1/messages",
    "openai-completions": "/chat/completions",
    "openai-responses": "/responses",
}

PROVIDERS = [
    ("openrouter", "openrouter/openai/gpt-4o-mini"),
    ("anthropic", "anthropic/claude-sonnet-4-6"),
    ("openai", "openai/gpt-4o-mini"),
    ("gemini", "gemini/gemini-2.5-flash"),
]


@pytest.mark.network
@pytest.mark.parametrize("provider,model", PROVIDERS, ids=[p for p, _ in PROVIDERS])
def test_the_route_reaches_the_vendors_api(provider, model, monkeypatch):
    for var in ("LLM_BASE_URL", "ARK_DSH_API"):
        monkeypatch.delenv(var, raising=False)

    protocol, endpoint = route(model)
    url = endpoint.rstrip("/") + CLIENT_PATH[protocol]
    request = urllib.request.Request(
        url, method="POST",
        data=json.dumps({"model": "x", "messages": [], "max_tokens": 1}).encode(),
        headers={"content-type": "application/json",
                 # Anthropic refuses outright without it; the others ignore it.
                 "anthropic-version": "2023-06-01"},
    )
    try:
        status = urllib.request.urlopen(request, timeout=30).status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:                      # offline, DNS, proxy
        pytest.skip(f"cannot reach {url}: {e}")

    # 404 is the failure this test exists for: the host answered, but nothing
    # listens on the path we send model requests to.
    assert status in (400, 401, 403), (
        f"{provider} route {url} answered {status}; expected the vendor to "
        f"refuse an unauthenticated request")
