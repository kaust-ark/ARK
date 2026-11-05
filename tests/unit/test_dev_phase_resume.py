"""A dev iteration cut off mid-way is re-entered, not skipped.

dev_phase_state.yaml saves `iteration = n` when iteration n STARTS. A resume
read that as "n is done" and began at n + 1; at the cap it went straight to
"Writing Initial Paper Draft". f028f3ba (relaunched while six SLURM jobs ran),
5c0e44f1 and b5fedacd all drafted papers on experiments that never finished,
and two of those drafts reported numbers no experiment had produced.
"""

from ark.pipeline import PipelineMixin


def _pipe(monkeypatch, state, max_iters=1, sufficient=True):
    p = PipelineMixin.__new__(PipelineMixin)
    p.logs, p.called, p.saved = [], [], []
    p.log = lambda msg, level="INFO": p.logs.append((level, msg))
    p.log_section = lambda *a, **k: None
    p.log_step = lambda *a, **k: None
    p._terminal_error = None
    p._send_dev_phase_telegram = lambda *a, **k: None
    p._load_findings_summary = lambda: ""
    p._experiment_approval_gate = lambda: p.called.append("approve")
    p.hooks = None
    p.config = {"max_dev_iterations": max_iters}
    p._load_dev_phase_state = lambda: state
    p._save_dev_phase_state = lambda s: p.saved.append(
        {k: (list(v) if isinstance(v, list) else v) for k, v in s.items()})
    monkeypatch.setattr(PipelineMixin, "_research_idea", "idea")

    def step(name):
        def _f(*a, **k):
            p.called.append((name, k.get("resumed")) if name == "run" else name)
        return _f

    p._plan_experiments = step("plan")
    p._run_experiments = step("run")
    p._analyze_results = step("analyze")
    p._evaluate_completeness = lambda *a, **k: (p.called.append("evaluate"), sufficient)[1]
    p._generate_all_figures = lambda: p.called.append("draft")
    p._write_initial_draft = lambda *a: None
    p._deliver_dev_phase = lambda *a: None
    return p


def test_a_fresh_dev_phase_runs_every_step(monkeypatch):
    p = _pipe(monkeypatch, {"iteration": 0, "status": "pending"})
    p._run_dev_phase()
    assert p.called == ["plan", "approve", ("run", False), "analyze", "evaluate", "draft"]


def test_a_cut_iteration_resumes_after_its_finished_steps(monkeypatch):
    p = _pipe(monkeypatch, {"iteration": 1, "status": "in_progress", "steps_done": ["plan"]})
    p._run_dev_phase()
    # The approved plan is kept; the run is told it is resuming.
    assert p.called == [("run", True), "analyze", "evaluate", "draft"]


def test_a_legacy_state_without_steps_redoes_the_iteration(monkeypatch):
    """What f028f3ba left on disk: started, never evaluated, no step record."""
    p = _pipe(monkeypatch, {"iteration": 1, "status": "in_progress"})
    p._run_dev_phase()
    assert p.called[:4] == ["plan", "approve", ("run", True), "analyze"]
    assert "evaluate" in p.called and p.called[-1] == "draft"


def test_a_finished_sufficient_iteration_goes_to_drafting(monkeypatch):
    p = _pipe(monkeypatch, {"iteration": 1, "status": "in_progress", "sufficient": True,
               "steps_done": ["plan", "run", "analyze", "evaluate"]}, max_iters=2)
    p._run_dev_phase()
    assert p.called == ["draft"]


def test_a_finished_insufficient_iteration_moves_on_to_the_next(monkeypatch):
    p = _pipe(monkeypatch, {"iteration": 1, "status": "in_progress", "sufficient": False,
               "steps_done": ["plan", "run", "analyze", "evaluate"]},
              max_iters=2, sufficient=True)
    p._run_dev_phase()
    assert p.called == ["plan", "approve", ("run", False), "analyze", "evaluate", "draft"]


def test_progress_is_saved_after_each_step(monkeypatch):
    p = _pipe(monkeypatch, {"iteration": 0, "status": "pending"})
    p._run_dev_phase()
    steps = [s.get("steps_done") for s in p.saved]
    assert ["plan"] in steps and ["plan", "run"] in steps
    assert p.saved[-1]["steps_done"] == ["plan", "run", "analyze", "evaluate"]
    assert p.saved[-1]["sufficient"] is True
