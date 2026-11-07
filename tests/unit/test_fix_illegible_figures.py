"""The last actor before delivery, for a figure nobody can read.

dsh8 shipped `fig_main_results` with the value label `1000  (hit max_iter=1000)`
printed across the `10³` y-tick. Every upstream part worked: the overlap was
found, Layer-1 tried and failed, the escalation file was written — addressed to
"the next planner iteration", on a `--iterations 1` run. Nothing read it. The
contract reported the violation and the paper shipped with it.

The gate under test is the one actor after that point. One attempt, then
deliver with an honest line either way.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

fitz = pytest.importorskip("fitz")

from ark.execution import ExecutionMixin


def _figure(path: Path, labels):
    doc = fitz.open()
    page = doc.new_page(width=240, height=160)
    for text, x, y in labels:
        page.insert_text((x, y), text, fontsize=9)
    doc.save(str(path))
    doc.close()


OVERPRINTED = [("Raw", 40, 150), ("Standardized", 44, 150)]
FINE = [("Raw", 40, 150), ("Standardized", 110, 150)]


def _stub(figures, code_dir):
    s = MagicMock(spec=ExecutionMixin)
    s.figures_dir = figures
    s.code_dir = code_dir
    s.config = {}
    s._quota_exhausted = False
    # log_step/log live on the logging mixin, not this one
    s.log_step = MagicMock()
    s.log = MagicMock()
    return s


def _pipeline(tmp_path, labels, script_body="def fig_main():\n    pass\n"):
    figures = tmp_path / "paper" / "figures"
    figures.mkdir(parents=True)
    _figure(figures / "fig_main_results.pdf", labels)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "create_paper_figures.py").write_text(script_body)

    s = _stub(figures, tmp_path)
    s._script_that_draws = ExecutionMixin._script_that_draws.__get__(s)
    return s, figures


def _run(s, context="pre-delivery"):
    ExecutionMixin._fix_illegible_figures.__get__(s)(context=context)


def test_an_unreadable_figure_sends_the_coder_once(tmp_path):
    s, _ = _pipeline(tmp_path, OVERPRINTED)

    _run(s)

    assert s._run_figure_code_tasks.call_count == 1
    tasks = s._run_figure_code_tasks.call_args[0][0]
    assert len(tasks) == 1
    assert tasks[0]["type"] == "FIGURE_CODE_REQUIRED"
    # the coder must be told WHICH labels collide, not merely that some do
    assert "Standardized" in tasks[0]["description"]
    assert "labels overprint" in tasks[0]["description"]
    assert tasks[0]["actions"][0]["target_file"] == "scripts/create_paper_figures.py"


def test_legible_figures_do_not_spend_a_coder_call(tmp_path):
    s, _ = _pipeline(tmp_path, FINE)

    _run(s)

    s._run_figure_code_tasks.assert_not_called()


def test_a_figure_still_unreadable_after_the_attempt_is_not_retried(tmp_path):
    """One attempt. A second one on a model that just failed is worth less
    than the honest line."""
    s, _ = _pipeline(tmp_path, OVERPRINTED)      # the mock changes nothing

    _run(s)

    assert s._run_figure_code_tasks.call_count == 1
    said = " ".join(str(c) for c in s.log_step.call_args_list)
    assert "still unreadable" in said
    assert "fig_main_results.pdf" in said


def test_a_figure_the_coder_fixes_is_reported_as_fixed(tmp_path):
    s, figures = _pipeline(tmp_path, OVERPRINTED)
    # stand in for the coder: the regeneration leaves the labels apart
    s._run_figure_code_tasks.side_effect = (
        lambda *a, **k: _figure(figures / "fig_main_results.pdf", FINE))

    _run(s)

    said = " ".join(str(c) for c in s.log_step.call_args_list)
    assert "now legible" in said
    assert "still unreadable" not in said


def test_the_coder_is_sent_at_the_script_that_draws_the_figure(tmp_path):
    """dsh6 had two plotting scripts; the default one did not draw the figure."""
    s, _ = _pipeline(tmp_path, OVERPRINTED)
    (tmp_path / "scripts" / "plot_main.py").write_text(
        "fig.savefig('paper/figures/fig_main_results.pdf')\n")

    _run(s)

    tasks = s._run_figure_code_tasks.call_args[0][0]
    assert tasks[0]["actions"][0]["target_file"] == "scripts/plot_main.py"


def test_an_exhausted_quota_spends_nothing(tmp_path):
    s, _ = _pipeline(tmp_path, OVERPRINTED)
    s._quota_exhausted = True

    _run(s)

    s._run_figure_code_tasks.assert_not_called()


def test_a_project_with_no_figures_is_quiet(tmp_path):
    figures = tmp_path / "paper" / "figures"
    figures.mkdir(parents=True)
    s = _stub(figures, tmp_path)

    _run(s)

    s._run_figure_code_tasks.assert_not_called()
    s.log_step.assert_not_called()
