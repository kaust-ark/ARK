"""Exercise decisions at the boundary between a saved experiment and a paper."""
import json

import pytest

from ark.orchestrator.state import StateManager
from ark.pipeline import PipelineMixin


@pytest.fixture
def project(tmp_path):
    p = PipelineMixin()
    p.code_dir = tmp_path
    p.state_dir = tmp_path / "auto_research/state"
    p.state_dir.mkdir(parents=True)
    (tmp_path / "results").mkdir()
    p.config = {"max_dev_iterations": 2, "idea": "Is the new method faster?"}
    p._terminal_error = None
    p.hooks = None
    p.events = []
    p._load_findings_summary = StateManager(p.state_dir).load_findings_summary
    p.log = p.log_step = p.log_section = p.log_step_header = lambda *args: None
    p._sync_db = p._send_dev_phase_telegram = lambda *args, **kwargs: None
    p._experiment_approval_gate = lambda: p.events.append("approve")
    p._run_experiments = lambda *a, **kw: p.events.append("run")
    p._plan_experiments = lambda *a: p.events.append(("plan", a[-1]))
    p._generate_all_figures = lambda: p.events.append("figures")
    p._list_available_figures = lambda: ""
    p.load_paper_requirements = lambda: {}
    p._deliver_dev_phase = lambda *a: p.events.append("deliver")
    return p


def finding(value):
    return {"findings": [{"id": "f1", "result": "One run on one machine.",
                          "metrics": {"time": value}, "source": "results/raw.json#time",
                          "metric_sources": {"time": "results/raw.json#/time"}}]}


def review(action="proceed_to_writing", **evidence):
    return json.dumps({"sufficient": action == "proceed_to_writing", "recommendation": action,
                       "contradictions": [], "claim_checks": [], "gaps": [], **evidence})


def test_analysis_repairs_existing_measurements_and_routes_open_questions(project):
    p = project
    (p.code_dir / "results/raw.json").write_text('{"time": 2}')
    gap = "The baseline has no repeated timings."
    analyzed = 0

    def role(name, prompt, **kwargs):
        nonlocal analyzed
        if name == "writer":
            p.events.append(("writer", prompt))
        elif "Evaluate whether" in prompt:
            return review("need_more_experiments", gaps=[gap])
        else:
            analyzed += 1
            if analyzed == 2:
                assert "contains 2" in prompt and "Do not submit" in prompt
            (p.state_dir / "findings.json").write_text(json.dumps(finding(92 if analyzed == 1 else 2)))
        return "ok"

    p.run_agent = role
    p._run_dev_phase()
    assert p.events.count("run") == 2  # one per scientific iteration; none for the analysis repair
    assert analyzed == 3
    plans = [event for event in p.events if isinstance(event, tuple) and event[0] == "plan"]
    assert gap in plans[1][1]
    writer = next(e[1] for e in p.events if isinstance(e, tuple) and e[0] == "writer")
    assert gap in writer and "Source: results/raw.json" in writer
    assert p._load_dev_phase_state()["evidence_gaps"] == [gap]


def test_repeated_contradiction_preserves_run_checkpoint_on_continue(project):
    p = project
    (p.code_dir / "results/raw.json").write_text('{"time": 2}')
    responses = iter([92, 92, 2])

    def role(name, prompt, **kwargs):
        if name == "writer":
            return "draft"
        if "Evaluate whether" in prompt:
            return review()
        (p.state_dir / "findings.json").write_text(json.dumps(finding(next(responses))))
        return "analyzed"

    p.run_agent = role
    p._run_dev_phase()
    assert p._load_dev_phase_state()["steps_done"] == ["plan", "run"]
    assert "contains 2" in p._terminal_error
    assert "deliver" not in p.events
    p._terminal_error = None
    p._run_dev_phase()
    assert p.events.count("run") == 1 and p.events.count("deliver") == 1


@pytest.mark.parametrize("invalid", [
    '{"sufficient":"false"}', 'broken {"sufficient": true,',
    '{"sufficient":true}', review("need_more_experiments", sufficient=True),
])
def test_unusable_verdict_retries_evaluation_only(project, invalid):
    p = project
    p._save_dev_phase_state({"status": "in_progress", "iteration": 1, "steps_done": ["plan", "run", "analyze"]})
    p.run_agent = lambda *a, **kw: invalid
    p._run_dev_phase()
    assert p._terminal_error and not p.events
    assert p._load_dev_phase_state()["steps_done"] == ["plan", "run", "analyze"]
    p._terminal_error = None
    p.run_agent = lambda *a, **kw: review()
    p._run_dev_phase()
    assert p.events == ["figures", "deliver"]


def test_review_finishes_in_the_original_session_and_checks_the_recovered_verdict(project):
    (project.code_dir / "results/raw.json").write_text('{"visits": 1}')
    calls = []

    def reviewer(role, task, *, outcome_out, session_out, continue_session, **options):
        calls.append(continue_session)
        outcome_out["completed"] = True
        if continue_session is None:
            session_out.append("review-with-observations")
            return "I found a counterexample. Let me construct the evaluation JSON:"
        assert "Do not call tools" in task and "counterexamples" in task
        return json.dumps({"sufficient": True, "recommendation": "proceed_to_writing",
                           "contradictions": [{"finding_id": "f1", "issue": "The proposed cost conflicts with the count."}],
                           "gaps": [], "claim_checks": [
                               {"claim": "The first input requires two visits.", "predicted": 2,
                                "observed_source": "results/raw.json#/visits"}]})

    project.run_agent = reviewer
    verdict = project._evaluate_completeness("How many visits?", "")
    assert calls == [None, "review-with-observations"]
    assert verdict.action == "correct_findings"
    assert any("f1" in gap and "conflicts" in gap for gap in verdict.gaps)
    assert any("contains 1" in gap for gap in verdict.gaps)
    assert project._terminal_error is None


@pytest.mark.parametrize("last_reply", [
    "Let me finish the evaluation.",
    '{"sufficient":true}',
    '{"sufficient":false,"recommendation":"need_more_experiments","contradictions":[],"claim_checks":[],"gaps":[]}',
])
def test_review_repair_is_bounded_and_preserves_saved_steps(project, last_reply):
    calls = []

    def reviewer(role, task, *, outcome_out, session_out, continue_session, **options):
        calls.append(continue_session)
        session_out.append("saved-review")
        outcome_out["completed"] = True
        return last_reply if continue_session else "Here are my observations."

    project.run_agent = reviewer
    done = ["plan", "run", "analyze"]
    project._save_dev_phase_state({"status": "in_progress", "iteration": 1, "steps_done": done})
    project._run_dev_phase()
    assert calls == [None, "saved-review"]
    assert project._terminal_error and project.events == []
    assert project._load_dev_phase_state()["steps_done"] == done


def test_interrupted_review_does_not_use_an_apparently_valid_partial_verdict(project):
    calls = []

    def reviewer(role, task, *, outcome_out, session_out, continue_session, **options):
        calls.append(continue_session)
        session_out.append("interrupted-review")
        outcome_out["completed"] = False
        return '{"sufficient":true}'

    project.run_agent = reviewer
    assert project._evaluate_completeness("Is there a difference?", "") is None
    assert calls == [None] and "stopped" in project._terminal_error


def test_failed_job_logs_do_not_count_as_experiments(project):
    results = project.code_dir / "results"
    (results / "slurm-87732.err").write_text("oom_kill")
    (results / "slurm-87732.out").write_text("starting Python")
    assert project._experiment_evidence_files() == []
    (results / "measurements.tsv").write_text("n\ttime\n2\t5\n")
    assert [p.name for p in project._experiment_evidence_files()] == ["measurements.tsv"]


@pytest.mark.parametrize("runtime", ["python", "dsh"])
def test_analysis_only_round_survives_interrupt_without_planning_or_submitting(project, monkeypatch, runtime):
    from datetime import datetime, timedelta
    import os
    from ark.orchestrator.dsh_workflow import run_workflow

    p = project
    if runtime == "dsh":
        interpreter = os.getenv("ARK_DSH_TEST_PYTHON")
        if not interpreter:
            pytest.skip("ARK_DSH_TEST_PYTHON needed for pinned DSH")
        monkeypatch.setenv("ARK_DSH_PYTHON", interpreter)
    p.iteration = 0
    p._prepare_research_workspace = p._should_run_dev_phase = lambda: True
    p._should_run_research_phase = lambda: False
    p._run_research_phase = lambda: pytest.fail("research reran")
    p._handle_terminal_error = lambda *a, **kw: False
    p._get_max_iteration_target = lambda: 0
    p.max_end_time = datetime.now() + timedelta(minutes=2)
    p._stop_requested = False
    p._write_initial_draft = lambda *a: p.events.append("draft")
    p._save_dev_phase_state({"iteration": 1, "status": "in_progress", "steps_done": ["plan", "run", "analyze"]})

    gap = "Compute paired differences from the saved trials."
    analyses = []
    def analyze(questions):
        analyses.append(questions)
        if len(analyses) == 1:
            p._terminal_error = "Temporary provider failure"

    p._analyze_results = analyze
    def role(name, prompt, **kw):
        assert "Evaluate whether" in prompt
        if analyses:
            return review()
        return review("reanalyze_existing", gaps=[gap])

    p.run_agent = role
    run = p._run_dev_phase if runtime == "python" else lambda: run_workflow(p)
    run()
    assert p.events == []
    checkpoint = p._load_dev_phase_state()
    assert checkpoint["iteration"] == 2 and checkpoint["reuse_results"] and checkpoint["steps_done"] == []
    p._terminal_error = None
    run()
    assert analyses == [[gap], [gap]]
    assert p.events == ["figures", "draft", "deliver"]
    assert p._load_dev_phase_state()["steps_done"] == ["analyze", "evaluate"]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_counterexample_at_budget_limit_cannot_be_waived_by_model_vote(project, repair_succeeds):
    p = project
    p.config["max_dev_iterations"] = 1
    p._save_dev_phase_state({"iteration": 1, "status": "in_progress", "steps_done": ["plan", "run", "analyze"]})
    (p.code_dir / "results/lookup.json").write_text('{"2": {"count": 1}}')
    corrections = []
    def analyze(issues):
        corrections.append(issues)
        # The worker could be interrupted at this point.
        assert p._load_dev_phase_state()["steps_done"] == ["plan", "run"]
    p._analyze_results = analyze
    def respond(role, prompt, **kw):
        if role == "writer":
            return "draft"
        return review(claim_checks=[{"claim": "At n=2", "predicted": 1 if corrections and repair_succeeds else 2,
                                     "observed_source": "results/lookup.json#/2/count"}])
    p.run_agent = respond
    p._run_dev_phase()
    assert len(corrections) == 1
    assert "contains 1" in corrections[0][0] and "reports 2" in corrections[0][0]
    if repair_succeeds:
        assert p.events == ["figures", "deliver"]
    else:
        assert "contradict" in p._terminal_error and p.events == []
        assert p._load_dev_phase_state()["steps_done"] == ["plan", "run"]

def test_structured_contradiction_can_override_positive_verdict(project):
    from ark.workflow import ResearchDecision
    raw = project.code_dir / "results/results.json"
    raw.write_text('{"2": 1}')
    output = review(contradictions=[
        {"finding_id": "f1", "claim": "The formula predicts two for input 2",
         "predicted": 2, "observed_source": "results/results.json#/2", "observed": 1},
    ])
    decision = ResearchDecision.from_review(output, project.code_dir, project.log)
    assert decision.action == "correct_findings"
    assert any("contains 1" in issue for issue in decision.gaps)

def test_findings_summary_preserves_metric_identity_and_denominator(project):
    import yaml
    (project.state_dir / "findings.yaml").write_text(yaml.safe_dump({"findings": [
        {"id": "f1", "result": "84 examples classified correctly", "metrics": {"accuracy": 0.84},
         "construct": "84 of 100 examples", "source": "results/counts.json"}
    ]}))
    summary = project._load_findings_summary()
    assert "Observation 1" in summary and "f1:" not in summary.lower()
    assert "accuracy=0.84" in summary and "84 of 100" in summary and "results/counts.json" in summary
