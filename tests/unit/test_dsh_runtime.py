"""DSH boundary contracts, plus optional real-SDK tests using a loopback API."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ark.engines import _add_usage, _is_transient_agent_error
from ark.engines.cli import AgentCLI, OpenHandsCLI, get_cli_for_model
from ark.engines.dsh_runtime import DSHRuntime, dsh_installation_error
from ark.observability.steps import parse_line


def event(kind, data, seq=1, sid="fixture"):
    return {"kind": "DSHEvent", "method": "session.event", "payload": {
        "sessionId": sid, "event": {"type": kind, "seq": seq, "data": data}}}


def test_explicit_dsh_selection(monkeypatch):
    monkeypatch.setenv("ARK_AGENT_RUNTIME", " DSH ")
    assert isinstance(get_cli_for_model("openai/fixture"), DSHRuntime)


def test_default_dsh_and_explicit_openhands(monkeypatch):
    monkeypatch.delenv("ARK_AGENT_RUNTIME", raising=False)
    assert isinstance(get_cli_for_model("openai/fixture"), DSHRuntime)
    monkeypatch.setenv("ARK_AGENT_RUNTIME", "cli")
    assert type(get_cli_for_model("openai/fixture")) is OpenHandsCLI


@pytest.mark.parametrize("status", [402, 429])
def test_structured_quota_is_terminal(status):
    assert not _is_transient_agent_error(
        "DSH.QuotaExceeded", f"QUOTA (HTTP {status}): exceeded your current quota")


def test_partial_output_is_not_success_and_retains_usage():
    record = event("assistant/message", {"message": {"content": [{"type": "text", "text": "done"}]},
                                         "usage": {"inputTokens": 10, "outputTokens": 5}})
    parsed = DSHRuntime("openai/fixture").parse_output(json.dumps(record))
    assert parsed["error_code"] == "DSH.ServiceUnavailableError"
    assert parsed["result"] == ""
    assert parsed["usage"]["input_tokens"] == 10
    assert parsed["usage"]["cost_usd"] is None


def test_usage_dedup_and_tool_failure_visibility():
    records = [event("assistant/message", {"usage": {"inputTokens": 10}})]
    records += records[:]
    records.append(event("tool/result", {"message": {"isError": True, "content": [
        {"type": "text", "text": "Permission denied"}]}}, seq=2))
    parsed = DSHRuntime("openai/fixture").parse_output("\n".join(map(json.dumps, records)))
    assert parsed["usage"]["input_tokens"] == 10
    assert parsed["failed_tools"] == ["Permission denied"]
    assert parse_line(json.dumps(records[-1])).type == "error"


def test_tool_command_reaches_existing_circuit_breaker():
    record = event("tool/call", {"name": "bash", "arguments": '{"command":"rm -rf /tmp/example"}'})
    step = parse_line(json.dumps(record))
    assert step.type == "command"
    assert step.detail == "rm -rf /tmp/example"


def test_unknown_cost_stays_unknown_across_attempts():
    assert _add_usage({"cost_usd": 1.0}, {"cost_usd": None})["cost_usd"] is None
    assert _add_usage({"cost_usd": None}, {"cost_usd": 1.0})["cost_usd"] is None


def test_nonzero_exit_cannot_keep_a_successful_final_record(tmp_path, monkeypatch):
    monkeypatch.setattr(AgentCLI, "execute", lambda *a, **k: (
        1, json.dumps({"kind": "__result__", "result": "done"}), "", 0, False))
    runner = DSHRuntime("openai/fixture")
    _, out, _, _, _ = runner.execute("test", "boundary", tmp_path, 10)
    assert runner.parse_output(out)["error_code"] == "DSH.ServiceUnavailableError"
    assert runner.parse_output(out)["result"] == ""


def test_dsh_environment_only_contains_selected_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-selected")
    for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "ARK_GITHUB_PAT", "GH_TOKEN",
              "AWS_ACCESS_KEY_ID", "DATABASE_URL"):
        monkeypatch.setenv(k, "synthetic-unrelated")
    monkeypatch.setenv("DSH_HOME", "/unrelated/personal/home")
    env = DSHRuntime("openai/fixture").build_env()
    assert env["ARK_DSH_API_KEY"] == "synthetic-selected"
    assert "synthetic-unrelated" not in env.values()
    assert "DSH_HOME" not in env
    assert "OPENAI_API_KEY" not in env


def test_invalid_gateway_is_terminal_without_spawn(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ARK_DSH_API", raising=False)
    monkeypatch.setattr("ark.engines.cli.subprocess.Popen",
                        lambda *a, **k: pytest.fail("invalid route spawned a process"))
    runner = DSHRuntime("hosted_vllm/fixture")
    rc, out, _, _, _ = runner.execute("test", "test boundary", tmp_path, 2)
    assert rc == 1
    assert runner.parse_output(out)["error_code"] == "DSHConfigurationError"


def test_generic_gateway_key_requires_a_gateway(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-gateway")
    runner = DSHRuntime("openai/fixture")
    assert "ARK_DSH_API_KEY" not in runner.build_env()
    monkeypatch.setenv("LLM_BASE_URL", "http://gateway.invalid/v1")
    assert runner.build_env()["ARK_DSH_API_KEY"] == "synthetic-gateway"

def test_providers_the_webapp_offers_route_without_a_base_url(monkeypatch):
    # The webapp sets no LLM_BASE_URL; OpenHands leaves the endpoint to litellm.
    from ark.engines.dsh_runtime import route
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ARK_DSH_API", raising=False)
    # Only the OpenRouter route is verified against a live endpoint; the other
    # three are pi-ai's documented protocol for that provider.
    assert route("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free") == (
        "openai-completions", "https://openrouter.ai/api/v1")
    assert route("anthropic/claude-sonnet-5-5") == (
        "anthropic-messages", "https://api.anthropic.com")
    assert route("gemini/gemini-2.5-flash") == (
        "openai-completions", "https://generativelanguage.googleapis.com/v1beta/openai")
    assert route("deepseek/deepseek-v4-pro") == ("", "")
    # A provider with no route says which variable to set, instead of failing
    # the first agent call minutes into the run.
    with pytest.raises(ValueError, match="ARK_DSH_API"):
        route("hosted_vllm/qwen3-32b")
    with pytest.raises(ValueError, match="provider/model"):
        route("bare-model-name")
    monkeypatch.setenv("LLM_BASE_URL", "http://gw.invalid/v1")
    assert route("openrouter/vendor/m") == ("openai-completions", "http://gw.invalid/v1")
    assert route("deepseek/deepseek-v4-pro") == ("", "http://gw.invalid/v1")
    monkeypatch.setenv("ARK_DSH_API", "openai-completions")
    assert route("hosted_vllm/qwen3-32b") == ("openai-completions", "http://gw.invalid/v1")


def test_openrouter_models_get_the_window_and_output_cap_openrouter_lists(monkeypatch):
    import ark.engines.dsh_runtime as dr
    monkeypatch.delenv("ARK_DSH_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("ARK_DSH_MAX_TOKENS", raising=False)
    monkeypatch.setattr(dr, "_LISTED", {"small/m:free": (65536, 8192), "big/m": (1_000_000, 65536)})
    assert dr._window("openrouter/small/m:free") == 65536
    assert dr._max_tokens("openrouter/small/m:free") == 8192
    assert dr._max_tokens("openrouter/big/m") == 16384
    monkeypatch.setenv("ARK_DSH_CONTEXT_WINDOW", "131072")
    assert dr._window("openrouter/small/m:free") == 131072


def test_sandbox_request_is_not_silently_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_AGENT_SANDBOX", "apptainer")
    runner = DSHRuntime("openai/fixture")
    _, out, _, _, _ = runner.execute("test", "test boundary", tmp_path, 2)
    assert "Apptainer" in runner.parse_output(out)["error_detail"]


def test_missing_sdk_is_terminal_and_never_uses_other_runtime(tmp_path, monkeypatch):
    interpreter = tmp_path / "without-site-packages"
    interpreter.write_text(f"#!{sys.executable}\nimport os, sys\nos.execv(sys.executable, [sys.executable, '-S', *sys.argv[1:]])\n")
    interpreter.chmod(0o700)
    monkeypatch.setenv("ARK_DSH_PYTHON", str(interpreter))
    monkeypatch.setenv("LLM_BASE_URL", "http://unused.invalid/v1")
    monkeypatch.delenv("ARK_DSH_API", raising=False)
    monkeypatch.delenv("ARK_AGENT_SANDBOX", raising=False)
    runner = DSHRuntime("openai/fixture")
    rc, out, _, _, _ = runner.execute("test", "boundary", tmp_path, 10)
    assert rc == 1
    assert runner.parse_output(out)["error_code"] == "DSHConfigurationError"
    assert "ARK_DSH_PYTHON" in runner.parse_output(out)["error_detail"]


@pytest.fixture
def real_gateway(monkeypatch):
    python = os.environ.get("ARK_DSH_TEST_PYTHON") or sys.executable
    monkeypatch.setenv("ARK_DSH_PYTHON", python)
    if error := dsh_installation_error():
        pytest.skip(error)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-gateway-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-gateway-key")
    monkeypatch.setenv("ARK_DSH_MAX_TOKENS", "1024")
    monkeypatch.setenv("ARK_DSH_RETRIES", "0")
    monkeypatch.setenv("ARK_DSH_CONTEXT_WINDOW", "32768")
    monkeypatch.delenv("ARK_DSH_API", raising=False)
    monkeypatch.delenv("ARK_AGENT_SANDBOX", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    state = {"requests": [], "mode": "success", "command": "printf verified > fixture.txt",
             "background": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "body": body,
                "auth": self.headers.get("Authorization") or "Bearer " + self.headers.get("x-api-key", "")})
            if state["mode"] == "balance":
                payload = json.dumps({"error": {"message": "Insufficient Balance",
                                                "type": "insufficient_balance_error"}}).encode()
                self.send_response(402)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            assert len(state["requests"]) <= 3, "unexpected repeat model call"
            # The sdk profile's bash requires a description; a background run
            # first ends its turn, then answers in the turn the job wakes.
            tool_name = state.get("tool_name", "bash")
            args = state.get("tool_args") or {"command": state["command"], "description": "fixture"}
            if state["background"]:
                args["run_in_background"] = True
            if len(state["requests"]) == 1:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call-fixture",
                    "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}]}
                reason = "tool_calls"
            elif state["background"] and len(state["requests"]) == 2:
                delta = {"role": "assistant", "content": "Started; waiting for the job."}
                reason = "stop"
            else:
                delta = {"role": "assistant", "content": "Fixture complete."}
                reason = "stop"
            chunks = [
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            ]
            if urlsplit(self.path).path.endswith("/messages"):
                tool = reason == "tool_calls"
                chunks = [
                    {"type": "message_start", "message": {"id": f"fixture-{len(state['requests'])}",
                        "type": "message", "role": "assistant", "model": "fixture", "content": [],
                        "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
                    {"type": "content_block_start", "index": 0, "content_block":
                        {"type": "tool_use", "id": "call-fixture", "name": tool_name, "input": {}} if tool
                        else {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta":
                        {"type": "input_json_delta", "partial_json": json.dumps(args)} if tool
                        else {"type": "text_delta", "text": delta["content"]}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn",
                        "stop_sequence": None}, "usage": {"output_tokens": 5}},
                    {"type": "message_stop"},
                ]
                payload = "".join("event: " + c["type"] + "\ndata: " + json.dumps(c) + "\n\n" for c in chunks).encode()
            else:
                wire = ""
                for chunk in chunks:
                    chunk.update(id=f"fixture-{len(state['requests'])}", object="chat.completion.chunk",
                                 created=0, model="fixture")
                    wire += "data: " + json.dumps(chunk) + "\n\n"
                payload = (wire + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("provider,protocol", [("openai", None), ("deepseek", None), ("anthropic", "anthropic-messages")])
def test_real_sdk_writes_artifact_and_restores_history(real_gateway, tmp_path, monkeypatch, provider, protocol):
    if protocol:
        monkeypatch.setenv("ARK_DSH_API", protocol)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-gateway-key")
        # Anthropic's client appends /v1/messages to its root base URL.
        monkeypatch.setenv("LLM_BASE_URL", os.environ["LLM_BASE_URL"].removesuffix("/v1"))
    monkeypatch.setenv("DATABASE_URL", "synthetic-unrelated")
    real_gateway["command"] = (
        'test -z "${ARK_DSH_API_KEY+x}${DEEPSEEK_API_KEY+x}${DATABASE_URL+x}" '
        '&& printf verified > fixture.txt'
    )
    runner = DSHRuntime(f"{provider}/fixture")
    runner.session_checkpoint = tmp_path / "dsh-session"
    # The first runtime start in a process can take ~30 s (module cache checks).
    rc, out, err, _, timed_out = runner.execute("Create fixture.txt then report completion.",
                                               "Stay in this workspace.", tmp_path, 90)
    parsed = runner.parse_output(out)
    assert rc == 0, (parsed, err[-2000:])
    assert runner.session_checkpoint.read_text().strip() == runner.session_id
    assert not timed_out
    assert (tmp_path / "fixture.txt").read_text() == "verified"
    assert parsed["result"] == "Fixture complete."
    assert parsed["usage"]["input_tokens"] == 20
    assert parsed["usage"]["cost_usd"] is None
    assert len(real_gateway["requests"]) == 2
    for request in real_gateway["requests"]:
        assert urlsplit(request["path"]).path == ("/v1/chat/completions" if provider == "openai" else "/v1/messages")
        assert request["auth"] == "Bearer synthetic-gateway-key"
        assert "dsh_session_log" not in json.dumps(request["body"])
    run_dir = tmp_path / ".dsh" / runner.session_id
    assert not (run_dir / "request.json").exists()
    assert (run_dir / "run.json").stat().st_mode & 0o777 == 0o600
    for file in run_dir.rglob("*"):
        if file.is_file():
            assert b"synthetic-gateway-key" not in file.read_bytes()

    resumed = DSHRuntime(f"{provider}/fixture", resume_session_id=runner.session_id)
    resumed.resume_instructions = "Do not resubmit a job whose results already exist."
    rc, out, err, _, _ = resumed.execute("Report the earlier result.", "Stay here.", tmp_path, 25)
    assert rc == 0, (resumed.parse_output(out), err[-2000:])
    messages = real_gateway["requests"][-1]["body"]["messages"]
    assert any(m.get("role") == "tool" for m in messages) or "tool_result" in json.dumps(messages)
    assert resumed.resume_instructions in json.dumps(messages)


def test_real_sdk_sends_rendered_page_to_vision_model(real_gateway, tmp_path, monkeypatch):
    from PIL import Image

    Image.new("RGB", (2, 2), "white").save(tmp_path / "page.png")
    monkeypatch.setattr("ark.engines.dsh_runtime.supports_images", lambda model: True)
    real_gateway.update(tool_name="read_image", tool_args={"file_path": "page.png"})
    runner = DSHRuntime("openai/fixture")
    runner.agent_type = "reviewer"
    rc, out, err, _, _ = runner.execute("Review page.png.", "", tmp_path, 60)
    assert rc == 0, (runner.parse_output(out), err[-2000:])
    messages = real_gateway["requests"][-1]["body"]["messages"]
    assert "data:image/png;base64," in json.dumps(messages)


def test_real_sdk_balance_failure_is_terminal(real_gateway, tmp_path):
    real_gateway["mode"] = "balance"
    runner = DSHRuntime("openai/fixture")
    rc, out, _, _, _ = runner.execute("Do the task.", "Stay here.", tmp_path, 25)
    parsed = runner.parse_output(out)
    assert rc == 1
    assert parsed["error_code"]
    assert "Insufficient Balance" in parsed["error_detail"]
    assert len(real_gateway["requests"]) == 1


def test_real_sdk_timeout_stops_owned_shell(real_gateway, tmp_path):
    real_gateway["command"] = "echo $$ > child.pid; sleep 60"
    runner = DSHRuntime("openai/fixture")
    rc, out, _, _, timed_out = runner.execute("Run the command.", "Stay here.", tmp_path, 15)
    # The driver stops itself before the hard kill and reports a used budget:
    # keep the work, no retry. Either way the owned shell must be gone.
    assert runner.parse_output(out)["error_code"] == "DSH.MaxIterationsReached"
    assert len(real_gateway["requests"]) == 1
    pid = int((tmp_path / "child.pid").read_text())
    proc = Path(f"/proc/{pid}/stat")
    assert not proc.exists() or proc.read_text().split()[2] == "Z"


def test_real_sdk_intervention_abort_is_cancelled(real_gateway, tmp_path):
    def stop_before_action(line):
        try:
            item = json.loads(line)
        except ValueError:
            return None
        if item.get("payload", {}).get("event", {}).get("type") == "tool/call":
            return "ABORT"

    runner = DSHRuntime("openai/fixture")
    rc, out, _, _, timed_out = runner.execute("Do the task.", "Stay here.", tmp_path, 20,
                                              on_event=stop_before_action)
    assert not timed_out
    assert rc != 0
    parsed = runner.parse_output(out)
    assert parsed["error_code"] == "DSH.Cancelled"
    # SIGTERM first: the driver closed the session and reported for itself.
    assert parsed["error_detail"] == "DSH execution was cancelled"
    assert len(real_gateway["requests"]) == 1


def test_real_sdk_background_job_is_waited_for(real_gateway, tmp_path):
    """Session.run() returns at the first idle; the job it started must not die
    with the harness, and the turn its completion wakes must be the answer."""
    real_gateway["background"] = True
    real_gateway["command"] = "sleep 3; printf woke > bg.txt"
    runner = DSHRuntime("openai/fixture")
    rc, out, err, _, _ = runner.execute("Start the job.", "Stay here.", tmp_path, 90)
    parsed = runner.parse_output(out)
    assert rc == 0, (parsed, err[-2000:])
    assert (tmp_path / "bg.txt").read_text() == "woke"
    assert parsed["result"] == "Fixture complete."
    assert parsed["open_jobs"] == [] and parsed["stop_reason"] == "quiescent"
    assert len(real_gateway["requests"]) == 3


# ── outcome mapping, profile and retry behaviour (no SDK needed) ────────────

from ark.engines.dsh_driver import classify  # noqa: E402
from ark.engines.dsh_profile import compaction, effective_window, role_patch  # noqa: E402


@pytest.mark.parametrize("reason,code", [
    ({"kind": "completed"}, None),
    ({"kind": "max-tokens"}, "DSH.MaxIterationsReached"),
    ({"kind": "error", "error": {"code": "RATE_LIMIT", "status": 429}}, "DSH.RateLimitError"),
    ({"kind": "error", "error": {"code": "SERVER", "status": 503}}, "DSH.ServiceUnavailableError"),
    ({"kind": "error", "error": {"code": "TRANSPORT"}}, "DSH.ServiceUnavailableError"),
    ({"kind": "error", "error": {"code": "CONTEXT_WINDOW_EXCEEDED"}}, "DSH.MaxIterationsReached"),
    ({"kind": "error", "error": {"code": "QUOTA", "status": 402}}, "DSH.QuotaExceeded"),
    ({"kind": "error", "error": {"code": "AUTH", "status": 401}}, "DSH.AUTH"),
    ({"kind": "aborted", "reason": {"kind": "host"}}, "DSH.Cancelled"),
])
def test_turn_endings_map_onto_arks_existing_classes(reason, code):
    from ark.engines import _is_transient_agent_error, _used_its_step_budget, _was_cancelled
    got, detail = classify(reason)
    assert got == code
    if code in ("DSH.RateLimitError", "DSH.ServiceUnavailableError"):
        assert _is_transient_agent_error(got, detail)
    if code == "DSH.MaxIterationsReached":
        assert _used_its_step_budget(got, detail) and not _is_transient_agent_error(got, detail)
    if code in ("DSH.QuotaExceeded", "DSH.AUTH"):
        assert not _is_transient_agent_error(got, detail) and not _used_its_step_budget(got, detail)
    if code == "DSH.Cancelled":
        assert _was_cancelled(got)


@pytest.mark.parametrize("window,max_tokens", [(131072, 16384), (1_000_000, 16384), (32768, 1024)])
def test_compaction_always_has_a_pressure_budget(window, max_tokens):
    c = compaction(window, max_tokens, 90_000)
    threshold = int(window * c["thresholdRatio"])
    assert 0 < c["retainTokens"] < threshold <= window - max_tokens - c["headroomTokens"]
    assert threshold <= 90_000


def test_catalogued_native_models_compact_near_the_budget_not_at_800k():
    w = effective_window(True, "deepseek-v4-pro", 131072)
    assert w == 1_000_000
    assert int(w * compaction(w, 16384, 90_000)["thresholdRatio"]) <= 90_000
    assert effective_window(False, "deepseek-v4-pro", 131072) == 131072


def test_role_patch_switches_off_phone_home_and_injection(tmp_path):
    rows = role_patch(role="writer", workdir=tmp_path, provider="deepseek-official",
                      model_id="deepseek-v4-pro", window=131072, max_tokens=16384,
                      history_budget=90_000, sandbox="workspace-write")
    off = {r["id"] for r in rows if r.get("disabled")}
    assert {"session-log-deepseek", "plugin-package-inventory-deepseek", "session-telemetry-otel",
            "agent-instructions", "tool-web", "tool-subagent"} <= off
    by_id = {r["id"]: r for r in rows if "id" in r}
    assert by_id["sandbox-policy"]["config"] == {"mode": "workspace-write", "workspaceRoot": str(tmp_path)}
    assert by_id["llm-deepseek"]["config"]["streamIdleTimeoutMs"] == 600_000
    assert "writer" in by_id["system-prompt"]["config"]["personaPrefix"]
    with pytest.raises(ValueError):
        role_patch(role="w", workdir=tmp_path, provider="deepseek-official", model_id="m",
                   window=131072, max_tokens=1024, history_budget=1, sandbox="yolo")


def test_a_resumed_call_sends_a_recovery_note_not_the_whole_task(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("ARK_DSH_CONTEXT_WINDOW", "131072")
    first = DSHRuntime("openai/fixture")
    first.build_command("THE FULL TASK", "boundary", tmp_path)
    again = DSHRuntime("openai/fixture", resume_session_id=first.session_id,
                       resume_reason="DSH.ServiceUnavailableError: 503")
    again.build_command("THE FULL TASK\n\n## Time limit\nstop at 12:00", "boundary", tmp_path)
    task = json.loads(again._config_path.read_text())["task"]
    assert "THE FULL TASK" not in task and "[Resumed]" in task and "503" in task
    assert "## Time limit" in task
    assert json.loads(again._config_path.read_text())["resume"] is True


class TestRunAgentWithDSHOutcomes:
    """run_agent decides retry, keep-work or abort from the driver's code."""

    def test_fix_rounds_continue_one_session(self, tmp_path):
        chain = []
        self._run(tmp_path, [(None, "fixed brace")], chain=chain)
        assert chain == ["a" * 32]
        _, made, _ = self._run(tmp_path, [(None, "fixed ref")], chain=chain)
        assert made[0].kw == {"resume_session_id": "a" * 32, "next_turn": True}

    def _run(self, tmp_path, outcomes, chain=None, **kw):
        from types import SimpleNamespace
        from unittest import mock
        import ark
        import ark.engines as eng
        made = []

        class Runner:
            supports_resume = True

            def __init__(self, **kw):
                self.kw, self.session_id = kw, "a" * 32
                made.append(self)

            def execute(self, **kw):
                return 0, "", "", 1, False

            def parse_output(self, stdout):
                code, text = outcomes[len(made) - 1]
                return {"result": text, "usage": None, "error_code": code, "error_detail": "x"}

        (tmp_path / "experimenter.prompt").write_text("You run experiments.")
        o = eng.AgentMixin()
        noop = lambda *a, **k: None
        for name, val in dict(
                agents_dir=tmp_path, state_dir=tmp_path, code_dir=tmp_path, log_dir=tmp_path,
                model="deepseek/deepseek-v4-pro",
                memory=SimpleNamespace(get_context_for_agent=lambda a: ""),
                _agent_stats=[], _agent_empty_count=0, _rate_limit_notified=False,
                _terminal_error=None, log=noop, log_step=noop, log_summary_box=noop,
                send_notification=noop, save_checkpoint=noop, _write_cost_report=noop,
                _cleanup_cli_state=noop, _build_path_boundary=lambda: "",
                _summarize_agent_output=lambda a, r: []).items():
            setattr(o, name, val)
        with mock.patch.object(eng, "get_cli_for_model", lambda m, **kw: Runner(**kw)), \
                mock.patch.object(eng, "RateLimitCountdown", lambda s: SimpleNamespace(run=noop)), \
                mock.patch.object(ark.shutting_down, "wait", lambda t=None: False):
            out = (o.run_agent_chain(chain, "experimenter", "fix it", timeout=600) if chain is not None
                   else o.run_agent("experimenter", "run E1", timeout=600, **kw))
        return out, made, o

    def test_a_transient_failure_resumes_the_same_session(self, tmp_path):
        out, made, o = self._run(tmp_path, [("DSH.ServiceUnavailableError", ""), (None, "done")])
        assert out == "done" and len(made) == 2
        assert made[0].kw == {}
        assert made[1].kw["resume_session_id"] == "a" * 32
        assert o._terminal_error is None

    def test_a_breaker_cancel_keeps_the_work_and_does_not_retry(self, tmp_path):
        out, made, o = self._run(tmp_path, [("DSH.Cancelled", "partial")])
        assert len(made) == 1 and o._terminal_error is None

    def test_a_used_budget_keeps_the_work_and_does_not_retry(self, tmp_path):
        out, made, o = self._run(tmp_path, [("DSH.MaxIterationsReached", "half done")])
        assert len(made) == 1 and o._terminal_error is None and out == "half done"

    def test_a_call_hands_back_its_session_and_a_later_one_continues_it(self, tmp_path):
        got = []
        self._run(tmp_path, [(None, "submitted")], session_out=got)
        assert got == ["a" * 32]
        _, made, _ = self._run(tmp_path, [(None, "collected")], continue_session="c" * 32)
        assert made[0].kw == {"resume_session_id": "c" * 32, "next_turn": True}

    def test_an_exhausted_balance_ends_the_run(self, tmp_path):
        out, made, o = self._run(tmp_path, [("DSH.QuotaExceeded", "")])
        assert len(made) == 1 and o._terminal_error.startswith("DSH.QuotaExceeded")


def test_dsh_read_is_logged_as_a_read_not_an_edit():
    step = parse_line(json.dumps(event("tool/call", {"name": "read", "arguments": '{"path": "paper/main.tex"}'})))
    assert step.type == "read" and step.detail == "paper/main.tex"
    step = parse_line(json.dumps(event("tool/call", {"name": "edit", "arguments": {"path": "a.tex"}})))
    assert step.type == "edit"


def test_dsh_usage_is_priced_explicitly_or_left_unknown(monkeypatch):
    from ark.engines.dsh_runtime import price
    usage = {"input_tokens": 1_000_000, "cache_read_tokens": 2_000_000, "output_tokens": 100_000}
    monkeypatch.setenv("ARK_DSH_PRICE_PER_M", "0.137,0.00274,0.548")
    cost, source = price("openai/ext/deepseek-v4.1-flash", usage)
    assert source == "ARK_DSH_PRICE_PER_M"
    assert cost == pytest.approx(0.137 + 2 * 0.00274 + 0.0548)
    monkeypatch.delenv("ARK_DSH_PRICE_PER_M")
    assert price("openai/no-such-model-anywhere", usage) == (None, None)
    # A free run used to read "cost unknown" on every call: litellm knows no
    # ':free' id, and the answer for one is not unknown.
    assert price("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", usage) == (0.0, "free tier")


def test_parsed_usage_carries_the_price(monkeypatch):
    monkeypatch.setenv("ARK_DSH_PRICE_PER_M", "1,0,0")
    record = event("assistant/message", {"usage": {"inputTokens": 500_000}})
    parsed = DSHRuntime("openai/fixture").parse_output(json.dumps(record))
    assert parsed["usage"]["cost_usd"] == pytest.approx(0.5)
    assert parsed["usage"]["priced_by"] == "ARK_DSH_PRICE_PER_M"


def test_a_stopped_session_still_reports_what_its_requests_cost():
    """What `_record_stopped_call` actually parses: a stream of events and no
    final result, because the Stop ended the driver before it printed one.
    E3-S1 lost the usage of 5 billed requests this way."""
    stream = "\n".join(json.dumps(event("assistant/message", {"usage": {
        "inputTokens": 30_000, "outputTokens": 900, "cacheReadTokens": 21_000}}, seq=n))
        for n in (1, 2, 3))
    out = DSHRuntime("openrouter/vendor/m:free").parse_output(stream)
    assert out["usage"]["requests"] == 3
    assert out["usage"]["input_tokens"] == 90_000
    assert out["usage"]["cost_usd"] == 0.0 and out["usage"]["priced_by"] == "free tier"
    # Nothing reported a result, so the call is not a success.
    assert out["error_code"] == "DSH.ServiceUnavailableError"


def test_one_unreachable_moment_does_not_pin_the_window_for_the_run(monkeypatch):
    """`_listed_limits` cached its own failure as `_LISTED[None]`.

    A single unreachable moment put a sentinel in the cache that made `if not
    _LISTED` false forever, so every later call skipped the fetch and every
    model fell back to litellm's window and max_tokens for the rest of the
    process. Only an answer is cacheable — the rule this PR applies in
    `sdk_runtime._served_window`.
    """
    import json as _json
    from ark.engines import dsh_runtime as dr

    dr._LISTED.clear()
    calls = []

    class _Resp:
        def __init__(self, payload): self._p = payload
        def read(self): return _json.dumps(self._p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def flaky(url, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("connection reset")
        # the catalogue lists ids without the `openrouter/` prefix
        return _Resp({"data": [{"id": "m", "context_length": 123456,
                                "top_provider": {"max_completion_tokens": 4096}}]})

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", flaky)

    assert dr._listed_limits("openrouter/m") == (None, None)   # the bad moment
    assert dr._listed_limits("openrouter/m") == (123456, 4096), (
        "the failure was cached, so the retry never happened")
    dr._LISTED.clear()
