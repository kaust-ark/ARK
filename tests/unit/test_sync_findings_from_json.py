"""Unit tests for ark.findings_schema.sync_findings_from_json.

Prevention-at-source: the experimenter/planner author ``findings.json`` and ARK
regenerates the canonical ``findings.yaml`` from it. These tests pin the
conversion contract — including the two guarantees that make it safe:
malformed JSON must NOT clobber an existing findings.yaml, and the generated
YAML must always parse.
"""
import json

import yaml

from ark.findings_schema import sync_findings_from_json


def _write(p, text):
    p.write_text(text, encoding="utf-8")


# ── happy path ──────────────────────────────────────────────────────────


def test_converts_json_to_canonical_yaml(tmp_path):
    payload = {
        "coverage": {"item1": {"status": "done", "evidence": "results/a.json"}},
        "findings": [{"id": "f1", "result": "R", "metrics": {"tpr": 0.5}}],
    }
    _write(tmp_path / "findings.json", json.dumps(payload))

    converted, msgs = sync_findings_from_json(tmp_path)

    assert converted is True
    assert msgs and any("regenerated" in m for m in msgs)
    yaml_path = tmp_path / "findings.yaml"
    assert yaml_path.exists()
    parsed = yaml.safe_load(yaml_path.read_text())
    assert parsed == payload
    assert parsed["findings"][0]["metrics"]["tpr"] == 0.5


def test_generated_yaml_always_parses(tmp_path):
    """The exact structures that break hand-written YAML (a scalar with an
    inner ``: ``, a key that looks like a nested mapping) round-trip cleanly
    when ARK owns serialization."""
    payload = {
        "coverage": {"Experiment 1: Framework Design": {"status": "deferred"}},
        "notes": ["ratio: 3: 1 split was used"],
        "findings": [{"id": "f1"}],
    }
    _write(tmp_path / "findings.json", json.dumps(payload))

    converted, _ = sync_findings_from_json(tmp_path)
    assert converted is True
    # Must parse, and preserve the tricky key/scalar verbatim.
    parsed = yaml.safe_load((tmp_path / "findings.yaml").read_text())
    assert "Experiment 1: Framework Design" in parsed["coverage"]
    assert parsed["notes"] == ["ratio: 3: 1 split was used"]


# ── tolerant parsing ────────────────────────────────────────────────────


def test_tolerates_trailing_comma(tmp_path):
    _write(tmp_path / "findings.json",
           '{\n  "findings": [\n    {"id": "f1"},\n  ],\n}\n')
    converted, _ = sync_findings_from_json(tmp_path)
    assert converted is True
    parsed = yaml.safe_load((tmp_path / "findings.yaml").read_text())
    assert parsed["findings"] == [{"id": "f1"}]


def test_tolerates_line_comments(tmp_path):
    _write(tmp_path / "findings.json",
           '{\n  // top finding\n  "findings": [{"id": "f1"}]\n}\n')
    converted, _ = sync_findings_from_json(tmp_path)
    assert converted is True
    parsed = yaml.safe_load((tmp_path / "findings.yaml").read_text())
    assert parsed["findings"] == [{"id": "f1"}]


# ── safety: never clobber on bad JSON ───────────────────────────────────


def test_malformed_json_does_not_clobber_existing_yaml(tmp_path):
    existing = "findings:\n  - id: prior\n"
    _write(tmp_path / "findings.yaml", existing)
    # Unrecoverable JSON (unterminated string).
    _write(tmp_path / "findings.json", '{"findings": [{"id": "f1"')

    converted, msgs = sync_findings_from_json(tmp_path)

    assert converted is False
    assert msgs and any("not valid JSON" in m for m in msgs)
    # The prior findings.yaml is preserved byte-for-byte.
    assert (tmp_path / "findings.yaml").read_text() == existing


# ── legacy / empty inputs ───────────────────────────────────────────────


def test_absent_json_is_noop(tmp_path):
    existing = "findings:\n  - id: prior\n"
    _write(tmp_path / "findings.yaml", existing)

    converted, msgs = sync_findings_from_json(tmp_path)

    assert converted is False
    assert msgs == []
    assert (tmp_path / "findings.yaml").read_text() == existing


def test_empty_json_is_noop(tmp_path):
    _write(tmp_path / "findings.json", "   \n")
    converted, msgs = sync_findings_from_json(tmp_path)
    assert converted is False
    assert msgs == []
    assert not (tmp_path / "findings.yaml").exists()
