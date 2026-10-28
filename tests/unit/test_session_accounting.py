"""Every billed session is on the ledger, and a long one knows when it ends.

954d7885, 25 to 26 October 2026: the owner asked for an itemized bill. Our
ledger showed $9.62 and 295M input tokens, but five experimenter sessions
killed at the 7,200 s wall clock, a pair ended by the Stop, and every attempt
that was retried had left nothing on it: 1,602 billed requests, about 124M
tokens. OpenHands saves usage only when a session ends on its own. Two of the
killed sessions' event streams were also lost, because parallel agents ending
in the same second wrote the same file.
"""
import calendar
import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import ark
import ark.engines as eng


@pytest.fixture(autouse=True)
def _clear_stop_flag():
    ark.shutting_down.clear()
    yield
    ark.shutting_down.clear()


def _stream(n, cond=0, noise=True):
    """An `openhands --json` stdout with n model responses, some with two
    actions each, plus condensations and terminal-UI noise."""
    lines = ["Initializing agent...", "\x1b[37mAgent is working\x1b[0m"] if noise else []
    for i in range(n):
        for _ in range(2 if i % 3 == 0 else 1):
            lines.append(json.dumps({"kind": "ActionEvent", "llm_response_id": f"r{i}"}))
        lines.append(json.dumps({"kind": "ObservationEvent", "action_id": f"a{i}"}))
    for j in range(cond):
        lines.append(json.dumps({"kind": "Condensation", "llm_response_id": f"c{j}"}))
    return "\n".join(lines) + "\n"


MEASURED = {"agent_type": "experimenter", "requests": 100, "input_tokens": 7_500_000,
            "cache_read_tokens": 7_200_000, "cache_creation_tokens": 0,
            "output_tokens": 95_000, "cost_usd": 0.24, "timestamp": "t0"}


def _agent(tmp_path, attempts, stats=()):
    """The real run_agent with a fake runner. `attempts` is a list of
    (returncode, stdout, elapsed, timeout_expired, parsed) per call."""
    calls = []

    class Runner:
        def execute(self, **kw):
            rc, out, el, to, _ = attempts[len(calls)]
            calls.append(kw)
            return rc, out, "", el, to

        def parse_output(self, stdout):
            return attempts[len(calls) - 1][4]

    (tmp_path / "experimenter.prompt").write_text("You run experiments.")
    o = eng.AgentMixin()
    noop = lambda *a, **k: None
    for name, val in dict(
            agents_dir=tmp_path, state_dir=tmp_path, code_dir=tmp_path, log_dir=tmp_path,
            model="deepseek/deepseek-reasoner",
            memory=SimpleNamespace(get_context_for_agent=lambda a: ""),
            _agent_stats=[dict(s) for s in stats], _agent_empty_count=0,
            _rate_limit_notified=False, _terminal_error=None, log=noop, log_step=noop,
            log_summary_box=noop, send_notification=noop, save_checkpoint=noop,
            _write_cost_report=noop, _cleanup_cli_state=noop,
            _build_path_boundary=lambda: "",
            _summarize_agent_output=lambda a, r: []).items():
        setattr(o, name, val)
    return o, calls, Runner


def _run(o, Runner, timeout=7200):
    with mock.patch.object(eng, "get_cli_for_model", lambda m: Runner()), \
            mock.patch.object(ark.shutting_down, "wait", lambda t=None: False):
        return o.run_agent("experimenter", "run E1", timeout=timeout)


class TestCountingRequests:
    def test_distinct_responses_and_condensations_are_counted_once(self):
        assert eng._count_llm_requests(_stream(10, cond=2)) == 12

    def test_noise_and_empty_streams_count_nothing(self):
        assert eng._count_llm_requests("") == 0
        assert eng._count_llm_requests("Goodbye!\n{not json}\n") == 0


class TestKilledSessionIsOnTheLedger:
    def test_a_session_killed_at_the_wall_clock_is_estimated_not_zero(self, tmp_path):
        """The 954d7885 case: 7,200 s, exit -9, no conversation id, no usage."""
        o, calls, Runner = _agent(
            tmp_path, [(-9, _stream(40), 7200, True, {"result": "", "usage": None})],
            stats=[MEASURED])
        _run(o, Runner)
        rec = o._agent_stats[-1]
        assert rec["estimated"] is True and rec["requests"] == 40
        assert rec["input_tokens"] == 3_000_000          # 75,000 per request
        assert rec["cache_read_tokens"] == 2_880_000
        assert rec["output_tokens"] == 38_000
        assert rec["cost_usd"] == pytest.approx(0.096)
        assert len(calls) == 1, "a wall-clock timeout is still not rerun"

    def test_with_nothing_measured_yet_the_requests_are_still_recorded(self, tmp_path):
        o, _, Runner = _agent(
            tmp_path, [(-9, _stream(7), 7200, True, {"result": "", "usage": None})])
        _run(o, Runner)
        rec = o._agent_stats[-1]
        assert rec["estimated"] is True and rec["requests"] == 7
        assert rec["input_tokens"] == 0 and rec["cost_usd"] == 0

    def test_saved_usage_is_used_as_is(self, tmp_path):
        saved = {"model": "m", "requests": 5, "input_tokens": 10, "output_tokens": 2,
                 "cache_read_tokens": 8, "cache_creation_tokens": 0, "cost_usd": 0.01,
                 "duration_api_ms": 0}
        o, _, Runner = _agent(
            tmp_path, [(0, _stream(5), 60, False, {"result": "done", "usage": saved})],
            stats=[MEASURED])
        _run(o, Runner)
        rec = o._agent_stats[-1]
        assert "estimated" not in rec and rec["input_tokens"] == 10

    def test_a_retried_attempt_is_billed_too(self, tmp_path):
        """25 Oct 15:30: an attempt ended on a tool-format error with no answer,
        was retried, and its 23 requests never reached the ledger."""
        first = {"model": "m", "requests": 23, "input_tokens": 2_000_000,
                 "output_tokens": 68_000, "cache_read_tokens": 1_900_000,
                 "cache_creation_tokens": 0, "cost_usd": 0.12, "duration_api_ms": 0}
        second = {"model": "m", "requests": 500, "input_tokens": 40_000_000,
                  "output_tokens": 470_000, "cache_read_tokens": 39_000_000,
                  "cache_creation_tokens": 0, "cost_usd": 1.18, "duration_api_ms": 0}
        o, calls, Runner = _agent(tmp_path, [
            (0, _stream(23), 426, False, {"result": "", "usage": first}),
            (0, _stream(500), 6700, False, {"result": "done", "usage": second}),
        ])
        _run(o, Runner)
        assert len(calls) == 2
        rec = o._agent_stats[-1]
        assert rec["requests"] == 523
        assert rec["input_tokens"] == 42_000_000
        assert rec["cost_usd"] == pytest.approx(1.30)


class TestDeadlineNote:
    def test_a_long_session_is_told_when_it_stops(self):
        t0 = calendar.timegm((2026, 10, 25, 11, 24, 0))
        note = eng._deadline_note(7200, t0)
        assert "13:24 UTC" in note and "13:14 UTC" in note and "120 minutes" in note

    def test_a_short_session_gets_no_note(self):
        assert eng._deadline_note(600, time.time()) == ""

    def test_the_note_reaches_the_agent(self, tmp_path):
        o, calls, Runner = _agent(
            tmp_path, [(0, _stream(3), 60, False, {"result": "done", "usage": None})] * 2)
        _run(o, Runner, timeout=7200)
        _run(o, Runner, timeout=600)
        assert "## Time limit" in calls[0]["prompt"]
        assert "## Time limit" not in calls[1]["prompt"]


class TestStreamFiles:
    def test_two_agents_ending_in_the_same_second_keep_both_streams(self, tmp_path):
        o, _, Runner = _agent(tmp_path, [
            (0, _stream(3) + "first\n", 60, False, {"result": "done", "usage": None}),
            (0, _stream(4) + "second\n", 60, False, {"result": "done", "usage": None}),
        ])
        with mock.patch.object(eng.time, "strftime", lambda *a: "20261025_132359"):
            _run(o, Runner)
            _run(o, Runner)
        files = sorted((tmp_path / "agent_streams").iterdir())
        assert [f.name for f in files] == ["20261025_132359_experimenter.jsonl",
                                           "20261025_132359_experimenter_2.jsonl"]
        assert files[0].read_text().endswith("first\n")
        assert files[1].read_text().endswith("second\n")
