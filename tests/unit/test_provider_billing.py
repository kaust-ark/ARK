"""openrouter_key_usage_usd: authoritative billed usage; fail-open on anything."""

import json
from unittest.mock import patch, MagicMock

from ark.provider_billing import openrouter_key_usage_usd


def _resp(payload: dict):
    m = MagicMock()
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: False
    m.read.return_value = json.dumps(payload).encode()
    return m


def test_returns_usage():
    with patch("urllib.request.urlopen",
               return_value=_resp({"data": {"usage": 12.3456}})):
        assert openrouter_key_usage_usd("k") == 12.3456


def test_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert openrouter_key_usage_usd() is None


def test_network_error_fails_open():
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert openrouter_key_usage_usd("k") is None


def test_schema_drift_fails_open():
    with patch("urllib.request.urlopen", return_value=_resp({"data": {}})):
        assert openrouter_key_usage_usd("k") is None
