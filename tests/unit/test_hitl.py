"""Unit tests for the HITL helpers in ``ark/hitl/utils.py``.

These cover the schema-coercion + decision-persistence helpers that
back ``_check_human_intervention``. The actual Telegram rendering and
reply routing go through ``ask_user_decision`` on the orchestrator and
are covered by the Telegram framework tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ark.hitl.utils import (
    _append_hitl_history,
    _coerce_hitl_options,
    _normalise_needs_human,
    _update_hitl_decisions,
)


# ── _normalise_needs_human / _coerce_hitl_options ───────────────────


def test_normalise_structured_options_preserved():
    raw = {
        "urgency": "blocker",
        "summary": "Need API key",
        "stage": "Phase 1",
        "options": [
            {"id": "1", "title": "Provide key", "consequence": "unblocks Phase 1"},
            {"id": "2", "title": "Defer", "consequence": "marks deferred"},
        ],
        "default_option": "2",
        "timeout_minutes": 30,
    }
    req = _normalise_needs_human(raw)
    assert req["summary"] == "Need API key"
    assert req["stage"] == "Phase 1"
    assert len(req["options"]) == 2
    assert req["options"][0]["consequence"] == "unblocks Phase 1"
    assert req["default_option"] == "2"
    assert req["timeout_minutes"] == 30


def test_normalise_legacy_letter_options_split():
    raw = {
        "reason": "Apptainer not on compute node",
        "phase": 3,
        "operator_action_needed": (
            "Either (a) install apptainer, or (b) allow sbatch routing, "
            "or (c) defer Phase 3"
        ),
        "tested_cmd": "command -v apptainer  →  not found",
    }
    req = _normalise_needs_human(raw)
    assert req["summary"] == "Apptainer not on compute node"
    assert req["stage"] == "3"
    titles = [o["title"] for o in req["options"]]
    assert titles == ["install apptainer", "allow sbatch routing", "defer Phase 3"]
    # Legacy tested_cmd lifts into evidence
    assert req["evidence"]["tested_commands"] == ["command -v apptainer  →  not found"]


def test_normalise_legacy_needed_items_list():
    raw = {
        "summary": "Credentials required",
        "needed_items": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
    }
    req = _normalise_needs_human(raw)
    assert len(req["options"]) == 2
    assert req["options"][0]["title"] == "ANTHROPIC_API_KEY"


def test_normalise_no_options_yields_empty_list():
    req = _normalise_needs_human({"summary": "Vague help"})
    assert req["options"] == []
    assert req["timeout_minutes"] == 60  # default


def test_normalise_string_evidence_coerced_to_dict():
    req = _normalise_needs_human({
        "summary": "x",
        "evidence": "raw stderr dump",
    })
    assert req["evidence"] == {"freeform": "raw stderr dump"}


def test_coerce_options_trims_trailing_conjunction():
    opts = _coerce_hitl_options({
        "operator_action_needed": "(a) try X, or (b) try Y, or (c) give up",
    })
    # "(a) try X, or" becomes "try X" — trailing ", or" trimmed
    assert [o["title"] for o in opts] == ["try X", "try Y", "give up"]


# ── _append_hitl_history ─────────────────────────────────────────────


def test_append_history_creates_jsonl_with_entry(tmp_path: Path):
    req = _normalise_needs_human({"summary": "Blocked on X"})
    chosen = {"id": "2", "title": "Defer", "consequence": "weakens claim"}
    path = _append_hitl_history(
        tmp_path, req, reply="2", chosen=chosen,
        decision_text="Selected option 2: Defer", stage_label="Execute",
    )
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["stage"] == "Execute"
    assert entry["chosen_option"]["id"] == "2"
    assert entry["decision_text"].startswith("Selected option 2")


def test_append_history_appends_across_calls(tmp_path: Path):
    req = _normalise_needs_human({"summary": "X"})
    _append_hitl_history(tmp_path, req, "1", None, "free text 1", "Execute")
    _append_hitl_history(tmp_path, req, "2", None, "free text 2", "Execute")
    lines = (tmp_path / "results" / "needs_human_history.jsonl").read_text().splitlines()
    assert len(lines) == 2


# ── _update_hitl_decisions ───────────────────────────────────────────


def test_update_decisions_creates_yaml_with_record(tmp_path: Path):
    req = _normalise_needs_human({"summary": "Need API key", "stage": "Phase 1"})
    chosen = {"id": "1", "title": "Provide key", "consequence": "unblocks"}
    path = _update_hitl_decisions(
        tmp_path, req, chosen, "Selected option 1: Provide key", "Execute",
    )
    data = yaml.safe_load(path.read_text())
    assert len(data["decisions"]) == 1
    rec = data["decisions"][0]
    assert rec["chosen_option"]["id"] == "1"
    assert rec["free_text"] is None
    assert rec["summary"] == "Need API key"


def test_update_decisions_replaces_same_id(tmp_path: Path):
    """Re-asking the same blocker should overwrite, not append."""
    req = _normalise_needs_human({"summary": "Same blocker", "stage": "Phase 1"})
    _update_hitl_decisions(tmp_path, req, None, "first reply", "Execute")
    _update_hitl_decisions(tmp_path, req,
                           {"id": "2", "title": "t", "consequence": ""},
                           "Selected option 2", "Execute")
    data = yaml.safe_load((tmp_path / "hitl_decisions.yaml").read_text())
    assert len(data["decisions"]) == 1
    assert data["decisions"][0]["chosen_option"]["id"] == "2"


def test_update_decisions_keeps_distinct_blockers_separate(tmp_path: Path):
    req1 = _normalise_needs_human({"summary": "Blocker A", "stage": "Phase 1"})
    req2 = _normalise_needs_human({"summary": "Blocker B", "stage": "Phase 2"})
    _update_hitl_decisions(tmp_path, req1, None, "reply A", "Execute")
    _update_hitl_decisions(tmp_path, req2, None, "reply B", "Execute")
    data = yaml.safe_load((tmp_path / "hitl_decisions.yaml").read_text())
    assert len(data["decisions"]) == 2


def test_update_decisions_free_text_when_no_option(tmp_path: Path):
    req = _normalise_needs_human({"summary": "Vague"})
    _update_hitl_decisions(tmp_path, req, None, "just use mcnode35", "Execute")
    rec = yaml.safe_load((tmp_path / "hitl_decisions.yaml").read_text())["decisions"][0]
    assert rec["chosen_option"] is None
    assert rec["free_text"] == "just use mcnode35"
