"""Unit tests for ark.observability.steps — event parsing, redaction, step log."""

import json
from pathlib import Path

import pytest

from ark.observability.steps import parse_line, Redactor, StepLogger


def ev(d):
    return json.dumps(d)


# ── parse_line ─────────────────────────────────────────────

def test_parse_bash_command():
    s = parse_line(ev({"kind": "ActionEvent",
                       "action": {"kind": "ExecuteBashAction", "command": "sbatch train.sh"}}))
    assert s.type == "command"
    assert "sbatch train.sh" in s.summary


def test_parse_file_edit():
    s = parse_line(ev({"kind": "ActionEvent",
                       "action": {"kind": "FileEditAction", "path": "exp.py"}}))
    assert s.type == "edit"
    assert "exp.py" in s.summary


def test_parse_file_read():
    s = parse_line(ev({"kind": "ActionEvent",
                       "action": {"kind": "FileReadAction", "path": "a.txt"}}))
    assert s.type == "read"


def test_parse_finish():
    s = parse_line(ev({"kind": "ActionEvent",
                       "action": {"kind": "FinishAction", "message": "all done"}}))
    assert s.type == "finish"
    assert "all done" in s.summary


def test_parse_observation_result():
    s = parse_line(ev({"kind": "ObservationEvent", "observation": {"output": "exit 0\nok"}}))
    assert s.type == "result"


def test_parse_agent_message_thought():
    s = parse_line(ev({"kind": "MessageEvent", "source": "agent",
                       "llm_message": {"content": [{"type": "text", "text": "let me check"}]}}))
    assert s.type == "thought"
    assert "let me check" in s.summary


def test_parse_error():
    s = parse_line(ev({"kind": "ConversationErrorEvent", "code": "rate_limit", "detail": "slow down"}))
    assert s.type == "error"


def test_parse_environment_observation_result():
    # OpenHands terminal observations arrive as source==environment, no kind.
    s = parse_line(ev({"source": "environment", "tool_name": "terminal", "content": "exit 0\nok"}))
    assert s.type == "result"
    assert "ok" in s.summary


def test_command_summary_has_no_double_dollar():
    # the "$" comes from the logger icon; the parsed summary must not add its own
    s = parse_line(ev({"kind": "ActionEvent",
                       "action": {"kind": "ExecuteBashAction", "command": "echo hi"}}))
    assert not s.summary.startswith("$")
    assert s.summary == "echo hi"


def test_parse_noise_returns_none():
    assert parse_line("Conversation ID: abc123") is None
    assert parse_line("") is None
    assert parse_line("random terminal text") is None


# ── redaction ──────────────────────────────────────────────

def test_redactor_scrubs_known_secret():
    r = Redactor(["sk-supersecret999"])
    assert r.redact("using sk-supersecret999 now") == "using *** now"


def test_redactor_scrubs_name_value():
    r = Redactor([])
    out = r.redact("OPENAI_API_KEY=sk-abc123def python run.py")
    assert "sk-abc123def" not in out
    assert "OPENAI_API_KEY=***" in out


def test_redactor_add_value():
    r = Redactor([])
    r.add("hf_tokenvalue123")
    assert "***" in r.redact("export X; echo hf_tokenvalue123")


# ── StepLogger ─────────────────────────────────────────────

def _logger(tmp_path, verbosity="normal", secrets=None):
    seen = []
    lg = StepLogger(
        steps_path=tmp_path / "agent_steps.jsonl",
        log_step_fn=lambda msg, status: seen.append((msg, status)),
        redactor=Redactor(secrets or []),
        verbosity=verbosity,
        agent_type="experimenter",
    )
    return lg, seen


def test_steplogger_writes_jsonl_and_human_line(tmp_path):
    lg, seen = _logger(tmp_path)
    lg.feed_line(ev({"kind": "ActionEvent",
                     "action": {"kind": "ExecuteBashAction", "command": "python train.py"}}))
    journal = (tmp_path / "agent_steps.jsonl").read_text().strip()
    assert json.loads(journal)["type"] == "command"
    assert seen and seen[0][1] == "progress"
    assert "python train.py" in seen[0][0]


def test_steplogger_verbosity_hides_read_at_normal(tmp_path):
    lg, seen = _logger(tmp_path, verbosity="normal")
    lg.feed_line(ev({"kind": "ActionEvent", "action": {"kind": "FileReadAction", "path": "a.txt"}}))
    # read is journaled but NOT shown at normal verbosity
    assert (tmp_path / "agent_steps.jsonl").exists()
    assert seen == []


def test_steplogger_verbose_shows_read(tmp_path):
    lg, seen = _logger(tmp_path, verbosity="verbose")
    lg.feed_line(ev({"kind": "ActionEvent", "action": {"kind": "FileReadAction", "path": "a.txt"}}))
    assert seen and "a.txt" in seen[0][0]


def test_steplogger_redacts_human_line(tmp_path):
    lg, seen = _logger(tmp_path, secrets=["sk-leakme123"])
    lg.feed_line(ev({"kind": "ActionEvent",
                     "action": {"kind": "ExecuteBashAction", "command": "curl -H sk-leakme123 x"}}))
    assert seen and "sk-leakme123" not in seen[0][0]
    assert "***" in seen[0][0]
