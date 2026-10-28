"""A restart continues the unfinished iteration where it stopped.

954d7885, 26 October 2026: after the DeepSeek balance ran out mid-Execute
(03:50) and after a Stop mid-Execute (06:47), each Continue reviewed the
unchanged paper again and wrote a new plan before getting back to the
experiments: 226 requests, about 20M tokens, the same 6.2 score both times.

A step checkpoint names the iteration in progress, but resume loaded it as the
last finished one, and _iteration_prep() then moved past it, so the finished
steps never matched. The abort handlers also overwrote the step checkpoint
with a full-iteration one, which wiped the pointer outright.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

import ark.pipeline as pl
from ark.orchestrator.core import Orchestrator


class Ran(BaseException):
    """A step that should have been skipped ran (not an Exception, so the
    pipeline's own error handling can't swallow it)."""


def _orch(tmp_path, ck):
    """An Orchestrator with the checkpoint store in `ck` and every agent stubbed."""
    o = Orchestrator.__new__(Orchestrator)
    o.ran, o.executed = [], []
    noop = lambda *a, **k: None

    def prep():
        o.iteration += 1
        o._asked_this_iteration = False

    def save_step(n, name):
        ck.clear()
        ck.update(iteration=o.iteration, completed_step=n, completed_step_name=name)

    def save_full():
        ck.clear()
        ck.update(iteration=o.iteration, completed_phase=0)

    def review_ran():
        raise Ran("review")

    def plan_ran(*a):
        raise Ran("plan")

    def execute(plan, out):
        o.executed.append([i["status"] for i in plan["issues"]])
        return True

    plan = {"issues": [{"id": "E4", "type": "EXPERIMENT_REQUIRED", "status": "in_progress"},
                       {"id": "E5", "type": "EXPERIMENT_REQUIRED", "status": "completed"}]}
    (tmp_path / "latest_review.md").write_text("Overall Score: 6.2/10")
    for name, val in dict(
            state_dir=tmp_path, iteration=0, _asked_this_iteration=False,
            max_iterations=5, paper_accept_threshold=8.0,
            total_input_tokens=0, total_output_tokens=0, hooks=None,
            _terminal_error=None, _quota_exhausted=False,
            memory=SimpleNamespace(stagnation_count=0),
            log=noop, log_section=noop, log_step=noop, log_step_header=noop,
            log_summary_box=noop, notify_progress=noop, send_notification=noop,
            _publish_state_docs=noop, _check_human_intervention=noop,
            _iteration_prep=prep, load_paper_state=lambda: {"current_score": 6.2},
            extract_issue_ids=lambda: [], _check_acceptance=lambda *a: (False, False, False),
            _run_citation_verification=review_ran, run_planning_phase=plan_ran,
            _load_action_plan=lambda: plan, _save_action_plan=noop,
            _run_execute_phase=execute,
            _step_compile=lambda *a: True, _step_validate=lambda *a: o.ran.append("validate"),
            _iteration_finalize=lambda *a: True,
            _resume_checkpoint=lambda: dict(ck), load_checkpoint=lambda: dict(ck),
            save_step_checkpoint=save_step, save_checkpoint=save_full).items():
        setattr(o, name, val)
    return o


def test_resume_re_enters_the_iteration_in_progress(tmp_path):
    ck = {"iteration": 2, "completed_step": 3, "completed_step_name": "Plan"}
    o = _orch(tmp_path, ck)
    o.resume_from_checkpoint()
    assert o.iteration == 1
    o._iteration_prep()
    assert o.get_resume_step() == 3


def test_a_full_iteration_checkpoint_resumes_at_the_next_iteration(tmp_path):
    """Unchanged: a finished iteration is not re-entered."""
    o = _orch(tmp_path, {"iteration": 2, "completed_phase": 0})
    o.resume_from_checkpoint()
    assert o.iteration == 2


def test_continue_after_a_stop_goes_straight_back_to_execute(tmp_path):
    """The 06:47 case: Stop mid-Execute leaves the Plan step checkpoint."""
    ck = {"iteration": 1, "completed_step": 3, "completed_step_name": "Plan"}
    o = _orch(tmp_path, ck)
    o.resume_from_checkpoint()
    assert o.run_paper_iteration() is True          # raises Ran if review/plan ran
    assert o.iteration == 1, "the unfinished iteration was not skipped"
    assert o.executed == [["pending", "completed"]], "the cut-off task runs again, the done one not"
    assert o.ran == ["validate"]


def test_continue_after_an_abort_keeps_the_step_pointer(tmp_path):
    """The 03:50 case: the balance runs out mid-Execute, the run aborts."""
    ck = {"iteration": 1, "completed_step": 3, "completed_step_name": "Plan"}
    o = _orch(tmp_path, ck)
    o.iteration = 1
    assert o._handle_terminal_error(6.2) is False
    assert ck["completed_step"] == 3, "the abort wiped the resume pointer"
    fresh = _orch(tmp_path, ck)
    fresh.resume_from_checkpoint()
    assert fresh.run_paper_iteration() is True
    assert fresh.executed and fresh.iteration == 1


def test_an_abort_before_any_step_still_redoes_the_iteration(tmp_path):
    """Unchanged: nothing finished, so the rolled-back pointer is saved."""
    ck = {"iteration": 0, "completed_phase": 0}
    o = _orch(tmp_path, ck)
    o.iteration = 1
    o._handle_terminal_error(6.2)
    assert ck == {"iteration": 0, "completed_phase": 0}


def test_quota_wait_in_process_resumes_after_the_finished_steps(tmp_path):
    ck = {"iteration": 1, "completed_step": 3, "completed_step_name": "Plan"}
    o = _orch(tmp_path, ck)
    o.iteration = 1
    with mock.patch.object(pl, "RateLimitCountdown", lambda s: SimpleNamespace(run=lambda: None)):
        assert o._handle_quota_exhausted(6.2) is True
    assert o.run_paper_iteration() is True
    assert o.iteration == 1 and o.executed
