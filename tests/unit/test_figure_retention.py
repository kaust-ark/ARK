"""A writing pass must not silently delete figures.

The writing phase's own accounting reads only the ADDED column of `git diff
--numstat`, so a pass that removes a figure and writes two paragraphs logs
"added 15 lines" and looks healthy. Observed live on smoke run 36763067: the
writer dropped an \\includegraphics for the concept figure, the PDF fell from
1.08 MB to 349 KB, LaTeX compiled clean, the page count still fit, and the
delivery contract reported "all checks passed".
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ark.execution import ExecutionMixin


@pytest.fixture
def ex(tmp_path):
    o = ExecutionMixin.__new__(ExecutionMixin)
    o.code_dir = tmp_path
    o.logs = []
    o.log = lambda msg, level="INFO": o.logs.append((level, msg))
    return o


def _diff(body):
    return patch("subprocess.run",
                 return_value=SimpleNamespace(stdout=body, stderr="", returncode=0))


def test_a_removed_figure_is_named(ex):
    with _diff("--- a/paper/main.tex\n+++ b/paper/main.tex\n"
               "-\\includegraphics[width=\\columnwidth]{figures/fig_concept.png}\n"
               "+Some replacement prose about the architecture.\n"):
        dropped = ex._report_dropped_figures("paper")
    assert dropped == ["figures/fig_concept.png"]
    assert any(lvl == "WARN" and "fig_concept.png" in m for lvl, m in ex.logs)


def test_moving_a_figure_is_not_reported(ex):
    """Relocation is a delete plus an insert; the figure never left."""
    with _diff("--- a/paper/main.tex\n+++ b/paper/main.tex\n"
               "-\\includegraphics[width=\\columnwidth]{figures/fig_main.pdf}\n"
               "+\\includegraphics[width=\\textwidth]{figures/fig_main.pdf}\n"):
        assert ex._report_dropped_figures("paper") == []
    assert not any(lvl == "WARN" for lvl, _ in ex.logs)


def test_pure_prose_edits_are_silent(ex):
    with _diff("--- a/paper/main.tex\n+++ b/paper/main.tex\n"
               "-We show that the method works.\n"
               "+We show that the method works well in practice.\n"):
        assert ex._report_dropped_figures("paper") == []


def test_several_removals_are_all_named(ex):
    with _diff("--- a/paper/main.tex\n+++ b/paper/main.tex\n"
               "-\\includegraphics{figures/a.pdf}\n"
               "-\\includegraphics[width=2in]{figures/b.png}\n"):
        dropped = ex._report_dropped_figures("paper")
    assert dropped == ["figures/a.pdf", "figures/b.png"]


def test_the_diff_header_is_not_mistaken_for_a_deletion(ex):
    """`--- a/…` and `+++ b/…` start with the same characters as content."""
    with _diff("--- a/paper/\\includegraphics{x}\n+++ b/paper/main.tex\n"):
        assert ex._report_dropped_figures("paper") == []


def test_a_git_failure_never_breaks_the_writing_phase(ex):
    with patch("subprocess.run", side_effect=OSError("git not found")):
        assert ex._report_dropped_figures("paper") == []
    assert any(lvl == "WARN" and "skipped" in m for lvl, m in ex.logs)
