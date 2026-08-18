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


class TestNothingDownstreamRunsAfterAnAbort:
    """The abort has three escape points and each one leaked in turn.

    Leaving the experiment loop still fell through to drafting (e2f643ab:
    aborted 02:02:40, writer editing main.tex 02:05:31). Returning from the dev
    phase still entered the paper loop, because the terminal handler RETURNS
    its stop decision rather than raising and the return was ignored
    (25e4fb90: aborted 02:17:23, compiling LaTeX the same second).
    """

    def _dev_phase(self, fail: bool):
        p = PipelineMixin.__new__(PipelineMixin)
        p.logs, p.drafted = [], []
        p.log = lambda msg, level="INFO": p.logs.append((level, msg))
        p.log_section = lambda *a, **k: None
        p._send_dev_phase_telegram = lambda *a, **k: None
        # _research_idea is a read-only property on the mixin.
        patch.object(type(p), "_research_idea", "idea").start()
        p.config = {"max_dev_iterations": 1}
        p._load_dev_phase_state = lambda: {"iteration": 0}
        p._terminal_error = None

        def loop(*a, **k):
            if fail:
                p._terminal_error = "AuthenticationError: 401"

        p._run_experiment_loop = loop
        p._generate_all_figures = lambda: p.drafted.append("figures")
        p._write_initial_draft = lambda *a: p.drafted.append("draft")
        p._deliver_dev_phase = lambda *a: p.drafted.append("deliver")
        p._run_dev_phase()
        return p.drafted

    def test_an_aborted_dev_phase_writes_no_draft(self):
        assert self._dev_phase(fail=True) == []

    def test_a_healthy_dev_phase_still_drafts_and_delivers(self):
        assert self._dev_phase(fail=False) == ["figures", "draft", "deliver"]


class TestExperimentPlanDigest:
    """Seed the task with what has to run, for models that will not go read it.

    The pipeline's convention is that agents Read their own source files. That
    assumes a model which reliably acts on "go read this first". A local 32B
    answered from the prompt alone and finished having done nothing — 13k
    tokens in, ~600 out, zero tool calls, empty results/ — three runs running.
    The same model did the identical work in ten tool calls when the task said
    what to run. The digest is orientation only; the file stays authoritative.
    """

    def _digest(self, tmp_path, text, **kw):
        p = PipelineMixin.__new__(PipelineMixin)
        p.state_dir = tmp_path
        if text is not None:
            (tmp_path / "experiment_plan.yaml").write_text(text)
        return p._experiment_plan_digest(**kw)

    def test_experiment_ids_and_descriptions_are_named(self, tmp_path):
        out = self._digest(tmp_path,
                           "experiments:\n"
                           "  - id: exp1\n    description: Raw vs standardized\n"
                           "  - id: exp2\n    description: Ablation on solver\n")
        assert "exp1" in out and "Raw vs standardized" in out
        assert "exp2" in out and "Ablation on solver" in out

    def test_a_missing_plan_adds_nothing(self, tmp_path):
        assert self._digest(tmp_path, None) == ""

    def test_unparseable_yaml_never_breaks_the_step(self, tmp_path):
        assert self._digest(tmp_path, "experiments: [unclosed\n") == ""

    def test_an_empty_plan_adds_nothing(self, tmp_path):
        assert self._digest(tmp_path, "experiments: []\n") == ""

    def test_a_long_plan_is_capped_and_says_so(self, tmp_path):
        body = "experiments:\n" + "".join(
            f"  - id: e{i}\n    description: d{i}\n" for i in range(20))
        out = self._digest(tmp_path, body, limit=3)
        assert out.count("\n- ") == 4          # 3 entries + the "plus more" line
        assert "authoritative" in out

    def test_the_planner_key_may_be_title_instead_of_description(self, tmp_path):
        """Same model, consecutive runs, different key. Accept both."""
        out = self._digest(tmp_path,
                           "experiments:\n  - id: exp1\n    title: Convergence study\n")
        assert "Convergence study" in out

    def test_entries_without_a_description_still_appear(self, tmp_path):
        out = self._digest(tmp_path, "experiments:\n  - id: exp9\n")
        assert "exp9" in out

    def test_junk_entries_are_skipped_not_fatal(self, tmp_path):
        out = self._digest(tmp_path,
                           "experiments:\n  - just a string\n  - id: exp1\n")
        assert "exp1" in out


class TestEvidenceMeansContent:
    """A zero-byte result file is a crash fingerprint, not evidence.

    a7235ecf: the script opened results/exp1_results.json and died one line
    later on a missing import; the empty file passed an existence check and
    the pipeline moved on to analyse nothing.
    """

    def _pipe(self, tmp_path):
        p = PipelineMixin.__new__(PipelineMixin)
        p.code_dir = tmp_path
        p.config = {}
        return p

    def test_an_empty_results_dir_is_no_evidence(self, tmp_path):
        (tmp_path / "results").mkdir()
        assert self._pipe(tmp_path)._experiment_evidence_files() == []

    def test_a_zero_byte_file_is_no_evidence(self, tmp_path):
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "exp1_results.json").write_bytes(b"")
        assert self._pipe(tmp_path)._experiment_evidence_files() == []

    def test_a_file_with_content_is_evidence(self, tmp_path):
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "exp1_results.json").write_text('{"n": 18}')
        files = self._pipe(tmp_path)._experiment_evidence_files()
        assert len(files) == 1

    def test_an_escalation_report_is_not_evidence(self, tmp_path):
        """needs_human.json is a cry for help; 9df9e778 sailed into the
        analysis step with nothing else in results/."""
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "needs_human.json").write_text('{"urgency": "x"}')
        assert self._pipe(tmp_path)._experiment_evidence_files() == []


class TestSkipDeepResearchSkipsOnlyDeepResearch:
    """The flag names one sub-step; it must not swallow the phase.

    It used to early-return out of the whole research phase, so idea analysis
    and project_context.md never ran — while the experimenter prompt makes
    project_context.md mandatory in three places. Every skip_deep_research
    project (the webapp cheap-test preset included) sent its experimenter
    after a file that could not exist; an obedient model correctly reported
    the missing prerequisite instead of experimenting (1c9e7020).
    """

    def _pipe(self, tmp_path, skip, **files):
        p = PipelineMixin.__new__(PipelineMixin)
        p.state_dir = tmp_path
        p.config = {"skip_deep_research": skip}
        for name, present in files.items():
            if present:
                (tmp_path / f"{name}.md").write_text("x")
        return p

    def test_skip_flag_still_runs_the_phase_for_specialization(self, tmp_path):
        p = self._pipe(tmp_path, skip=True, idea=False, project_context=False)
        assert p._should_run_research_phase() is True

    def test_skip_flag_counts_only_the_dr_substep_as_done(self, tmp_path):
        p = self._pipe(tmp_path, skip=True, idea=True, project_context=True)
        # deep_research.md absent, but the flag stands in for it.
        assert p._should_run_research_phase() is False

    def test_without_the_flag_a_missing_dr_report_still_triggers_the_phase(self, tmp_path):
        p = self._pipe(tmp_path, skip=False, idea=True, project_context=True)
        assert p._should_run_research_phase() is True


class TestReviewIsTheFileNotTheClaim:
    """'Report saved to latest_review.md' is a pointer, not a review.

    95241962: delivery contract all-pass, then the reviewer claimed the save,
    wrote nothing, no score existed anywhere, and the run ended 0.0/failed
    with a perfectly good paper on disk.
    """

    def _step(self, tmp_path, file_text, answer, corrective_writes=None):
        from unittest.mock import patch
        import ark.pipeline as pl
        p = PipelineMixin.__new__(PipelineMixin)
        p.state_dir = tmp_path
        p.config = {"venue": "V", "latex_dir": "paper"}
        p.logs = []
        p.log = lambda m, l="INFO": p.logs.append((l, m))
        p.log_step = lambda *a, **k: None
        p.log_step_header = lambda *a, **k: None
        p._run_citation_verification = lambda: None
        p._archive_and_load_prior_review = lambda: ""
        p._build_visual_review_section = lambda: ""
        p.extract_issue_ids = lambda: []
        p.save_step_checkpoint = lambda *a, **k: None
        p.notify_progress = lambda *a, **k: None
        p.send_notification = lambda *a, **k: None
        p.parse_review_score = PipelineMixin.parse_review_score.__get__(p) \
            if hasattr(PipelineMixin, "parse_review_score") else (lambda t: 7.0 if "Overall Score" in (t or "") else 0.0)
        if file_text is not None:
            (tmp_path / "latest_review.md").write_text(file_text)

        calls = []
        def fake_agent(agent_type, task, timeout=None, **kw):
            calls.append(task[:60])
            if len(calls) > 1 and corrective_writes is not None:
                (tmp_path / "latest_review.md").write_text(corrective_writes)
            return answer
        p.run_agent = fake_agent
        from types import SimpleNamespace
        p.memory = SimpleNamespace(record_issues=lambda *a, **k: None,
                                   get_repeat_issues=lambda *a, **k: [])
        p.iteration = 1
        p.extract_issue_ids = lambda: []
        p._check_repeat_issues = lambda: None
        p.telegram = SimpleNamespace(is_configured=False)
        p.save_paper_state = lambda *a, **k: None
        p.log_file = tmp_path / "x.log"
        out, score, _ = PipelineMixin._step_review(p, 2, 5, 0, {'reviews': []}, 0.0)
        return out, score, calls

    def test_a_real_report_file_becomes_the_review_of_record(self, tmp_path):
        body = "Detailed review... Overall Score: 7/10\n" + "x" * 300
        out, score, calls = self._step(tmp_path, body, "report saved to latest_review.md")
        assert "Overall Score" in out
        assert len(calls) == 1                     # no corrective needed

    def test_a_claimed_but_empty_report_gets_one_corrective_pass(self, tmp_path):
        body = "Second try review. Overall Score: 6/10\n" + "y" * 300
        out, score, calls = self._step(tmp_path, None,
                                       "Review report has been saved to latest_review.md",
                                       corrective_writes=body)
        assert len(calls) == 2                     # corrective fired
        assert "Overall Score" in out
