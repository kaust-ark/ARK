"""The agent runtime that gives history a size budget.

OpenHands' headless CLI hardcodes
``LLMSummarizingCondenser(max_size=80, keep_first=4)`` and never sets
``max_tokens``, so the only live compaction trigger counts EVENTS and is
blind to their size. Eighty small events are nothing; eighty 33 KB file
observations are millions of tokens. Measured: 12.5M input tokens for 146k
of output on a 2-page paper. This runtime assembles the same agent with a
token budget instead.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ark.engines import sdk_runtime as sr
from ark.engines.cli import OpenHandsCLI, get_cli_for_model


class TestRuntimeSelection:
    def test_cli_is_the_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARK_AGENT_RUNTIME", None)
            assert type(get_cli_for_model("m")) is OpenHandsCLI

    def test_flag_selects_the_sdk_runtime(self):
        with patch.dict(os.environ, {"ARK_AGENT_RUNTIME": "sdk"}):
            assert type(get_cli_for_model("m")) is sr.OpenHandsSDK

    def test_selection_is_case_and_space_tolerant(self):
        with patch.dict(os.environ, {"ARK_AGENT_RUNTIME": " SDK "}):
            assert sr.sdk_runtime_enabled() is True

    def test_a_broken_sdk_module_never_breaks_the_agent(self):
        """The budget is an optimisation; it must not be able to stop a run."""
        with patch.dict(os.environ, {"ARK_AGENT_RUNTIME": "sdk"}), \
             patch("ark.engines.sdk_runtime.sdk_runtime_enabled",
                   side_effect=RuntimeError("boom")):
            assert type(get_cli_for_model("m")) is OpenHandsCLI


class TestBudget:
    def test_budget_is_a_fraction_of_the_model_window(self):
        with patch.object(sr, "_context_window", return_value=200_000), \
             patch.object(sr, "_BUDGET_FRACTION", 0.45):
            assert sr.history_token_budget("any/model") == 90_000

    def test_unknown_model_gets_a_conservative_budget(self):
        with patch("litellm.get_model_info", side_effect=Exception("unknown")):
            assert sr.history_token_budget("made/up") >= 20_000

    def test_a_tiny_window_still_leaves_room_to_work(self):
        with patch.object(sr, "_context_window", return_value=8_000):
            assert sr.history_token_budget("tiny/model") == 20_000


class TestOutputContract:
    """parse_output must return exactly what run_agent already expects."""

    def _runtime(self):
        return sr.OpenHandsSDK("openrouter/x", "openrouter/x")

    def test_result_and_usage_come_from_the_result_line(self):
        stdout = "\n".join([
            json.dumps({"kind": "ActionEvent", "text": "editing"}),
            json.dumps({"kind": "__result__", "result": "done it",
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                        "error_code": None, "error_detail": None}),
        ])
        out = self._runtime().parse_output(stdout)
        assert out["result"] == "done it"
        assert out["usage"]["input_tokens"] == 10
        assert out["error_code"] is None

    def test_last_agent_message_is_the_fallback(self):
        stdout = json.dumps({"kind": "MessageEvent", "text": "partial answer"})
        assert self._runtime().parse_output(stdout)["result"] == "partial answer"

    def test_errors_are_surfaced_for_the_terminal_classifier(self):
        stdout = json.dumps({"kind": "__result__", "result": "", "usage": None,
                             "error_code": "AuthenticationError",
                             "error_detail": "bad key"})
        out = self._runtime().parse_output(stdout)
        assert out["error_code"] == "AuthenticationError"

    def test_garbage_lines_are_survived(self):
        out = self._runtime().parse_output("not json\n{broken\n")
        assert out == {"result": "", "usage": None,
                       "error_code": None, "error_detail": None}


class TestCommandBuilding:
    def test_config_carries_the_token_budget_and_is_not_world_readable(self, tmp_path):
        rt = sr.OpenHandsSDK("openrouter/x", "openrouter/x")
        with patch.object(sr, "openhands_python", return_value="/usr/bin/python3"), \
             patch.object(sr, "history_token_budget", return_value=90_000):
            cmd = rt.build_command("do it", "stay here", tmp_path)
        cfg_path = Path(cmd[2])
        try:
            cfg = json.loads(cfg_path.read_text())
            assert cfg["max_tokens"] == 90_000        # the whole point
            assert "stay here" in cfg["task"]
            assert oct(cfg_path.stat().st_mode)[-3:] == "600"  # holds an API key
        finally:
            cfg_path.unlink(missing_ok=True)

    def test_falls_back_to_the_stock_cli_without_an_sdk_interpreter(self, tmp_path):
        rt = sr.OpenHandsSDK("openrouter/x", "openrouter/x")
        with patch.object(sr, "openhands_python", return_value=None):
            cmd = rt.build_command("do it", "stay here", tmp_path)
        assert cmd[0] == "openhands"
