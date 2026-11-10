"""A figure check that could not run must not report a clean figure.

dsh6 ended one minute before delivery on

    [03:09:43] Checking figures for text overlaps...
    [03:09:44] No overlaps detected in 0 figures

and that read as reassurance. Nothing had been examined: the detector
re-executes the plotting script in a subprocess, the script resolves its data
paths against the project root, and the detector ran it from `scripts/`, so it
died on `FileNotFoundError` before the first `savefig`. No report file was
written, and the fallback return carried no `error`, so zero figures checked was
indistinguishable in the log from zero figures broken.

These tests pin the distinction. They do not pin the working directory — that is
recorded separately, because fixing it switches on an auto-fixer whose single
observed run doubled the number of broken figures.
"""
import json
from pathlib import Path

import pytest

from ark.figure_overlap import detect_overlaps_from_script

CONFIG = {"columnwidth_in": 3.33, "font_size_pt": 9}


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_a_script_that_dies_before_saving_is_an_error_not_a_clean_figure(tmp_path):
    """The dsh6 shape: the script cannot find its data and never plots."""
    script = _script(tmp_path / "scripts" / "plot.py",
                     "open('results/exp1/comparison_table.json')\n")
    figures = tmp_path / "paper" / "figures"
    figures.mkdir(parents=True)

    report = detect_overlaps_from_script(script, figures, CONFIG)

    assert "error" in report, "a detector that checked nothing reported no problems"
    assert "FileNotFoundError" in report["error"], report["error"]
    assert report["summary"] == {"total": 0, "with_overlaps": 0}


def test_a_report_from_an_earlier_attempt_is_not_this_run_s_result(tmp_path):
    """`check_and_fix_figures` calls this up to four times on one figures_dir.

    Attempt 1 leaves `overlap_report.json` behind. If attempt 2's script dies,
    reading that file would credit attempt 1's findings to attempt 2 — and with
    `with_overlaps` from a round whose fixes were never applied.
    """
    script = _script(tmp_path / "scripts" / "plot.py", "raise SystemExit(3)\n")
    figures = tmp_path / "paper" / "figures"
    figures.mkdir(parents=True)
    stale = {"figures": [{"name": "fig_main", "has_overlaps": True}],
             "summary": {"total": 1, "with_overlaps": 1}}
    (figures / "overlap_report.json").write_text(json.dumps(stale))

    report = detect_overlaps_from_script(script, figures, CONFIG)

    assert "error" in report
    assert report["summary"] == {"total": 0, "with_overlaps": 0}
    assert report["figures"] == []


def test_an_unreadable_report_is_an_error_not_a_clean_figure(tmp_path):
    """A torn report is a failed check, which is not the same as a pass.

    The wrapper writes the report after the plotting script returns, so the
    only way to leave a half-written one is to die between the two. `os._exit`
    reproduces that: it skips the wrapper's own write and leaves whatever is on
    disk, exactly as a kill would.
    """
    figures = tmp_path / "paper" / "figures"
    figures.mkdir(parents=True)
    script = _script(
        tmp_path / "scripts" / "plot.py",
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(figures / 'overlap_report.json')!r})"
        ".write_text('{\"figures\": [')\n"
        "os._exit(0)\n")

    report = detect_overlaps_from_script(script, figures, CONFIG)

    assert "Unreadable overlap report" in report.get("error", "")
    assert report["summary"] == {"total": 0, "with_overlaps": 0}


def test_a_script_that_does_plot_is_reported_without_an_error(tmp_path):
    """The positive control: without it the tests above pass on a detector
    that errors unconditionally."""
    pytest.importorskip("matplotlib")
    figures = tmp_path / "paper" / "figures"
    figures.mkdir(parents=True)
    out = figures / "fig_main_results.pdf"
    script = _script(
        tmp_path / "scripts" / "plot.py",
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots(figsize=(3.33, 2.2))\n"
        "ax.bar(['a', 'b'], [1, 2])\n"
        f"fig.savefig({str(out)!r})\n")

    report = detect_overlaps_from_script(script, figures, CONFIG)

    assert "error" not in report, report.get("error")
    assert report["summary"]["total"] == 1


def test_a_rerun_that_did_not_report_is_not_resolved_overlaps(tmp_path, monkeypatch):
    """The in-loop re-run never checked `error`, so a broken re-run read clean.

    The first detection checks `report["error"]` and bails. The re-run after
    `apply_auto_fixes` did not: an error report carries no `summary`, so
    `with_overlaps` defaulted to 0 and the very next line logged "All overlaps
    resolved after auto-fix on attempt 1" and returned
    `escalation_needed=False`. `apply_auto_fixes` is a regex rewriter this
    branch documents as having turned one broken figure into two, so a script
    it breaks is the realistic way in — the gate reports success precisely when
    its own fixer destroyed the thing it was measuring.
    """
    from ark import figure_overlap as fo

    script = tmp_path / "plot.py"
    script.write_text("# plotting script\n")
    figures = tmp_path / "figures"
    figures.mkdir()

    calls = []

    def fake_detect(*a, **k):
        calls.append(1)
        if len(calls) == 1:                      # first pass: a real overlap
            return {"summary": {"total": 1, "with_overlaps": 1},
                    "figures": [{"name": "f.pdf", "has_overlaps": True,
                                 "overlap_count": 1, "density": 0.3,
                                 "suggestions": ["widen"]}]}
        return {"error": "script failed: SyntaxError"}   # the fixer broke it

    monkeypatch.setattr(fo, "detect_overlaps_from_script", fake_detect)
    monkeypatch.setattr(fo, "apply_auto_fixes", lambda *a, **k: True)

    said = []
    report = fo.check_and_fix_figures(script, figures, log_fn=lambda m, l="INFO": said.append(m))

    joined = " ".join(said)
    assert "All overlaps resolved" not in joined
    assert "UNKNOWN, not resolved" in joined
    assert report["escalation_needed"] is True
