"""Tests for ark.memory.SimpleMemory."""

import yaml
from pathlib import Path

from ark.memory import SimpleMemory


class TestScoreTracking:
    def test_record_score(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.record_score(5.0)
        assert mem.scores == [5.0]
        assert mem.best_score == 5.0

    def test_best_score_updates(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.record_score(5.0)
        mem.record_score(7.0)
        mem.record_score(6.0)
        assert mem.best_score == 7.0

    def test_stagnation_increments(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.record_score(5.0)
        mem.record_score(5.1)  # delta < 0.3
        mem.record_score(5.0)
        assert mem.stagnation_count == 2

    def test_stagnation_resets_on_progress(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.record_score(5.0)
        mem.record_score(5.1)  # stagnation +1
        mem.record_score(5.5)  # delta >= 0.3 → reset
        assert mem.stagnation_count == 0


class TestStagnationDetection:
    def test_is_stagnating_by_count(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.stagnation_count = 5
        is_stuck, reason = mem.is_stagnating()
        assert is_stuck
        assert "5" in reason

    def test_not_stagnating(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.stagnation_count = 2
        is_stuck, _ = mem.is_stagnating()
        assert not is_stuck

    def test_stagnation_by_variance(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.scores = [6.0, 6.1, 6.0, 6.2, 6.1, 6.0]
        is_stuck, reason = mem.is_stagnating()
        assert is_stuck
        assert "fluctuation" in reason


class TestIssueTracking:
    def test_record_issues(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.record_issues(["M1", "M2", "m1"], iteration=1)
        assert mem.issue_history["M1"] == 1
        assert mem.issue_history["M2"] == 1
        assert mem.last_issues == ["M1", "M2", "m1"]

    def test_repeat_issues(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.record_issues(["M1", "M2"], iteration=1)
        mem.record_issues(["M1", "M2"], iteration=2)
        mem.record_issues(["M1"], iteration=3)
        repeats = mem.get_repeat_issues(threshold=3)
        assert len(repeats) == 1
        assert repeats[0][0] == "M1"

    def test_record_repair_method(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.record_repair_method("M1", "WRITING_ONLY")
        mem.record_repair_method("M1", "FIGURE_CODE_REQUIRED")
        assert mem.get_tried_methods("M1") == ["WRITING_ONLY", "FIGURE_CODE_REQUIRED"]


class TestEscalation:
    def test_strategy_escalation(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.issue_history = {"M1": 5}
        mem.issue_repair_methods = {"M1": ["WRITING_ONLY", "WRITING_ONLY"]}
        escalations = mem.get_strategy_escalation()
        assert "M1" in escalations
        assert escalations["M1"]["count"] == 5


class TestSaveLoadRoundtrip:
    def test_roundtrip(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.record_score(7.5)
        mem.record_issues(["M1", "m1"], iteration=1)
        mem.record_repair_method("M1", "WRITING_ONLY")

        # Reload from file
        mem2 = SimpleMemory(state_dir=tmp_state_dir)
        assert mem2.scores == [7.5]
        assert mem2.best_score == 7.5
        assert mem2.issue_history["M1"] == 1
        assert "WRITING_ONLY" in mem2.issue_repair_methods.get("M1", [])


class TestHealthStatus:
    def test_healthy(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.scores = [5.0, 6.0, 7.0]
        mem.stagnation_count = 0
        status, reasons = mem.get_health_status()
        assert status == "HEALTHY"

    def test_warning_stagnation(self, tmp_state_dir):
        mem = SimpleMemory(state_dir=tmp_state_dir)
        mem.scores = [5.0]
        mem.stagnation_count = 5  # threshold is 5
        status, reasons = mem.get_health_status()
        assert status in ("WARNING", "CRITICAL")
