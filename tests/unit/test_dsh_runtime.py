"""DSH boundary contracts, plus optional real-SDK tests using a loopback API."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ark.engines import _add_usage
from ark.engines.cli import AgentCLI, get_cli_for_model
from ark.engines.dsh_runtime import DSHRuntime
from ark.observability.steps import parse_line


def event(kind, data, seq=1, sid="fixture"):
    return {"kind": "DSHEvent", "method": "session.event", "payload": {
        "sessionId": sid, "event": {"type": kind, "seq": seq, "data": data}}}


def test_explicit_dsh_selection(monkeypatch):
    monkeypatch.setenv("ARK_AGENT_RUNTIME", " DSH ")
    assert isinstance(get_cli_for_model("openai/fixture"), DSHRuntime)


def test_partial_output_is_not_success_and_retains_usage():
    record = event("assistant/message", {"message": {"content": [{"type": "text", "text": "done"}]},
                                         "usage": {"inputTokens": 10, "outputTokens": 5}})
    parsed = DSHRuntime("openai/fixture").parse_output(json.dumps(record))
    assert parsed["error_code"] == "DSHIncompleteRun"
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
    assert runner.parse_output(out)["error_code"] == "DSHProcessError"
    assert runner.parse_output(out)["result"] == ""


def test_dsh_environment_only_contains_selected_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-selected")
    for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "ARK_GITHUB_PAT", "GH_TOKEN", "AWS_ACCESS_KEY_ID"):
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
    runner = DSHRuntime("openai/fixture")
    rc, out, _, _, _ = runner.execute("test", "test boundary", tmp_path, 2)
    assert rc == 1
    assert runner.parse_output(out)["error_code"] == "DSHConfigurationError"


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
    python = os.environ.get("ARK_DSH_TEST_PYTHON")
    if not python:
        pytest.skip("set ARK_DSH_TEST_PYTHON to the pinned, isolated SDK interpreter")
    monkeypatch.setenv("ARK_DSH_PYTHON", python)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-gateway-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-gateway-key")
    monkeypatch.setenv("ARK_DSH_MAX_TOKENS", "1024")
    monkeypatch.setenv("ARK_DSH_CONTEXT_WINDOW", "32768")
    monkeypatch.delenv("ARK_DSH_API", raising=False)
    monkeypatch.delenv("ARK_AGENT_SANDBOX", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    state = {"requests": [], "mode": "success", "command": "printf verified > fixture.txt"}

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
            if len(state["requests"]) == 1:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call-fixture",
                    "type": "function", "function": {"name": "bash", "arguments": json.dumps({
                        "command": state["command"]})}}]}
                reason = "tool_calls"
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
                        {"type": "tool_use", "id": "call-fixture", "name": "bash", "input": {}} if tool
                        else {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta":
                        {"type": "input_json_delta", "partial_json": json.dumps({"command": state["command"]})} if tool
                        else {"type": "text_delta", "text": "Fixture complete."}},
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
    runner = DSHRuntime(f"{provider}/fixture")
    rc, out, err, _, timed_out = runner.execute("Create fixture.txt then report completion.",
                                               "Stay in this workspace.", tmp_path, 35)
    parsed = runner.parse_output(out)
    assert rc == 0, (parsed, err[-2000:])
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
    rc, out, err, _, _ = resumed.execute("Report the earlier result.", "Stay here.", tmp_path, 25)
    assert rc == 0, (resumed.parse_output(out), err[-2000:])
    messages = real_gateway["requests"][-1]["body"]["messages"]
    assert any(m.get("role") == "tool" for m in messages) or "tool_result" in json.dumps(messages)


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
    assert timed_out
    assert rc != 0
    assert runner.parse_output(out)["error_code"] == "DSHTimeout"
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
    assert runner.parse_output(out)["error_code"] == "DSHCancelled"
    assert len(real_gateway["requests"]) == 1
