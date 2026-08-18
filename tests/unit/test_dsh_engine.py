"""Tests for the DeepSeek Harness (dsh) agent runtime engine.

Covers the full engine contract without touching the network or a real dsh
install: model-string routing, the generated patch overlay, env isolation,
session-log parsing (result / usage / error), the skills bridge symlink, the
dsh event mapping in the step log, and an end-to-end ``execute()`` →
``parse_output()`` round trip against a fake ``dsh`` binary (same pattern as
``tests/e2e/fake_openhands.py``).

Ground truth for the shapes asserted here: dsh 0.1.0-rc.7
(``@deepseek-ai/dsh``) — see docs/DEEPSEEK_HARNESS.md.
"""
import json
import os
import stat
import sys
from pathlib import Path

import yaml

from ark.engines.cli import DshCLI, OpenHandsCLI, get_cli_for_model
from ark.observability.steps import parse_line


# ---------------------------------------------------------------- routing

def test_factory_routes_dsh_prefix_to_dsh_engine():
    assert isinstance(get_cli_for_model("dsh/deepseek-v4"), DshCLI)
    assert isinstance(get_cli_for_model("x", "dsh/deepseek-v4-flash"), DshCLI)


def test_factory_keeps_litellm_strings_on_openhands():
    assert isinstance(get_cli_for_model("anthropic/claude-sonnet-4-6"), OpenHandsCLI)
    assert isinstance(get_cli_for_model("deepseek/deepseek-chat"), OpenHandsCLI)
    assert isinstance(get_cli_for_model(""), OpenHandsCLI)


def test_model_spec_resolution():
    assert DshCLI("dsh/deepseek-v4")._spec() == ("deepseek-official", "deepseek-v4")
    # Explicit provider form for forward-compat with other dsh providers.
    assert DshCLI("dsh/my-gateway/some-model")._spec() == ("my-gateway", "some-model")


# ---------------------------------------------------------------- launch

def test_build_command_writes_patch_and_boundary(tmp_path):
    cli = DshCLI("dsh/deepseek-v4")
    cmd = cli.build_command("do the task", "STAY IN /work", tmp_path)

    assert cmd[0] == "dsh"
    assert cmd[1:3] == ["--profile", "headless"]
    assert cmd[3] == "--patch"
    # Path boundary folded into the task text (the OS sandbox enforces it too).
    assert cmd[5].startswith("[SYSTEM RULE] STAY IN /work")
    assert "do the task" in cmd[5]

    patch = yaml.safe_load(Path(cmd[4]).read_text())
    by_id = {row["id"]: row["config"] for row in patch}
    assert by_id["agent-default-model"] == {
        "provider": "deepseek-official", "model": "deepseek-v4"}
    # Plain JSONL so the orchestrator can tail/parse without zstd.
    assert by_id["session-persistence-jsonl"]["compression"] == "none"
    sessions_root = Path(by_id["session-persistence-jsonl"]["root"])
    assert sessions_root == tmp_path / ".dsh_home" / "sessions"
    assert sessions_root.is_dir()
    # 60s default is too short for experiment installs/compiles.
    assert by_id["bash-sandbox"]["timeoutMs"] == 600000


def test_build_command_respects_bin_and_timeout_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_DSH_BIN", "/opt/dsh/bin/dsh")
    monkeypatch.setenv("ARK_DSH_BASH_TIMEOUT_MS", "120000")
    cmd = DshCLI("dsh/deepseek-v4").build_command("t", "b", tmp_path)
    assert cmd[0] == "/opt/dsh/bin/dsh"
    patch = yaml.safe_load(Path(cmd[4]).read_text())
    by_id = {row["id"]: row["config"] for row in patch}
    assert by_id["bash-sandbox"]["timeoutMs"] == 120000


def test_build_env_isolation_and_dsh_knobs(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_GITHUB_PAT", "ghp_secret_value")
    monkeypatch.setenv("GITHUB_TOKEN", "gho_other_secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-key")
    monkeypatch.delenv("DSH_PERMISSION_MODE", raising=False)

    env = DshCLI("dsh/deepseek-v4").build_env(tmp_path)

    # Same orchestrator-credential stripping contract as OpenHands.
    assert "ARK_GITHUB_PAT" not in env
    assert "GITHUB_TOKEN" not in env
    # Per-project home → sessions, profile state, settings stay in the sandbox.
    assert env["DSH_HOME"] == str(tmp_path / ".dsh_home")
    # OS-enforced workspace sandbox by default; telemetry off unless opted in.
    assert env["DSH_PERMISSION_MODE"] == "workspace-write"
    assert env["DSH_TELEMETRY_MODE"] == "DISABLED"
    assert env["DEEPSEEK_API_KEY"] == "sk-ds-key"


def test_build_env_permission_mode_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_DSH_PERMISSION_MODE", "danger-full-access")
    monkeypatch.delenv("DSH_PERMISSION_MODE", raising=False)
    env = DshCLI("dsh/deepseek-v4").build_env(tmp_path)
    assert env["DSH_PERMISSION_MODE"] == "danger-full-access"


def test_skills_symlink_bridges_claude_skills_to_agents(tmp_path):
    skills = tmp_path / ".claude" / "skills" / "research-integrity"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: research-integrity\n---\n")

    DshCLI("dsh/deepseek-v4").build_command("t", "b", tmp_path)

    link = tmp_path / ".agents" / "skills"
    assert link.is_symlink()
    assert (link / "research-integrity" / "SKILL.md").exists()


def test_skills_symlink_skipped_without_skills(tmp_path):
    DshCLI("dsh/deepseek-v4").build_command("t", "b", tmp_path)
    assert not (tmp_path / ".agents").exists()


# ---------------------------------------------------------------- parsing

def _write_session(tmp_path, events, code_dir=None):
    """Materialize a session.jsonl exactly where the engine expects one."""
    root = (code_dir or tmp_path) / ".dsh_home" / "sessions"
    sess = root / "--proj--" / "session-0000"
    sess.mkdir(parents=True)
    lines = [json.dumps({"type": "session", "id": "session-0000"})]
    for i, (etype, data) in enumerate(events):
        lines.append(json.dumps({"seq": i, "type": etype, "data": data}))
    (sess / "session.jsonl").write_text("\n".join(lines) + "\n")
    return sess


def _prepared_cli(tmp_path):
    """A DshCLI that has 'launched' (build_command sets the session baseline)."""
    cli = DshCLI("dsh/deepseek-v4")
    cli.build_command("t", "b", tmp_path)
    return cli


def test_parse_output_success_with_usage(tmp_path):
    cli = _prepared_cli(tmp_path)
    _write_session(tmp_path, [
        ("turn/start", {"turn": 1}),
        ("request/header", {"header": {"config": {
            "provider": "deepseek-official", "model": "deepseek-v4"}}}),
        # Usage repeats per (turn, step) with running totals — last one wins
        # per step, steps sum.
        ("assistant/chunk", {"turn": 1, "step": 1, "chunk": {
            "type": "usage", "usage": {"inputTokens": 10, "outputTokens": 1}}}),
        ("assistant/chunk", {"turn": 1, "step": 1, "chunk": {
            "type": "usage", "usage": {
                "inputTokens": 100, "outputTokens": 20,
                "cacheReadTokens": 50, "cacheWriteTokens": 5}}}),
        ("assistant/message", {"turn": 1, "step": 2, "usage": {
            "inputTokens": 200, "outputTokens": 30}, "message": {
                "content": [{"type": "text", "text": "done"}]}}),
        ("turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ])

    parsed = cli.parse_output("Final answer text\n")

    assert parsed["result"] == "Final answer text"
    assert parsed["error_code"] is None
    assert parsed["conversation_id"] == "session-0000"
    usage = parsed["usage"]
    assert usage["model"] == "deepseek-v4"
    assert usage["input_tokens"] == 300      # 100 (step-1 final) + 200 (step 2)
    assert usage["output_tokens"] == 50      # 20 + 30
    assert usage["cache_read_tokens"] == 50
    assert usage["cache_creation_tokens"] == 5
    assert isinstance(usage["cost_usd"], float)


def test_parse_output_surfaces_turn_error(tmp_path):
    cli = _prepared_cli(tmp_path)
    _write_session(tmp_path, [
        ("turn/start", {"turn": 1}),
        ("turn/end", {"turn": 1, "reason": {"kind": "error", "error": {
            "code": "AUTH", "message": "Authentication Fails", "status": 401}}}),
    ])

    parsed = cli.parse_output("")

    assert parsed["result"] == ""
    assert parsed["error_code"] == "AUTH"
    assert "Authentication Fails" in parsed["error_detail"]


def test_parse_output_without_session_file(tmp_path):
    cli = _prepared_cli(tmp_path)
    parsed = cli.parse_output("text only\n")
    assert parsed["result"] == "text only"
    assert parsed["usage"] is None
    assert parsed["error_code"] is None
    assert parsed["conversation_id"] is None


def test_parse_output_ignores_preexisting_sessions(tmp_path):
    # A session left over from an earlier run must not be read as this run's.
    _write_session(tmp_path, [
        ("turn/end", {"turn": 1, "reason": {"kind": "error", "error": {
            "code": "STALE", "message": "old run"}}}),
    ])
    cli = _prepared_cli(tmp_path)  # baseline snapshot taken AFTER the stale file
    parsed = cli.parse_output("fresh\n")
    assert parsed["error_code"] is None
    assert parsed["conversation_id"] is None


# ---------------------------------------------------------------- step log

def _dsh_line(etype, data):
    return json.dumps({"seq": 1, "type": etype, "data": data})


def test_parse_line_maps_dsh_bash_to_command():
    evt = parse_line(_dsh_line("tool/call", {
        "turn": 1, "step": 1, "callId": "c1", "name": "bash",
        "arguments": {"command": "python train.py --epochs 3"}}))
    assert evt.type == "command"
    assert "python train.py" in evt.summary


def test_parse_line_maps_dsh_fs_tools():
    edit = parse_line(_dsh_line("tool/call", {
        "name": "edit", "arguments": {"file_path": "paper/main.tex"}}))
    assert (edit.type, edit.detail) == ("edit", "paper/main.tex")

    read = parse_line(_dsh_line("tool/call", {
        "name": "read", "arguments": {"file_path": "results/out.csv"}}))
    assert read.type == "read"

    view = parse_line(_dsh_line("tool/call", {
        "name": "str_replace_editor",
        "arguments": {"command": "view", "path": "/w/f.py"}}))
    assert view.type == "read"

    sre = parse_line(_dsh_line("tool/call", {
        "name": "str_replace_editor",
        "arguments": {"command": "str_replace", "path": "/w/f.py"}}))
    assert sre.type == "edit"


def test_parse_line_maps_dsh_message_result_and_errors():
    thought = parse_line(_dsh_line("assistant/message", {
        "message": {"content": [{"type": "text", "text": "I will now run tests"}]}}))
    assert thought.type == "thought"

    result = parse_line(_dsh_line("tool/result", {
        "message": {"content": [{"type": "text", "text": "3 passed"}]}}))
    assert result.type == "result"

    err = parse_line(_dsh_line("turn/end", {
        "reason": {"kind": "error", "error": {"code": "QUOTA", "message": "no balance"}}}))
    assert err.type == "error"
    assert "QUOTA" in err.summary

    done = parse_line(_dsh_line("turn/end", {"reason": {"kind": "completed"}}))
    assert done.type == "finish"


def test_parse_line_openhands_events_still_parse():
    evt = parse_line(json.dumps({
        "kind": "MessageEvent", "source": "agent",
        "llm_message": {"content": [{"type": "text", "text": "hi"}]}}))
    assert evt.type == "thought"


# ---------------------------------------------------------------- execute e2e

FAKE_DSH = r'''#!/usr/bin/env python3
"""Stand-in for the `dsh` binary: emits the headless contract.

Writes a session.jsonl (plain compression) under the patched sessions root,
prints the final assistant text on stdout, exits 0 — mirroring dsh
0.1.0-rc.7's headless runner (`@deepseek-ai/dsh-headless`).
"""
import json, sys, os
args = sys.argv[1:]
patch_file = args[args.index("--patch") + 1]
import re
root = None
for line in open(patch_file):
    m = re.search(r"root: (.*)$", line.strip())
    if m:
        root = m.group(1).strip().strip("'\"")
assert root, "patch must carry the sessions root"
sess = os.path.join(root, "--proj--", "session-fake0001")
os.makedirs(sess, exist_ok=True)
events = [
    {"type": "session", "id": "session-fake0001"},
    {"seq": 0, "type": "turn/start", "data": {"turn": 1}},
    {"seq": 1, "type": "tool/call", "data": {"turn": 1, "step": 1,
        "callId": "c1", "name": "bash", "arguments": {"command": "ls results/"}}},
    {"seq": 2, "type": "assistant/message", "data": {"turn": 1, "step": 2,
        "usage": {"inputTokens": 42, "outputTokens": 7},
        "message": {"content": [{"type": "text", "text": "All done."}]}}},
    {"seq": 3, "type": "turn/end", "data": {"turn": 1,
        "reason": {"kind": "completed"}}},
]
with open(os.path.join(sess, "session.jsonl"), "w") as fh:
    for e in events:
        fh.write(json.dumps(e) + "\n")
sys.stdout.write("All done.\n")
'''


def test_execute_round_trip_with_fake_dsh(tmp_path, monkeypatch):
    """Full engine path: build_command → subprocess → tailer → parse_output."""
    fake = tmp_path / "bin" / "dsh"
    fake.parent.mkdir()
    fake.write_text(FAKE_DSH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    # Route the fake through the current interpreter for portability.
    monkeypatch.setenv("ARK_DSH_BIN", sys.executable)

    code_dir = tmp_path / "proj"
    code_dir.mkdir()
    cli = DshCLI("dsh/deepseek-v4")

    real_build = cli.build_command

    def build(prompt, boundary, cdir):
        cmd = real_build(prompt, boundary, cdir)
        return [cmd[0], str(fake)] + cmd[1:]  # python fake_dsh --profile ...

    cli.build_command = build

    seen = []
    rc, stdout, stderr, elapsed, timed_out = cli.execute(
        prompt="finish the analysis", path_boundary="stay in project",
        code_dir=code_dir, timeout=30, on_event=lambda l: seen.append(l))

    assert rc == 0 and not timed_out
    parsed = cli.parse_output(stdout)
    assert parsed["result"] == "All done."
    assert parsed["usage"]["input_tokens"] == 42
    assert parsed["usage"]["output_tokens"] == 7
    assert parsed["error_code"] is None
    assert parsed["conversation_id"] == "session-fake0001"

    # The session tailer must have streamed the bash step to on_event.
    step_types = [getattr(parse_line(l), "type", None) for l in seen]
    assert "command" in step_types, f"tailer streamed: {step_types}"
