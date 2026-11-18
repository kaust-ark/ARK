import json
from types import SimpleNamespace

import pytest
import yaml

from ark.findings_schema import comparison_control_gaps, freeze_comparison_controls
from ark.pipeline import PipelineMixin
from ark.workflow import ExperimentIteration, ResearchDecision


CONTROL = {"label": "same scored cohort",
           "reference_source": "results/measurements.json#/reference/cohort",
           "treatment_source": "results/measurements.json#/variant/cohort"}


def record(root, reference, treatment):
    results = root / "results"
    results.mkdir(exist_ok=True)
    (results / "measurements.json").write_text(json.dumps({
        "reference": {"cohort": reference}, "variant": {"cohort": treatment},
    }))


@pytest.mark.parametrize(("reference", "treatment", "matches"), [
    ("held-out-2", "held-out-2", True), (42, 42.0, True), (False, False, True),
    ("A100", "H100", False), (True, 1, False), ("1", 1, False),
    (None, None, False), ("  ", "  ", False), ("unknown", "unknown", False),
    ([1], [1], False), ({"n": 1}, {"n": 1}, False), (float("nan"), float("nan"), False),
])
def test_matches_only_recorded_scalars(tmp_path, reference, treatment, matches):
    record(tmp_path, reference, treatment)
    assert (not comparison_control_gaps([CONTROL], tmp_path)) == matches


def test_bad_plan_fails_before_execution_and_never_truncates(tmp_path):
    path = tmp_path / "experiment_plan.yaml"
    for entries in (None, [dict(CONTROL, treatment_source="results/../paper.json#/cohort")],
                    [dict(CONTROL, treatment_source="results/./measurements.json#/%72eference/cohort")],
                    [dict(CONTROL, treatment_source="results/measurements.json#/bad~2")],
                    [dict(CONTROL, label=f"label {i}") for i in range(17)]):
        path.write_text(yaml.safe_dump({"comparison_controls": entries}))
        with pytest.raises(ValueError):
            freeze_comparison_controls(path, [], tmp_path)
    path.write_text("experiments: []")
    assert freeze_comparison_controls(path, [CONTROL], tmp_path) == [CONTROL]


def test_location_is_checked_again_after_run(tmp_path):
    path = tmp_path / "experiment_plan.yaml"
    control = dict(CONTROL, treatment_source="results/variant.json#/reference/cohort")
    path.write_text(yaml.safe_dump({"comparison_controls": [control]}))
    frozen = freeze_comparison_controls(path, [], tmp_path)
    record(tmp_path, "example", "example")
    alias = tmp_path / "results/variant.json"
    alias.hardlink_to(tmp_path / "results/measurements.json")
    assert "same raw field" in comparison_control_gaps(frozen, tmp_path)[0]
    alias.unlink()
    alias.symlink_to(path)
    assert comparison_control_gaps(frozen, tmp_path)


def test_frozen_control_survives_approval_resume_reanalysis_and_replanning(tmp_path):
    state_dir = tmp_path / "auto_research/state"
    state_dir.mkdir(parents=True)
    plan = state_dir / "experiment_plan.yaml"
    saved = []
    project = SimpleNamespace(state_dir=state_dir, code_dir=tmp_path, hooks=None,
                              log=lambda *args: None, log_section=lambda *args: None,
                              log_step=lambda *args: None, _send_dev_phase_telegram=lambda *a: None,
                              _abort_dev_on_terminal_error=lambda step: False,
                              _save_dev_phase_state=lambda state: saved.append(json.loads(json.dumps(state))),
                              _load_findings_summary=lambda: "", _analyze_results=lambda *args: None)
    project._plan_experiments = lambda *args: plan.write_text("experiments: []")
    project._experiment_approval_gate = lambda: plan.write_text(yaml.safe_dump({
        "comparison_controls": [dict(CONTROL, extraneous="discard this key")],
    }))
    state = {}
    experiment = ExperimentIteration(project, state, 1, 3, True, "idea", "")
    assert experiment.plan()
    assert saved[-1]["comparison_controls"] == [CONTROL] and saved[-1]["steps_done"] == ["plan"]
    plan.write_text("comparison_controls: []")  # plan edited after approval
    record(tmp_path, "split-a", "split-b")
    resumed = ExperimentIteration(project, saved[-1], 1, 3, True, "idea", "")
    assert resumed.remaining_steps() == ["run", "analyze"]
    project._run_experiments = lambda *a, **kw: None
    assert resumed.run() and resumed.analyze()
    project._evaluate_completeness = lambda *args: ResearchDecision("reanalyze_existing", ["Inspect saved pairs."])
    assert resumed.evaluate() is False
    assert saved[-1]["next_action"] == "reanalyze_existing"
    reuse = ExperimentIteration(project, saved[-1], 2, 3, False, "idea", "")
    assert reuse.remaining_steps() == ["analyze"] and reuse.analyze()
    project._evaluate_completeness = lambda *a: ResearchDecision("proceed_to_writing", [])
    assert reuse.evaluate() is False
    assert saved[-1]["next_action"] == "need_more_experiments"
    assert "same scored cohort" in saved[-1]["evidence_gaps"][0]
    final = ExperimentIteration(project, saved[-1], 3, 3, False, "idea", "")
    assert final.plan() and final.run() and final.analyze()
    assert saved[-1]["comparison_controls"] == [CONTROL]
    assert final.evaluate() is False and saved[-1]["sufficient"] is False


def test_draft_rechecks_metadata_after_checkpointed_positive_verdict(tmp_path):
    p = PipelineMixin()
    p.code_dir = tmp_path
    p._list_available_figures = p._load_findings_summary = lambda: ""
    p.load_paper_requirements = lambda: {}
    p.config = {}
    p._load_dev_phase_state = lambda: {"sufficient": True, "comparison_controls": [CONTROL]}
    prompts = []
    p.run_agent = lambda role, prompt, **kwargs: prompts.append(prompt)
    record(tmp_path, "A", "A")
    p._write_initial_draft("idea")
    assert "Unverified matched control" not in prompts[-1]
    (tmp_path / "results/measurements.json").unlink()
    p._write_initial_draft("idea")
    assert "same scored cohort" in prompts[-1]
    assert "does not exist" in prompts[-1] and "omit any attribution" in prompts[-1]


def test_new_matched_observation_resolves_a_missing_control(tmp_path):
    record(tmp_path, "holdout-1", None)
    project = SimpleNamespace(
        code_dir=tmp_path, hooks=None, _load_findings_summary=lambda: "",
        _evaluate_completeness=lambda *a: ResearchDecision("proceed_to_writing", []),
        _save_dev_phase_state=lambda state: None, _send_dev_phase_telegram=lambda *a: None,
        log=lambda *a: None, log_section=lambda *a: None, log_step=lambda *a: None,
    )
    state = {"comparison_controls": [CONTROL]}
    before = ExperimentIteration(project, state, 1, 2, False, "idea", "")
    assert before.evaluate() is False
    assert state["next_action"] == "need_more_experiments"
    record(tmp_path, "holdout-1", "holdout-1")
    after = ExperimentIteration(project, state, 2, 2, False, "idea", "")
    assert after.evaluate() is True and state["evidence_gaps"] == []


@pytest.mark.parametrize(("replacement", "followup_cohort", "sufficient"), [
    (True, "paired", True), (True, "different", False), (True, None, False), (False, "paired", False),
])
def test_followup_can_resolve_only_its_declared_predecessor(tmp_path, replacement, followup_cohort, sufficient):
    p = PipelineMixin()
    p.state_dir = tmp_path / "state"
    p.state_dir.mkdir()
    p.code_dir = tmp_path
    p.config = {}
    p.hooks = None
    p.log = p.log_step = p.log_section = p._send_dev_phase_telegram = lambda *a: None
    p._sync_db = p._plan_experiments = lambda *a, **kw: None
    p._list_available_figures = p._load_findings_summary = lambda: ""
    p.load_paper_requirements = lambda: {}
    p._evaluate_completeness = lambda *a: ResearchDecision("proceed_to_writing", [])
    plan = p.state_dir / "experiment_plan.yaml"
    state = {}
    plan.write_text(yaml.safe_dump({"comparison_controls": [CONTROL]}))
    first = ExperimentIteration(p, state, 1, 2, False, "idea", "")
    assert first.plan()
    record(tmp_path, "pilot-a", "pilot-b")
    pilot = (tmp_path / "results/measurements.json").read_bytes()
    assert first.evaluate() is False
    new = {key: value.replace("measurements.json", "followup.json") for key, value in CONTROL.items()}
    if replacement:
        new["replaces"] = {key: CONTROL[key] for key in ("reference_source", "treatment_source")}
    plan.write_text(yaml.safe_dump({"comparison_controls": [new]}))
    second = ExperimentIteration(p, state, 2, 2, False, "idea", "")
    assert second.plan()
    plan.unlink()  # Resume sees only the saved comparison declarations.
    resumed = ExperimentIteration(p, p._load_dev_phase_state(), 2, 2, False, "idea", "")
    (tmp_path / "results/followup.json").write_text(json.dumps({
        "reference": {"cohort": "paired"}, "variant": {"cohort": followup_cohort},
    }))
    assert resumed.evaluate() is sufficient
    assert pilot == (tmp_path / "results/measurements.json").read_bytes()
    prompts = []
    p.run_agent = lambda role, prompt, **kw: prompts.append(prompt)
    p._write_initial_draft("idea")
    assert CONTROL["reference_source"] in prompts[0] and "values differ" in prompts[0]
    if replacement:
        assert new["treatment_source"] in prompts[0] and "earlier pair" in prompts[0]


def test_replacement_requires_exact_prior_reference_and_new_evidence(tmp_path):
    path = tmp_path / "experiment_plan.yaml"
    old = {k: CONTROL[k] for k in ("reference_source", "treatment_source")}
    followup = dict(CONTROL, treatment_source="results/followup.json#/variant/cohort", replaces=old)
    for bad in (dict(followup, label="different"), dict(followup, replaces={}),
                dict(followup, replaces={**old, "treatment_source": "results/wrong.json#/a"}),
                dict(followup, treatment_source=old["treatment_source"]),
                dict(followup, reference_source=old["treatment_source"], treatment_source=old["reference_source"])):
        path.write_text(yaml.safe_dump({"comparison_controls": [bad]}))
        with pytest.raises(ValueError):
            freeze_comparison_controls(path, [CONTROL], tmp_path)
    path.write_text(yaml.safe_dump({"comparison_controls": [followup]}))
    frozen = freeze_comparison_controls(path, [CONTROL], tmp_path)
    assert freeze_comparison_controls(path, frozen, tmp_path) == frozen
    other = dict(followup, treatment_source="results/another.json#/variant/cohort")
    path.write_text(yaml.safe_dump({"comparison_controls": [other]}))
    with pytest.raises(ValueError, match="already been replaced"):
        freeze_comparison_controls(path, frozen, tmp_path)
