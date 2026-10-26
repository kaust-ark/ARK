"""An upstream provider 5xx must be retried, not treated as terminal.

A run that had already produced a clean 7.6/10 paper was discarded because
OpenRouter relayed "Upstream error from Nvidia: Internal server error" during
the review step. The error CODE was a generic APIError, so the code-based
transient list missed it — only the detail text names the real cause.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "ark" / "engines" / "__init__.py").read_text()


def _transient_details():
    m = re.search(r"_TRANSIENT_DETAIL = \((.*?)\n\s*\)", SRC, re.S)
    assert m, "could not locate _TRANSIENT_DETAIL"
    return [s.lower() for s in re.findall(r'"([^"]+)"', m.group(1))]


def _is_transient(detail: str) -> bool:
    d = (detail or "").lower()
    return any(sig in d for sig in _transient_details())


def test_the_exact_error_that_killed_the_run():
    assert _is_transient(
        "litellm.APIError: APIError: OpenrouterException - "
        "Upstream error from Nvidia: Internal server error")


def test_other_upstream_5xx_shapes():
    for d in ("Provider returned 503 Service Unavailable",
              "The model is temporarily unavailable, try again",
              "Upstream error from Anthropic: overloaded_error"):
        assert _is_transient(d), d


def test_real_terminal_errors_are_not_swallowed():
    """A retry loop that eats auth and quota failures is worse than the bug."""
    for d in ("Invalid API key provided",
              "This request requires more credits, or fewer max_tokens",
              "messages.32.content: The image was specified using the image/png "
              "media type, but the image appears to be a image/jpeg image",
              "model `foo/bar` not found"):
        assert not _is_transient(d), d


def test_gemini_quota_429_is_transient_even_as_badrequesterror():
    """litellm wraps Gemini's quota 429 as BadRequestError, and its 'check
    your plan and billing details' text read like a billing cap → terminal.
    That killed 382656fd's review pass minutes after the dev phase delivered
    a complete free-tier paper. A 429 is transient by definition; a real
    daily cap still fails after the bounded retries exhaust."""
    assert _is_transient(
        'litellm.BadRequestError: GeminiException BadRequestError - '
        '{"error": {"code": 429, "message": "You exceeded your current '
        'quota, please check your plan and billing details."}}')


def test_a_stuck_conversation_is_retried_not_aborted():
    """The loop detector's verdict names a wedged agent, not a dead provider.
    A fresh conversation (carrying the RETRY NOTE) is the remedy. 4906d1a2
    died on its FIRST stuck verdict with zero retries because the note was
    wired into a branch this error never reached."""
    assert _is_transient(
        "ConversationRunError: Conversation run failed for id=x: "
        "Remote conversation got stuck")


class TestQuotaIsTransientOnlyWhereItResets:
    """Gemini's free-tier 429 and OpenAI's unpaid-account error share the text
    "You exceeded your current quota". One resets in a minute, the other never."""

    def test_gemini_quota_429_is_retried(self):
        from ark.engines import _is_transient_agent_error
        assert _is_transient_agent_error(
            "BadRequestError",
            '{"error": {"code": 429, "message": "You exceeded your current quota, please '
            'check your plan and billing details.", "status": "RESOURCE_EXHAUSTED"}}')

    def test_openai_insufficient_quota_fails_fast(self):
        from ark.engines import _is_transient_agent_error
        assert not _is_transient_agent_error(
            "RateLimitError",
            "Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
            "please check your plan and billing details.', 'type': 'insufficient_quota', "
            "'code': 'insufficient_quota'}}")

    def test_an_auth_error_is_terminal(self):
        from ark.engines import _is_transient_agent_error
        assert not _is_transient_agent_error("AuthenticationError", "401 invalid api key")


def test_the_stuck_note_is_added_once():
    from ark.engines import _with_stuck_retry_note
    once = _with_stuck_retry_note("task")
    twice = _with_stuck_retry_note(once)
    assert once != "task" and twice == once
    assert twice.count("## RETRY NOTE") == 1


class TestStepBudgetEndsTheCallNotTheRun:
    """The SDK's 500-step cap arrives as a ConversationErrorEvent. Read as a
    provider error it aborted 954d7885 seven hours after one experimenter ran
    out of steps, with the writer already done. Driven through the real
    run_agent with a fake runner, so the branch order is what is tested."""

    DETAIL = "Agent reached maximum iterations limit (500)."

    def _run(self, tmp_path, code, detail, result="partial summary"):
        from types import SimpleNamespace
        from unittest import mock
        import ark.engines as eng

        calls = []

        class Runner:
            def execute(self, **kw):
                calls.append(kw)
                return 0, "{}", "", 42, False

            def parse_output(self, stdout):
                return {"result": result, "usage": None,
                        "error_code": code, "error_detail": detail}

        (tmp_path / "experimenter.prompt").write_text("You run experiments.")
        o = eng.AgentMixin()
        noop = lambda *a, **k: None
        for name, val in dict(
                agents_dir=tmp_path, state_dir=tmp_path, code_dir=tmp_path,
                model="m", memory=SimpleNamespace(get_context_for_agent=lambda a: ""),
                _agent_stats=[], _agent_empty_count=0, _rate_limit_notified=False,
                _terminal_error=None, log=noop, log_step=noop, log_summary_box=noop,
                send_notification=noop, save_checkpoint=noop, _write_cost_report=noop,
                _cleanup_cli_state=noop, _build_path_boundary=lambda: "",
                _summarize_agent_output=lambda a, r: []).items():
            setattr(o, name, val)
        with mock.patch.object(eng, "get_cli_for_model", lambda m: Runner()), \
                mock.patch.object(eng.time, "sleep", noop), \
                mock.patch.object(eng, "RateLimitCountdown",
                                  lambda s: SimpleNamespace(run=noop)):
            out = o.run_agent("experimenter", "run E1", timeout=60)
        return o, out, calls

    def test_running_out_of_steps_is_not_terminal(self, tmp_path):
        o, out, calls = self._run(tmp_path, "MaxIterationsReached", self.DETAIL)
        assert o._terminal_error is None
        assert out == "partial summary"

    def test_the_sandboxed_wrapper_counts_too(self, tmp_path):
        o, out, _ = self._run(tmp_path, "ConversationRunError",
                              "Conversation run failed for id=x: " + self.DETAIL)
        assert o._terminal_error is None and out == "partial summary"

    def test_running_out_of_steps_is_not_retried(self, tmp_path):
        _, _, calls = self._run(tmp_path, "MaxIterationsReached", self.DETAIL, result="")
        assert len(calls) == 1

    def test_a_real_terminal_error_still_aborts(self, tmp_path):
        o, out, calls = self._run(tmp_path, "AuthenticationError", "401 invalid api key")
        assert o._terminal_error.startswith("AuthenticationError")
        assert out == "" and len(calls) == 1

    def test_a_transient_error_is_still_retried(self, tmp_path):
        _, _, calls = self._run(tmp_path, "InternalServerError", "upstream 500")
        assert len(calls) == 4

    def test_the_step_cap_is_not_classified_transient(self):
        from ark.engines import _is_transient_agent_error
        assert not _is_transient_agent_error("MaxIterationsReached", self.DETAIL)
