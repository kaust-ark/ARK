"""Tests for ``ark.quality`` (raw-log sanity) and ``ark.findings_schema``.

The highest-value regression is the safeclaw run where the L2 judge
silently default-allowed: nearly every intercept record in that file
has a ``reason`` mentioning ``Expecting value``. If ``raw_log_sanity``
catches it, gate B would have prevented the bad run from being
reported in the paper.

Rather than depend on the live project path (which won't exist in CI),
the test synthesises a minimal fixture that matches the real file's
shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ark.quality import (
    Anomaly,
    format_for_prompt,
    raw_log_sanity,
    registered_rules,
    register_rule,
)
from ark.findings_schema import validate_findings


# ─── Fixtures ─────────────────────────────────────────────────────────────


def _write_intercepts(path: Path, *, parse_failure_rate: float) -> None:
    """Write an intercepts.jsonl-shaped file mirroring the real schema.

    Each line carries a ``firewall_intercepts`` list whose inner records
    have a ``reason`` field — matching the safeclaw run07 shape.
    """
    n_records = 20
    n_bad = int(n_records * parse_failure_rate)
    lines = []
    for i in range(n_records):
        intercept = {
            "ts": 1776796480.0 + i,
            "tool": "exec",
            "args_preview": f"{{\"command\": \"cmd{i}\"}}",
            "verdict": "allow",
            "rule": None,
            "reason": (
                "LLM escalation exception: Expecting value: line 1 column 1 (char 0)"
                if i < n_bad
                else "rules pass"
            ),
            "layer": "llm",
        }
        record = {
            "scenario_id": f"scenario_{i}",
            "firewall_intercepts": [intercept],
        }
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── quality.raw_log_sanity ───────────────────────────────────────────────


def test_raw_log_sanity_missing_dir(tmp_path: Path) -> None:
    # Missing dirs must never raise.
    assert raw_log_sanity(tmp_path / "doesnotexist") == []


def test_raw_log_sanity_empty_dir(tmp_path: Path) -> None:
    # Empty results dir should also be clean.
    (tmp_path / "empty").mkdir()
    assert raw_log_sanity(tmp_path / "empty") == []


def test_judge_parse_failure_catches_silent_l2(tmp_path: Path) -> None:
    """Regression: the safeclaw-v3 L2 silent-failure pattern must fire."""
    results = tmp_path / "results" / "phase3" / "run07_skills"
    results.mkdir(parents=True)
    _write_intercepts(results / "intercepts.jsonl", parse_failure_rate=1.0)

    anomalies = raw_log_sanity(tmp_path / "results")
    parse_anoms = [a for a in anomalies if a.rule_id == "judge_parse_failure"]
    assert len(parse_anoms) == 1
    a = parse_anoms[0]
    assert a.severity == "block"
    assert "intercepts.jsonl" in a.location
    assert "Expecting value" in a.evidence


def test_judge_parse_failure_quiet_when_healthy(tmp_path: Path) -> None:
    """Healthy judge responses must not trigger the rule."""
    results = tmp_path / "results"
    results.mkdir()
    _write_intercepts(results / "intercepts.jsonl", parse_failure_rate=0.1)
    anomalies = raw_log_sanity(results)
    assert [a for a in anomalies if a.rule_id == "judge_parse_failure"] == []


def test_judge_parse_failure_ignores_small_samples(tmp_path: Path) -> None:
    """Under 10 records, too noisy to flag — rule must stay silent."""
    results = tmp_path / "results"
    results.mkdir()
    records = [
        {"firewall_intercepts": [{"reason": "Expecting value"}]}
        for _ in range(5)
    ]
    (results / "small.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    anomalies = raw_log_sanity(results)
    assert [a for a in anomalies if a.rule_id == "judge_parse_failure"] == []


def test_nonzero_stderr_catches_traceback(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "slurm_12345.err").write_text(
        "Traceback (most recent call last):\n"
        '  File "exp.py", line 42, in <module>\n'
        "    main()\n"
        "ValueError: bad thing happened with enough bytes to clear threshold\n"
        + ("x" * 80)
    )
    anomalies = raw_log_sanity(results)
    stderr_anoms = [a for a in anomalies if a.rule_id == "nonzero_stderr"]
    assert len(stderr_anoms) == 1
    assert "Traceback" in stderr_anoms[0].evidence


def test_nonzero_stderr_ignores_empty_and_tiny(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "empty.err").write_text("")
    (results / "tiny.err").write_text("ok")
    assert raw_log_sanity(results) == []


def test_buggy_rule_does_not_crash_pipeline(tmp_path: Path) -> None:
    """A rule that raises must surface as an internal_error anomaly."""
    (tmp_path / "x").mkdir()

    def broken_rule(_dir: Path):
        raise RuntimeError("boom")

    out = raw_log_sanity(tmp_path / "x", rules=[broken_rule])
    assert len(out) == 1
    assert out[0].rule_id.endswith(":internal_error")
    assert out[0].severity == "warn"


def test_format_for_prompt_empty() -> None:
    assert format_for_prompt([]) == ""


def test_format_for_prompt_renders_both_severities() -> None:
    anomalies = [
        Anomaly(
            rule_id="judge_parse_failure",
            severity="block",
            location="results/x.jsonl",
            message="50% bad",
            evidence="Expecting value",
        ),
        Anomaly(
            rule_id="nonzero_stderr",
            severity="warn",
            location="results/slurm.err",
            message="traceback",
            evidence="Traceback",
        ),
    ]
    rendered = format_for_prompt(anomalies)
    assert "BLOCKING (1)" in rendered
    assert "WARN (1)" in rendered
    assert "judge_parse_failure" in rendered
    assert "nonzero_stderr" in rendered


def test_rule_registry_nonempty() -> None:
    # Module-level registration must stick so pipeline callers get defaults.
    assert len(registered_rules()) >= 2


# ─── findings_schema.validate_findings ────────────────────────────────────


def _write_findings(path: Path, findings: list) -> None:
    path.write_text(yaml.safe_dump({"findings": findings}))


def test_validate_findings_missing_file(tmp_path: Path) -> None:
    assert validate_findings(tmp_path / "nope.yaml") == []


def test_validate_findings_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "findings.yaml"
    p.write_text("")
    assert validate_findings(p) == []


def test_validate_findings_legacy_no_metrics_clean(tmp_path: Path) -> None:
    """A pre-existing free-form findings.yaml without metrics stays clean."""
    p = tmp_path / "findings.yaml"
    _write_findings(p, [
        {"id": "f1", "experiment": "e1", "result": "qualitative observation"},
    ])
    assert validate_findings(p) == []


def test_validate_findings_metrics_without_source_warns(tmp_path: Path) -> None:
    p = tmp_path / "findings.yaml"
    _write_findings(p, [
        {"id": "f1", "experiment": "e1", "metrics": {"tpr": 0.23}},
    ])
    violations = validate_findings(p)
    fields = {v.field for v in violations}
    assert "source" in fields
    assert "construct" in fields
    assert all(v.severity == "warn" for v in violations)


def test_validate_findings_good_entry_clean(tmp_path: Path) -> None:
    results_dir = tmp_path / "results" / "run1"
    results_dir.mkdir(parents=True)
    (results_dir / "tpr.json").write_text('{"tpr": 0.23}')
    p = tmp_path / "findings.yaml"
    _write_findings(p, [{
        "id": "f1",
        "experiment": "e1",
        "metrics": {"tpr": 0.23},
        "source": "results/run1/tpr.json#tpr",
        "construct": "TPR where denominator = tool_calls emitted",
    }])
    assert validate_findings(p, project_root=tmp_path) == []


def test_validate_findings_dangling_source(tmp_path: Path) -> None:
    p = tmp_path / "findings.yaml"
    _write_findings(p, [{
        "id": "f1",
        "metrics": {"tpr": 0.23},
        "source": "results/ghost.json#tpr",
        "construct": "x",
    }])
    violations = validate_findings(p, project_root=tmp_path)
    msgs = " ".join(v.message for v in violations)
    assert "does not exist" in msgs


def test_validate_findings_malformed_source_string(tmp_path: Path) -> None:
    p = tmp_path / "findings.yaml"
    _write_findings(p, [{
        "id": "f1",
        "metrics": {"tpr": 0.23},
        "source": "not a valid reference string with spaces",
        "construct": "x",
    }])
    violations = validate_findings(p)
    msgs = " ".join(v.message for v in violations)
    assert "format" in msgs


def test_validate_findings_parse_error_blocks(tmp_path: Path) -> None:
    p = tmp_path / "findings.yaml"
    p.write_text(":\n: :\nnot yaml: [")
    violations = validate_findings(p)
    assert any(v.severity == "block" for v in violations)
