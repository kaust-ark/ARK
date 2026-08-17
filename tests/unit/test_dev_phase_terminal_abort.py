"""A dead agent must stop the dev phase, not be drafted around.

The review loop has always checked ``_terminal_error`` between steps. The dev
loop never did. Seen live on smoke run 6d753a94: a misresolved API key made
every agent die on a 401, each dead agent was still logged "✓ completed", and
the loop walked plan → run → analyze → evaluate producing nothing at all. The
compute backend then read the absent results directory as "nothing is running"
and returned done, so the pipeline went on to write a paper about experiments
that had never executed.
"""

from unittest.mock import MagicMock, patch

import pytest

from ark.pipeline import PipelineMixin


@pytest.fixture
def pipe():
    p = PipelineMixin.__new__(PipelineMixin)
    p.logs = []
    p.log = lambda msg, level="INFO": p.logs.append((level, msg))
    return p


def test_a_clean_step_does_not_abort(pipe):
    pipe._terminal_error = None
    assert pipe._abort_dev_on_terminal_error("running experiments") is False
    assert pipe.logs == []


def test_a_missing_attribute_does_not_abort(pipe):
    """Called before anything sets the flag — must not raise."""
    assert pipe._abort_dev_on_terminal_error("planning experiments") is False


def test_a_terminal_error_aborts_and_names_the_step(pipe):
    pipe._terminal_error = "AuthenticationError: 401 no credentials"
    assert pipe._abort_dev_on_terminal_error("running experiments") is True
    joined = " ".join(m for _, m in pipe.logs)
    assert "running experiments" in joined
    assert "401" in joined                       # the real reason survives
    assert all(lvl == "ERROR" for lvl, _ in pipe.logs)


def test_the_refusal_to_draft_is_stated_explicitly(pipe):
    """The log is the only place a human sees why nothing was delivered."""
    pipe._terminal_error = "BadRequestError: model not found"
    pipe._abort_dev_on_terminal_error("analyzing results")
    joined = " ".join(m for _, m in pipe.logs).lower()
    assert "no experiments" in joined and "refusing" in joined


class TestLoopStopsAtTheFailingStep:
    """Each dev step is followed by the check, so failure ends the iteration."""

    def _loop(self, fail_at):
        """Drive _run_experiment_loop with agents that die at `fail_at`."""
        p = PipelineMixin.__new__(PipelineMixin)
        p.logs, p.called = [], []
        p.log = lambda msg, level="INFO": p.logs.append((level, msg))
        p.log_section = lambda *a, **k: None
        p.log_step = lambda *a, **k: None
        p._terminal_error = None
        p._save_dev_phase_state = lambda *a, **k: None
        p._send_dev_phase_telegram = lambda *a, **k: None
        p._load_findings_summary = lambda: ""
        p._experiment_approval_gate = lambda: None
        p.hooks = None

        def step(name):
            def _f(*a, **k):
                p.called.append(name)
                if name == fail_at:
                    p._terminal_error = "AuthenticationError: 401"
            return _f

        p._plan_experiments = step("plan")
        p._run_experiments = step("run")
        p._analyze_results = step("analyze")
        p._evaluate_completeness = lambda *a, **k: (p.called.append("evaluate"), True)[1]
        p._run_experiment_loop({}, 0, 1, "idea")
        return p.called

    def test_failure_while_planning_skips_the_experiments(self):
        assert self._loop("plan") == ["plan"]

    def test_failure_while_running_skips_the_analysis(self):
        assert self._loop("run") == ["plan", "run"]

    def test_failure_while_analysing_skips_the_completeness_call(self):
        assert self._loop("analyze") == ["plan", "run", "analyze"]

    def test_a_healthy_iteration_runs_every_step(self):
        assert self._loop(None) == ["plan", "run", "analyze", "evaluate"]
