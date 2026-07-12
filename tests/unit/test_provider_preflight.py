"""_provider_preflight: block only on CONFIRMED insufficient balance; fail-open
on every uncertainty (no key / network error / schema drift)."""

import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException

from website.dashboard.routes import _provider_preflight


def _resp(payload: dict):
    m = MagicMock()
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: False
    m.read.return_value = json.dumps(payload).encode()
    return m


def test_no_openrouter_key_is_noop():
    _provider_preflight({})           # no raise
    _provider_preflight({"deepseek": "x"})


def test_confirmed_low_balance_blocks():
    with patch("urllib.request.urlopen",
               return_value=_resp({"data": {"total_credits": 5.0, "total_usage": 4.5}})):
        with pytest.raises(HTTPException) as e:
            _provider_preflight({"openrouter": "k"})
        assert "0.50" in e.value.detail


def test_sufficient_balance_allows():
    with patch("urllib.request.urlopen",
               return_value=_resp({"data": {"total_credits": 20.0, "total_usage": 1.0}})):
        _provider_preflight({"openrouter": "k"})


def test_network_error_fails_open():
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        _provider_preflight({"openrouter": "k"})


def test_schema_drift_fails_open():
    with patch("urllib.request.urlopen", return_value=_resp({"data": {}})):
        _provider_preflight({"openrouter": "k"})
