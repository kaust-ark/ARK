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
    def test_budget_scales_with_the_model_window(self):
        with patch.object(sr, "_context_window", return_value=200_000), \
             patch.object(sr, "_BUDGET_FRACTION", 0.45), \
             patch.object(sr, "_BUDGET_CEILING", 150_000), \
             patch.object(sr, "_BUDGET_FLOOR", 60_000):
            assert sr.history_token_budget("any/model") == 90_000

    def test_a_huge_window_is_capped_by_the_absolute_ceiling(self):
        """A percentage alone would hand a 1M-token model a budget that never
        binds, which is how the first calibration bought nothing."""
        with patch.object(sr, "_context_window", return_value=1_000_000), \
             patch.object(sr, "_BUDGET_CEILING", 150_000):
            assert sr.history_token_budget("huge/model") == 150_000

    def test_unknown_model_gets_a_workable_budget(self):
        with patch("litellm.get_model_info", side_effect=Exception("unknown")):
            assert sr.history_token_budget("made/up") >= 60_000

    def test_budget_is_never_starvation_tight(self):
        """A 24k budget cost 2.5x MORE than 90k: the agent lost its working
        context and repeated work for hundreds of turns. The floor exists to
        make that configuration unreachable by accident."""
        assert sr._BUDGET_FLOOR >= 60_000

    def test_a_small_window_is_never_handed_more_than_it_holds(self):
        """The floor must not outgrow the model's own context.

        This previously asserted the opposite — floor wins, always — which is
        right for a large model and nonsense for a small one: a 32k local model
        given the 60k floor never reaches the compaction trigger, so history
        grows unchecked until the request overflows the window and every call
        fails. Cap by the window instead, leaving room for the reply.
        """
        with patch.object(sr, "_context_window", return_value=8_000), \
             patch.object(sr, "_BUDGET_FLOOR", 60_000), \
             patch.object(sr, "_WINDOW_SAFETY", 0.8):
            assert sr.history_token_budget("tiny/model") == 6_400

    def test_a_local_32k_model_compacts_inside_its_window(self):
        with patch.object(sr, "_context_window", return_value=32_768), \
             patch.object(sr, "_BUDGET_FLOOR", 60_000), \
             patch.object(sr, "_WINDOW_SAFETY", 0.8):
            budget = sr.history_token_budget("hosted_vllm/qwen2.5-32b-awq")
        assert budget < 32_768, "compaction would never fire"
        assert budget > 20_000, "and it must not be starvation-tight either"

    def test_a_large_window_is_unaffected_by_the_cap(self):
        with patch.object(sr, "_context_window", return_value=200_000), \
             patch.object(sr, "_BUDGET_FRACTION", 0.45), \
             patch.object(sr, "_BUDGET_CEILING", 150_000), \
             patch.object(sr, "_WINDOW_SAFETY", 0.8):
            assert sr.history_token_budget("big/model") == 90_000


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
        stdout = json.dumps({"kind": "MessageEvent", "text": "partial answer",
                             "source": "agent"})
        assert self._runtime().parse_output(stdout)["result"] == "partial answer"

    def test_the_task_and_the_nudge_are_never_the_answer(self):
        """The task prompt and the continue nudge are MessageEvents too. A run
        killed before __result__ returned the prompt as the agent's answer,
        long enough to pass the reviewer's 400-character check."""
        stdout = "\n".join([
            json.dumps({"kind": "MessageEvent", "text": "Please review the paper " + "x" * 500,
                        "source": "user"}),
            json.dumps({"kind": "ActionEvent", "text": "editing", "source": "agent"}),
            json.dumps({"kind": "MessageEvent", "text": "You wrote a plan but executed none of it.",
                        "source": "user"}),
        ])
        assert self._runtime().parse_output(stdout)["result"] == ""

    def test_an_untagged_message_is_not_trusted(self):
        stdout = json.dumps({"kind": "MessageEvent", "text": "who said this?"})
        assert self._runtime().parse_output(stdout)["result"] == ""

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

    def test_rejected_tool_calls_survive_a_successful_run(self):
        """A run with no error code can still have hidden failures.

        This is the false-success path: the agent's own summary says the file
        was written, the run reports success, and the only trace that the write
        was refused is this list.
        """
        stdout = json.dumps({
            "kind": "__result__", "result": "The file has been created.",
            "usage": None, "error_code": None, "error_detail": None,
            "failed_tools": ["str_replace_editor: Ran into [Errno 13] "
                             "Permission denied: '/local_ok.txt'"],
        })
        out = self._runtime().parse_output(stdout)
        assert out["error_code"] is None          # the run "succeeded"
        assert len(out["failed_tools"]) == 1
        assert "Permission denied" in out["failed_tools"][0]

    def test_a_clean_run_reports_no_rejected_calls(self):
        stdout = json.dumps({"kind": "__result__", "result": "done",
                             "usage": None, "error_code": None,
                             "error_detail": None})
        assert self._runtime().parse_output(stdout)["failed_tools"] == []


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


class TestDriverReadsRealSdkFields:
    """The driver names SDK attributes that no unit test can otherwise reach.

    ``sdk_driver`` runs under OpenHands' own interpreter, so its callback is
    invisible to this suite — a renamed field would break silently and the
    failure mode is quiet (rejected tool calls stop being recorded, and the
    false-success guard goes dark without a single error). Assert the names
    against the installed SDK, skipping where that interpreter is absent.
    """

    def test_observation_fields_the_driver_depends_on_still_exist(self):
        import subprocess

        py = sr.openhands_python()
        if not py:
            pytest.skip("no OpenHands interpreter on this machine")
        probe = (
            "from openhands.sdk.event import ObservationEvent;"
            "from openhands.sdk.tool import Observation;"
            "assert 'tool_name' in ObservationEvent.model_fields;"
            "assert 'is_error' in Observation.model_fields;"
            "print('ok')"
        )
        r = subprocess.run([py, "-c", probe], capture_output=True, text=True,
                           timeout=180, env={**os.environ,
                                             "OPENHANDS_SUPPRESS_BANNER": "1"})
        assert "ok" in r.stdout, f"SDK shape changed: {r.stderr[-400:]}"


class TestSandboxIsRecordedPerCall:
    """Confinement must be evidenced per call, not asserted once at startup.

    The pipeline's "sandbox: STRUCTURAL" banner is a capability check made
    before any agent runs — it proves the image exists, not that this call
    entered it. The driver reports the real outcome (including a sandbox that
    failed to start and fell back to the host) and nothing was reading it.
    """

    def _runtime(self):
        return sr.OpenHandsSDK("openrouter/x", "openrouter/x")

    def test_a_sandbox_that_started_is_reported(self):
        stdout = "\n".join([
            json.dumps({"kind": "__sandbox__",
                        "text": "apptainer: agent confined to /img/server.sif"}),
            json.dumps({"kind": "__result__", "result": "ok", "usage": None,
                        "error_code": None, "error_detail": None}),
        ])
        out = self._runtime().parse_output(stdout)
        assert "confined to" in out["sandbox"]

    def test_a_sandbox_that_fell_back_to_the_host_is_reported(self):
        stdout = "\n".join([
            json.dumps({"kind": "__sandbox__",
                        "text": "apptainer sandbox FAILED to start (OSError: no "
                                "such file) — running on the HOST, unconfined"}),
            json.dumps({"kind": "__result__", "result": "ok", "usage": None,
                        "error_code": None, "error_detail": None}),
        ])
        out = self._runtime().parse_output(stdout)
        assert "FAILED" in out["sandbox"] and "unconfined" in out["sandbox"]

    def test_an_unsandboxed_run_reports_nothing_rather_than_claiming_success(self):
        stdout = json.dumps({"kind": "__result__", "result": "ok", "usage": None,
                             "error_code": None, "error_detail": None})
        assert not self._runtime().parse_output(stdout).get("sandbox")


class TestLocalServerWindow:
    """litellm knows no hosted_vllm/ slug, so the window cap meant for small
    local models never applied: they got the 200k fallback and a 90k budget."""

    def _serve(self, payload):
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()
        return patch("urllib.request.urlopen", return_value=_R())

    def setup_method(self):
        sr._SERVED_WINDOWS.clear()

    def test_the_server_reports_the_window(self):
        data = {"data": [{"id": "other", "max_model_len": 8192},
                         {"id": "qwen2.5-32b-awq", "max_model_len": 32768}]}
        with patch.dict(os.environ, {"LLM_BASE_URL": "http://n:8078/v1/"}), self._serve(data):
            assert sr._context_window("hosted_vllm/qwen2.5-32b-awq") == 32768
            budget = sr.history_token_budget("hosted_vllm/qwen2.5-32b-awq")
        assert budget < 32768

    def test_asked_once_per_process(self):
        data = {"data": [{"id": "m", "max_model_len": 16384}]}
        with patch.dict(os.environ, {"LLM_BASE_URL": "http://n:1/v1"}), self._serve(data) as u:
            sr._context_window("hosted_vllm/m"); sr._context_window("hosted_vllm/m")
        assert u.call_count == 1

    def test_an_unreachable_server_keeps_the_fallback(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "http://n:1/v1"}), \
             patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert sr._context_window("hosted_vllm/m") == sr._FALLBACK_CONTEXT_TOKENS

    def test_hosted_models_never_ask(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "http://n:1/v1"}), \
             patch("urllib.request.urlopen") as u:
            sr._context_window("openrouter/some/unknown-model")
        u.assert_not_called()


class TestNudgeOnlyForRolesThatAct:
    """A planner or reviewer answering in text did its job. The nudge told it
    "you executed none of it" and its reply replaced the answer."""

    def _cfg(self, tmp_path, agent_type):
        rt = sr.OpenHandsSDK("openrouter/x", "openrouter/x")
        if agent_type:
            rt.agent_type = agent_type
        with patch.object(sr, "openhands_python", return_value="/usr/bin/python3"), \
             patch.object(sr, "history_token_budget", return_value=90_000):
            cmd = rt.build_command("do it", "stay here", tmp_path)
        try:
            return json.loads(Path(cmd[2]).read_text())
        finally:
            Path(cmd[2]).unlink(missing_ok=True)

    @pytest.mark.parametrize("role", ["planner", "researcher", "reviewer"])
    def test_answer_roles_get_no_nudge(self, tmp_path, role):
        assert self._cfg(tmp_path, role)["nudge"] is False

    @pytest.mark.parametrize("role", ["experimenter", "coder", "writer", None])
    def test_acting_roles_keep_it(self, tmp_path, role):
        assert self._cfg(tmp_path, role)["nudge"] is True

    def test_the_driver_honours_the_flag(self):
        src = (Path(sr.__file__).parent / "sdk_driver.py").read_text()
        assert 'cfg.get("nudge", True) and state["actions"] == 0' in src

    def test_run_agent_names_the_role_on_the_runner(self):
        src = (Path(sr.__file__).parent / "__init__.py").read_text()
        assert "cli_runner = get_cli_for_model(self.model)\n" \
               "                cli_runner.agent_type = agent_type" in src


class TestCliFallbackParsesCliOutput:
    def test_fallback_output_is_read_by_the_cli_parser(self, tmp_path):
        rt = sr.OpenHandsSDK("openrouter/x", "openrouter/x")
        with patch.object(sr, "openhands_python", return_value=None):
            rt.build_command("do it", "stay here", tmp_path)
        cli_out = "\n".join([
            json.dumps({"kind": "MessageEvent", "source": "agent",
                        "llm_message": {"content": [{"type": "text", "text": "all done"}]}}),
            json.dumps({"kind": "ConversationErrorEvent", "code": "AuthenticationError",
                        "detail": "401 bad key"}),
        ])
        out = rt.parse_output(cli_out)
        assert out["result"] == "all done"
        assert out["error_code"] == "AuthenticationError"

    def test_the_sdk_path_keeps_the_driver_parser(self, tmp_path):
        rt = sr.OpenHandsSDK("openrouter/x", "openrouter/x")
        with patch.object(sr, "openhands_python", return_value="/usr/bin/python3"), \
             patch.object(sr, "history_token_budget", return_value=90_000):
            cmd = rt.build_command("do it", "stay here", tmp_path)
        Path(cmd[2]).unlink(missing_ok=True)
        line = json.dumps({"kind": "__result__", "result": "r", "usage": None})
        assert rt.parse_output(line)["result"] == "r"


class TestUsageIsThisConversations:
    """OpenHands names the state directory by the id's hex. The dashed id never
    matched, so usage came from the newest base_state.json in the project."""

    def _state(self, root, name, tokens):
        d = root / name
        d.mkdir(parents=True)
        (d / "base_state.json").write_text(json.dumps({"stats": {"usage_to_metrics": {
            "agent": {"accumulated_cost": 0.01,
                      "accumulated_token_usage": {"prompt_tokens": tokens,
                                                  "completion_tokens": 1}}}}}))

    def test_the_hex_directory_is_found(self, tmp_path):
        from ark.engines import sdk_driver
        cid = "049d3f93-239e-4401-b188-e347cca70e45"
        self._state(tmp_path, cid.replace("-", ""), 111)
        self._state(tmp_path, "ffffffffffffffffffffffffffffffff", 999)  # newer, someone else's
        u = sdk_driver._collect_usage(str(tmp_path), cid, "m")
        assert u["input_tokens"] == 111

    def test_no_state_of_its_own_means_no_usage(self, tmp_path):
        from ark.engines import sdk_driver
        self._state(tmp_path, "ffffffffffffffffffffffffffffffff", 999)
        assert sdk_driver._collect_usage(
            str(tmp_path), "049d3f93-239e-4401-b188-e347cca70e45", "m") is None
