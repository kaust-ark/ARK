"""Replay a reviewer's directional comparison against the two cited measurements."""
import json

import pytest

from ark.workflow import ResearchDecision


@pytest.fixture
def project(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "baseline.json").write_text('{"times/ms":[3],"zero":0,"two":2}')
    (results / "trial.json").write_text('{"times/ms":[2],"gain_pct":98,"bool":true,"text":"2","nan":NaN}')
    return tmp_path


def decision(root, operation="relative_change_pct", predicted=-33.3, **fields):
    check = {"claim": "The treatment reduced time by 33.3%.", "predicted": predicted,
             "operation": operation, "reference_source": "results/baseline.json#/times~1ms/0",
             "treatment_source": "results/trial.json#/times~1ms/0", **fields}
    return review(root, [check])


def review(root, checks, contradictions=None):
    verdict = {"sufficient": True, "recommendation": "proceed_to_writing", "gaps": [],
               "contradictions": contradictions or [], "claim_checks": checks}
    return ResearchDecision.from_review(json.dumps(verdict), root, lambda *args: pytest.fail(str(args)))


@pytest.mark.parametrize(("operation", "predicted", "fields"), [
    ("difference", -1, {}),
    ("difference", 2, {"reference_source": "results/baseline.json#/zero"}),
    ("ratio", 0.67, {"decimal_places": 2}),
    ("relative_change_pct", -33.3, {"decimal_places": 1}),
    ("ratio", 1, {"reference_source": "results/baseline.json#/two"}),
])
def test_verified_comparison_can_proceed(project, operation, predicted, fields):
    assert decision(project, operation, predicted, **fields).action == "proceed_to_writing"


@pytest.mark.parametrize(("operation", "predicted", "fields", "reason"), [
    ("difference", 1, {}, "gives -1"),
    ("relative_change_pct", -33.3, {}, "approximately -33.333"),
    ("relative_change_pct", 98, {"observed": 98}, "claim reports 98"),
    ("ratio", 1.5, {}, "approximately 0.666"),
    ("relative_change_pct", 0, {"reference_source": "results/baseline.json#/zero"}, "zero reference"),
    ("ratio", 0, {"reference_source": "results/baseline.json#/zero"}, "zero reference"),
    ("product", 6, {}, "requires operation"),
    ("ratio", None, {}, "numeric predicted"),
    ("ratio", float("inf"), {}, "numeric predicted"),
    ("ratio", 1, {"treatment_source": "results/missing.json#/time"}, "does not exist"),
    ("ratio", 1, {"reference_source": "results/trial.json#/missing"}, "was not found"),
    ("ratio", 1, {"treatment_source": None}, "project-relative"),
    ("ratio", 1, {"treatment_source": "results/trial.json#/times~1ms"}, "finite numeric"),
    ("ratio", 1, {"treatment_source": "results/trial.json#/bool"}, "finite numeric"),
    ("ratio", 1, {"treatment_source": "results/trial.json#/text"}, "finite numeric"),
    ("ratio", 1, {"treatment_source": "results/trial.json#/nan"}, "finite numeric"),
    ("ratio", 1, {"decimal_places": False}, "decimal_places"),
    ("ratio", 1, {"decimal_places": -1}, "decimal_places"),
    ("ratio", 1, {"observed_source": "results/trial.json#/gain_pct"}, "both raw operands"),
])
def test_unsupported_or_contradicted_checks_override_positive_vote(project, operation, predicted, fields, reason):
    verdict = decision(project, operation, predicted, **fields)
    assert verdict.action == "correct_findings" and reason in verdict.gaps[0]


def test_same_measurement_cannot_verify_two_arms(project):
    (project / "results/alias.json").symlink_to(project / "results/baseline.json")
    verdict = decision(project, "ratio", 1, treatment_source="results/alias.json#/%74imes~1ms/0")
    assert verdict.action == "correct_findings" and "distinct" in verdict.gaps[0]


def test_structured_contradiction_replays_the_reported_comparison(project):
    verdict = review(project, [], contradictions=[{
        "issue": "The exact speed estimate is disputed.", "operation": "ratio", "predicted": 2,
        "reference_source": "results/baseline.json#/times~1ms/0", "treatment_source": "results/trial.json#/times~1ms/0",
    }])
    assert verdict.action == "correct_findings"
    assert any("claim reports 2" in gap for gap in verdict.gaps)


def test_csv_line_cannot_substitute_for_a_numeric_comparison_operand(project):
    (project / "results/trial.csv").write_text("time_ms\n2\n")
    verdict = decision(project, "difference", -1, treatment_source="results/trial.csv:2")
    assert verdict.action == "correct_findings" and "not independently verified" in verdict.gaps[0]


def test_unreadable_or_oversize_json_does_not_confirm_comparison(project, monkeypatch):
    raw = project / "results/trial.json"
    raw.write_text("broken")
    assert "could not be parsed" in decision(project).gaps[0]
    monkeypatch.setattr("ark.findings_schema._MAX_RESULT_BYTES", 1)
    assert "size limit" in decision(project).gaps[0]


def test_rounded_ratio_uses_exact_operands_and_rounds_ties_away_from_zero(project):
    (project / "results/trial.json").write_text('{"times/ms":[-3.375]}')
    assert decision(project, "ratio", -1.13, decimal_places=2).action == "proceed_to_writing"
    assert "gives -1.13" in decision(project, "ratio", -1.12, decimal_places=2).gaps[0]


def test_json_prediction_keeps_decimal_precision_and_never_rounds_silently(project):
    check = {"claim": "Times are equal.", "operation": "ratio", "predicted": "PREDICTION",
             "reference_source": "results/baseline.json#/two",
             "treatment_source": "results/trial.json#/times~1ms/0"}
    output = json.dumps({"sufficient": True, "recommendation": "proceed_to_writing", "gaps": [],
                         "contradictions": [], "claim_checks": [check]})
    verdict = ResearchDecision.from_review(output.replace('"PREDICTION"', '1.0000000000000001'), project, print)
    assert verdict.action == "correct_findings" and "1.0000000000000001" in verdict.gaps[0]


def test_ratio_with_extreme_exponent_abstains_without_expanding_the_number(project):
    (project / "results/trial.json").write_text('{"times/ms":[1e999999999]}')
    assert "exact-check limit" in decision(project, "ratio", 2).gaps[0]


@pytest.mark.parametrize(("raw", "predicted", "fields", "accepted"), [
    ("0.9473684210526315", 0.9474, {"decimal_places": 4}, True),
    ("0.9824561403508771", 0.9825, {"decimal_places": 4}, True),
    ("-1.125", -1.13, {"decimal_places": 2}, True),
    ("-1.125", -1.12, {"decimal_places": 2}, False),
    ("0.9473684210526315", 0.9474, {}, False),
    ("0.9473684210526315", 0.94742, {"decimal_places": 4}, False),
    ("1.125", 1.13, {"decimal_places": True}, False),
    ("1", 1, {"decimal_places": -1}, False),
    ("1", 1, {"decimal_places": 101}, False),
    ("1", 1, {"decimal_places": None}, False),
    ("1", "1", {"decimal_places": 0}, False),
    ("1e999999999", 1, {"decimal_places": 1}, False),
    ("true", 1, {"decimal_places": 0}, False),
    ('"1"', 1, {"decimal_places": 0}, False),
    ("1", 1, {"decimal_places": 0, "treatment_source": "results/trial.json#/value"}, False),
    ("1", 1, {"decimal_places": 0, "operation": "ratio"}, False),
    ("1", 1, {"decimal_places": 0, "observed_source": "results/missing.json#/value"}, False),
    ("1", 1, {"decimal_places": 0, "observed_source": "results/trial.csv:2"}, False),
])
def test_scalar_claims_round_only_the_raw_number_at_explicit_precision(project, raw, predicted, fields, accepted):
    (project / "results/trial.json").write_text('{"value":' + raw + '}')
    (project / "results/trial.csv").write_text("value\n1\n")
    check = {"claim": "Measured value", "predicted": predicted,
             "observed_source": "results/trial.json#/value", **fields}
    assert review(project, [check]).action == ("proceed_to_writing" if accepted else "correct_findings")
