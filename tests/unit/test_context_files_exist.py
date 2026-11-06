"""The Context Files block names files that are actually there.

The list was fixed and hedged with "(if they exist)". Two of its entries never
exist in a paper run: `research_state.yaml` is written only by
`StateManager.load_state()`, which paper mode never calls, and `report.md` is
left over from the old dev mode. The hedge did not save us — measured across
one DSH and one OpenHands run of the same project, chasing those two was the
single most common refused tool call, 8 of 67. A weak model reads the miss as
a dead end rather than a missing optional file.
"""

from pathlib import Path

import pytest

from ark.engines import context_files_section


def _state(root: Path, *names):
    (root / "auto_research" / "state").mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / "auto_research" / "state" / n).write_text("x")


def test_a_file_that_is_not_there_is_not_advertised(tmp_path):
    _state(tmp_path, "findings.yaml", "project_context.md")

    block = context_files_section(tmp_path, deep_research=False)

    assert "auto_research/state/findings.yaml" in block
    assert "auto_research/state/project_context.md" in block
    # Neither of these is ever created in a paper run.
    assert "research_state.yaml" not in block
    assert "report.md" not in block
    # And the hedge is gone, because every line is now true.
    assert "if they exist" not in block


def test_a_file_that_is_there_is_advertised(tmp_path):
    _state(tmp_path, "research_state.yaml")

    assert "auto_research/state/research_state.yaml" in \
        context_files_section(tmp_path, deep_research=False)


def test_the_deep_research_report_is_listed_when_it_exists(tmp_path):
    _state(tmp_path, "deep_research.md", "project_context.md")

    block = context_files_section(tmp_path, deep_research=True)

    assert "auto_research/state/deep_research.md" in block
    assert "do not skim" in block


def test_a_role_that_gets_deep_research_is_not_sent_to_a_missing_report(tmp_path):
    _state(tmp_path, "project_context.md")

    block = context_files_section(tmp_path, deep_research=True)

    assert "deep_research.md" not in block
    assert "auto_research/state/project_context.md" in block


def test_an_empty_project_gets_no_heading_at_all(tmp_path):
    assert context_files_section(tmp_path, deep_research=True) == ""


def test_the_results_directory_counts(tmp_path):
    (tmp_path / "results").mkdir()

    assert "results/ - Experiment results" in \
        context_files_section(tmp_path, deep_research=False)


def test_an_unreadable_project_dir_does_not_raise(tmp_path):
    assert context_files_section(tmp_path / "gone", deep_research=True) == ""


def test_the_writer_is_pointed_at_the_block_its_task_carries_not_a_file_written_later():
    """`figure_manifest.json` does not exist yet when the first draft is written.

    The compile step registers the figures and writes it, and the compile step
    runs after the writer. The prompt still called the file "the authoritative
    source for how a figure should be placed", so the writer opened it and got
    `not found` — 1 of the 12 refused tool calls in the post-fix DSH run. The
    same `placement`/`scalable` data is inlined under `## Available Figures`
    in every writer call, so the prompt names the block instead. Hedging the
    sentence with "if it exists" was not an option: this file's own history
    says a weak model reads the miss as a dead end either way.
    """
    import ark
    root = Path(ark.__file__).parent
    prompt = (root / "templates" / "agents" / "writer.prompt").read_text()

    assert "Available Figures" in prompt
    assert "{FIGURES_DIR}/figure_manifest.json" not in prompt

    # The header the prompt names is the header the writer's task really has.
    assert (root / "execution.py").read_text().count("## Available Figures") >= 2
