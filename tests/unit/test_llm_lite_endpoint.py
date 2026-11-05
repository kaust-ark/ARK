"""Utility calls must use the selected gateway without leaking its fallback key."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from ark.llm_lite import complete


@pytest.fixture
def gateway(monkeypatch):
    # Exercise real LiteLLM serialization against a loopback-only fake provider.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-gateway-key")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            response = json.dumps({
                "id": "fixture-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "  gateway reply  "},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield endpoint, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("explicit_endpoint", [False, True])
def test_utility_request_reaches_gateway(gateway, monkeypatch, explicit_endpoint):
    endpoint, requests = gateway
    monkeypatch.setenv("LLM_BASE_URL", "http://unused.invalid/v1" if explicit_endpoint else endpoint)
    kwargs = {"base_url": endpoint} if explicit_endpoint else {}

    result = complete(
        "test prompt", model="openai/fixture-model", system="test system",
        timeout=5, max_tokens=10, temperature=0.0, **kwargs,
    )

    assert result == "gateway reply"
    assert len(requests) == 1
    path, auth, body = requests[0]
    assert path == "/v1/chat/completions"
    assert auth == "Bearer synthetic-gateway-key"
    assert body["model"] == "fixture-model"
    assert body["messages"] == [
        {"role": "system", "content": "test system"},
        {"role": "user", "content": "test prompt"},
    ]
    assert body["max_tokens"] == 10
    assert body["temperature"] == 0.0


@pytest.mark.parametrize("explicit_key,expected", [
    (None, "synthetic-provider-key"),
    ("synthetic-explicit-key", "synthetic-explicit-key"),
])
def test_existing_key_precedence_with_gateway(gateway, monkeypatch, explicit_key, expected):
    endpoint, requests = gateway
    monkeypatch.setenv("LLM_BASE_URL", endpoint)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider-key")

    assert complete("test", model="openai/fixture-model", api_key=explicit_key, timeout=5)
    assert requests[0][1] == f"Bearer {expected}"


@pytest.mark.parametrize("disable_override", [False, True])
def test_default_endpoint_does_not_receive_gateway_key(monkeypatch, disable_override):
    import litellm

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-private-key")
    if disable_override:
        monkeypatch.setenv("LLM_BASE_URL", "http://gateway.invalid/v1")
    request = Mock(side_effect=RuntimeError("no real network calls"))
    monkeypatch.setattr(litellm, "completion", request)

    kwargs = {"base_url": ""} if disable_override else {}
    assert complete("test", model="openai/fixture-model", **kwargs) == ""
    request.assert_called_once()
    assert "base_url" not in request.call_args.kwargs
    assert request.call_args.kwargs["api_key"] is None


def test_gateway_failure_does_not_retry_on_public_endpoint(monkeypatch):
    import litellm

    monkeypatch.setenv("LLM_BASE_URL", "http://gateway.invalid/v1")
    request = Mock(side_effect=RuntimeError("gateway unavailable"))
    monkeypatch.setattr(litellm, "completion", request)

    assert complete("test", model="openai/fixture-model") == ""
    request.assert_called_once()
    assert request.call_args.kwargs["base_url"] == "http://gateway.invalid/v1"
