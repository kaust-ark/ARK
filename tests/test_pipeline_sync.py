"""Tests for cost_report merge-on-write and title propagation.

Covers two fixes:
1. ``_write_cost_report`` now merges with on-disk raw_stats so orchestrator
   restarts don't clobber prior agent costs.
2. ``_sync_paper_metadata`` rewrites ``\\title{...}`` in main.tex and refreshes
   agent prompts after ``_update_title_from_idea`` picks a new title.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from ark.pipeline import _replace_latex_title


# ---------------------------------------------------------------------------
#  _replace_latex_title (pure function)
# ---------------------------------------------------------------------------

class TestReplaceLatexTitle:
    def test_simple_replacement(self):
        src = r"\title{Old Title}"
        assert _replace_latex_title(src, "New Title") == r"\title{New Title}"

    def test_neurips_template_realistic(self):
        # Exact situation from project d9b7fab8: NeurIPS template with its
        # own hardcoded title.
        src = (
            "% ... comments ...\n"
            r"\title{Formatting Instructions For NeurIPS 2026}" "\n"
            "\n"
            r"\author{...}" "\n"
        )
        out = _replace_latex_title(src, "Compute-Aware Deployment")
        assert r"\title{Compute-Aware Deployment}" in out
        assert "Formatting Instructions" not in out

    def test_skips_commented_title(self):
        # First \title is in a comment; the active one is second.
        src = (
            "% example: \\title{WORKSHOP TITLE}\n"
            r"\title{Real Title}" "\n"
        )
        out = _replace_latex_title(src, "New")
        assert r"\title{New}" in out
        assert "WORKSHOP TITLE" in out  # comment untouched

    def test_handles_nested_braces(self):
        src = r"\title{A \emph{brief} note on things}"
        out = _replace_latex_title(src, "Plain title")
        assert out == r"\title{Plain title}"

    def test_handles_leading_whitespace(self):
        src = "    \\title{Old}\n"
        out = _replace_latex_title(src, "New")
        assert out == "    \\title{New}\n"

    def test_returns_unchanged_if_no_title(self):
        src = r"\documentclass{article}" "\n" r"\begin{document}"
        assert _replace_latex_title(src, "Anything") == src

    def test_does_not_match_titleformat(self):
        # \titleformat is a different command; shouldn't match the \title regex
        # because \title must be followed immediately by { (after whitespace),
        # and \titleformat has "format" before {.
        src = r"\titleformat{\section}{\large}{}{0em}{}" "\n" r"\title{Real}"
        out = _replace_latex_title(src, "New")
        assert r"\title{New}" in out
        assert r"\titleformat{\section}" in out

    def test_preserves_surrounding_content(self):
        src = (
            r"\documentclass{article}" "\n"
            r"\usepackage{amsmath}" "\n"
            r"\title{Placeholder}" "\n"
            r"\author{Anonymous}" "\n"
            r"\begin{document}" "\n"
        )
        out = _replace_latex_title(src, "Final")
        assert out.count("\n") == src.count("\n")
        assert r"\usepackage{amsmath}" in out
        assert r"\author{Anonymous}" in out


# ---------------------------------------------------------------------------
#  _write_cost_report merge-on-write
# ---------------------------------------------------------------------------

def _make_stat(agent_type: str, timestamp: str, cost: float = 0.1,
               input_tokens: int = 10, output_tokens: int = 5) -> dict:
    return {
        "agent_type": agent_type,
        "timestamp": timestamp,
        "elapsed_seconds": 1,
        "prompt_len": 100,
        "output_len": 50,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": cost,
        "model": "test-model",
    }


@pytest.fixture
def pipeline_stub(tmp_path):
    """Minimal stub exposing the attributes _write_cost_report needs."""
    from ark.pipeline import PipelineMixin

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    stub = MagicMock(spec=PipelineMixin)
    stub.state_dir = state_dir
    stub._agent_stats = []
    stub._sync_db = MagicMock()
    # Bind the real method so it uses our stub's attributes
    stub._write_cost_report = PipelineMixin._write_cost_report.__get__(stub)
    return stub


class TestCostReportMerge:
    def test_first_write_creates_file(self, pipeline_stub):
        pipeline_stub._agent_stats = [_make_stat("planner", "2026-04-22T08:54:36")]
        pipeline_stub._write_cost_report()
        report = yaml.safe_load(
            (pipeline_stub.state_dir / "cost_report.yaml").read_text()
        )
        assert report["total_agent_calls"] == 1
        assert "planner" in report["per_agent"]

    def test_restart_preserves_prior_costs(self, pipeline_stub):
        """Simulate restart: run 1 records planner+writer, run 2 starts fresh
        and only runs planner. The persisted report must reflect ALL THREE
        calls, not just the second run."""
        # --- Run 1: planner + writer (from an earlier orchestrator process) ---
        pipeline_stub._agent_stats = [
            _make_stat("planner", "2026-04-22T08:00:00", cost=0.3),
            _make_stat("writer", "2026-04-22T08:10:00", cost=0.2),
        ]
        pipeline_stub._write_cost_report()

        # --- Run 2: fresh orchestrator, only planner runs ---
        pipeline_stub._agent_stats = [
            _make_stat("planner", "2026-04-22T09:00:00", cost=0.4),
        ]
        pipeline_stub._write_cost_report()

        report = yaml.safe_load(
            (pipeline_stub.state_dir / "cost_report.yaml").read_text()
        )
        # All three calls persisted
        assert report["total_agent_calls"] == 3
        assert report["total_cost_usd"] == pytest.approx(0.9, rel=1e-6)
        # Both agent types aggregated
        assert report["per_agent"]["planner"]["calls"] == 2
        assert report["per_agent"]["writer"]["calls"] == 1
        # raw_stats sorted by timestamp
        ts = [s["timestamp"] for s in report["raw_stats"]]
        assert ts == sorted(ts)

    def test_dedup_by_timestamp_and_agent(self, pipeline_stub):
        """If the same (timestamp, agent_type) appears on disk and in-memory,
        the in-memory version wins — no double counting."""
        # Seed disk with one stat
        seed = _make_stat("planner", "2026-04-22T08:00:00", cost=0.1)
        pipeline_stub._agent_stats = [seed]
        pipeline_stub._write_cost_report()

        # In-memory has the SAME (timestamp, agent_type) — e.g. restart
        # replayed the same stat. Merge must dedup, not double.
        pipeline_stub._agent_stats = [
            _make_stat("planner", "2026-04-22T08:00:00", cost=0.1),
        ]
        pipeline_stub._write_cost_report()

        report = yaml.safe_load(
            (pipeline_stub.state_dir / "cost_report.yaml").read_text()
        )
        assert report["total_agent_calls"] == 1
        assert report["total_cost_usd"] == pytest.approx(0.1, rel=1e-6)

    def test_empty_inmemory_leaves_disk_intact(self, pipeline_stub):
        """If _agent_stats is empty (orchestrator just started, no calls yet),
        don't touch the existing cost_report.yaml."""
        pipeline_stub._agent_stats = [_make_stat("planner", "2026-04-22T08:00:00")]
        pipeline_stub._write_cost_report()
        disk_before = (pipeline_stub.state_dir / "cost_report.yaml").read_text()

        pipeline_stub._agent_stats = []
        pipeline_stub._write_cost_report()
        disk_after = (pipeline_stub.state_dir / "cost_report.yaml").read_text()
        assert disk_before == disk_after

    def test_corrupt_disk_file_does_not_block_write(self, pipeline_stub):
        """A malformed cost_report.yaml on disk should not prevent writing
        the new in-memory stats (existing data is just dropped)."""
        (pipeline_stub.state_dir / "cost_report.yaml").write_text(
            "this: is: not: valid: yaml: [[[\n"
        )
        pipeline_stub._agent_stats = [_make_stat("planner", "2026-04-22T08:00:00")]
        pipeline_stub._write_cost_report()
        report = yaml.safe_load(
            (pipeline_stub.state_dir / "cost_report.yaml").read_text()
        )
        assert report["total_agent_calls"] == 1


# ---------------------------------------------------------------------------
#  _sync_paper_metadata
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_stub(tmp_path):
    """Stub exposing the attributes _sync_paper_metadata needs."""
    from ark.pipeline import PipelineMixin

    code_dir = tmp_path / "project"
    latex_dir = code_dir / "paper"
    agents_dir = code_dir / "agents"
    latex_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)

    stub = MagicMock(spec=PipelineMixin)
    stub.code_dir = code_dir
    stub.latex_dir = latex_dir
    stub.agents_dir = agents_dir
    stub.project_name = "test-project"
    stub._project_id = "test-project-id"
    stub.config = {
        "venue": "NeurIPS",
        "venue_format": "neurips_2026",
        "venue_pages": 9,
        "latex_dir": "paper",
        "figures_dir": "paper/figures",
    }
    stub.log = MagicMock()
    stub._sync_paper_metadata = PipelineMixin._sync_paper_metadata.__get__(stub)
    return stub


class TestSyncPaperMetadata:
    def test_rewrites_main_tex_title(self, sync_stub):
        main_tex = sync_stub.latex_dir / "main.tex"
        main_tex.write_text(
            "\\documentclass{article}\n"
            "\\title{Formatting Instructions For NeurIPS 2026}\n"
            "\\begin{document}\n"
        )
        sync_stub._sync_paper_metadata("Compute-Aware World Models")
        out = main_tex.read_text()
        assert "\\title{Compute-Aware World Models}" in out
        assert "Formatting Instructions" not in out

    def test_refreshes_agent_prompts(self, sync_stub):
        # First we need the real template files on disk — they live in the
        # ark package, so the sync helper can read them.
        from ark import pipeline
        templates_dir = Path(pipeline.__file__).parent / "templates" / "agents"
        assert templates_dir.exists(), "expected packaged agent templates"

        # Pre-seed agents_dir with stale content that references an old title.
        (sync_stub.agents_dir / "writer.prompt").write_text(
            "Paper title: OLD-UUID\n"
        )

        sync_stub._sync_paper_metadata("My Real Title")
        refreshed = (sync_stub.agents_dir / "writer.prompt").read_text()
        assert "My Real Title" in refreshed
        # Placeholders resolved
        assert "{PAPER_TITLE}" not in refreshed
        assert "{VENUE_NAME}" not in refreshed
        # Stale content replaced
        assert "OLD-UUID" not in refreshed

    def test_noop_when_main_tex_missing(self, sync_stub):
        # No main.tex — should not raise.
        sync_stub._sync_paper_metadata("Any")
        # Agent prompts should still refresh
        assert (sync_stub.agents_dir / "writer.prompt").exists()

    def test_preserves_nonreplaced_tex_lines(self, sync_stub):
        main_tex = sync_stub.latex_dir / "main.tex"
        original = (
            "\\documentclass{article}\n"
            "\\usepackage{amsmath}\n"
            "\\title{Old}\n"
            "\\author{Anon}\n"
            "\\begin{document}\n"
            "Content that must survive intact.\n"
            "\\end{document}\n"
        )
        main_tex.write_text(original)
        sync_stub._sync_paper_metadata("New")
        out = main_tex.read_text()
        for line in ("\\usepackage{amsmath}", "\\author{Anon}",
                     "Content that must survive intact."):
            assert line in out


# ---------------------------------------------------------------------------
#  End-to-end: _update_title_from_idea triggers main.tex sync
# ---------------------------------------------------------------------------

class TestUpdateTitleEndToEnd:
    def test_generated_title_propagates_to_main_tex(self, sync_stub, tmp_path):
        """Reproduces the d9b7fab8 scenario: idea.md exists, config.yaml has
        no title, NeurIPS template ships with its own \\title{} placeholder.
        After _update_title_from_idea, main.tex must carry the LLM-generated
        title, not the template's hardcoded one."""
        from ark.pipeline import PipelineMixin

        # -- Attach state_dir + idea.md + config.yaml --
        state_dir = sync_stub.code_dir / "auto_research" / "state"
        state_dir.mkdir(parents=True)
        sync_stub.state_dir = state_dir
        (state_dir / "idea.md").write_text(
            "Position paper on compute-aware deployment of world models.\n"
        )
        config_path = sync_stub.code_dir / "config.yaml"
        config_path.write_text(yaml.dump({"title": "", "venue_format": "neurips_2026"}))
        sync_stub.config = yaml.safe_load(config_path.read_text())

        # -- Seed main.tex with a template placeholder title --
        main_tex = sync_stub.latex_dir / "main.tex"
        main_tex.write_text(
            "\\documentclass{article}\n"
            "\\title{Formatting Instructions For NeurIPS 2026}\n"
            "\\begin{document}\n"
        )

        # -- Bind the real _update_title_from_idea to the stub --
        sync_stub._update_title_from_idea = (
            PipelineMixin._update_title_from_idea.__get__(sync_stub)
        )
        sync_stub._sync_db = MagicMock()

        generated = "Compute-Aware Deployment of World Models"
        with patch("ark.pipeline._generate_title_via_llm", return_value=generated):
            sync_stub._update_title_from_idea()

        # 1. config.yaml carries the new title
        cfg = yaml.safe_load(config_path.read_text())
        assert cfg["title"] == generated
        # 2. main.tex now references the generated title, not the placeholder
        out = main_tex.read_text()
        assert f"\\title{{{generated}}}" in out
        assert "Formatting Instructions" not in out
        # 3. Agent prompts were refreshed with the new title
        writer_prompt = sync_stub.agents_dir / "writer.prompt"
        assert writer_prompt.exists()
        assert generated in writer_prompt.read_text()

    def test_committed_title_still_syncs_drifted_main_tex(self, sync_stub, tmp_path):
        """Restart-after-preprocess scenario: config.yaml already carries the
        real title but main.tex was stubbed to "ARK Pending Title" by
        template_preprocess. _update_title_from_idea must still push the
        committed title into main.tex instead of short-circuiting.
        """
        from ark.pipeline import PipelineMixin

        state_dir = sync_stub.code_dir / "auto_research" / "state"
        state_dir.mkdir(parents=True)
        sync_stub.state_dir = state_dir
        (state_dir / "idea.md").write_text("Some idea text.\n")

        committed = "Compute-Aware Deployment of World Models"
        sync_stub.config = {
            **sync_stub.config,
            "title": committed,
        }

        # main.tex drifted from config — this is what preprocess leaves behind.
        main_tex = sync_stub.latex_dir / "main.tex"
        main_tex.write_text(
            "\\documentclass{article}\n"
            "\\title{ARK Pending Title}\n"
            "\\begin{document}\n"
        )

        sync_stub._update_title_from_idea = (
            PipelineMixin._update_title_from_idea.__get__(sync_stub)
        )
        sync_stub._sync_db = MagicMock()

        # LLM must NOT be called — we already have a title, just need to sync.
        with patch("ark.pipeline._generate_title_via_llm") as llm:
            sync_stub._update_title_from_idea()
            llm.assert_not_called()

        out = main_tex.read_text()
        assert f"\\title{{{committed}}}" in out
        assert "ARK Pending Title" not in out
