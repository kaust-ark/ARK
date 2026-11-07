"""Did the coder actually edit the script it was sent to edit?

`FIGURE_CODE_REQUIRED` tasks send the coder at `scripts/create_paper_figures.py`
and the pipeline then checks whether it changed. That check used to ask
`git diff --name-only`, which lists tracked files only — and the pipeline tracks
`auto_research/state/*` and nothing else, so the plotting script is untracked.
The answer was therefore always "not modified":

    [01:10:11] ✎ edit scripts/create_paper_figures.py   (twice, then py_compile,
    [01:10:29] ✗ WARNING: FIGURE_CODE_REQUIRED task but Python not modified!

dsh6 03:00:42 and dsh8 01:10:29, both false, and the accompanying message sends
whoever reads it after the wrong cause.
"""
import subprocess
from pathlib import Path

from ark.execution import _digests

SCRIPT = "scripts/create_paper_figures.py"


def _repo(tmp_path: Path) -> Path:
    """A project laid out the way the pipeline leaves one: the state files
    tracked, the agent's own code not."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "auto_research" / "state").mkdir(parents=True)
    (tmp_path / "auto_research" / "state" / "idea.md").write_text("idea\n")
    subprocess.run(["git", "add", "auto_research"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "state"], cwd=tmp_path, check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / SCRIPT).write_text("ax.set_ylim(0, 60)\n")
    return tmp_path


def test_an_edit_to_the_untracked_plotting_script_is_seen(tmp_path):
    root = _repo(tmp_path)

    before = _digests(root, {SCRIPT})
    (root / SCRIPT).write_text("ax.set_ylim(0, 100)\n")
    after = _digests(root, {SCRIPT})

    assert before[SCRIPT] != after[SCRIPT], "the edit was not detected"

    # The instrument this replaced, on the same edit, in the same repo:
    out = subprocess.run(["git", "diff", "--name-only"], cwd=root,
                         capture_output=True, text=True).stdout.splitlines()
    assert SCRIPT not in out, (
        "git now lists the script; if the pipeline started tracking scripts/, "
        "this test's premise is stale — but the content check is still correct")


def test_a_script_left_alone_is_reported_unchanged(tmp_path):
    """The positive control: the warning must still fire when it is true."""
    root = _repo(tmp_path)

    before = _digests(root, {SCRIPT})
    after = _digests(root, {SCRIPT})

    assert before[SCRIPT] == after[SCRIPT]


def test_rewriting_a_file_with_identical_bytes_is_not_a_change(tmp_path):
    """Content, not mtime — a rewrite that changes nothing changed nothing."""
    root = _repo(tmp_path)
    before = _digests(root, {SCRIPT})

    (root / SCRIPT).write_text("ax.set_ylim(0, 60)\n")   # same bytes

    assert _digests(root, {SCRIPT})[SCRIPT] == before[SCRIPT]


def test_a_target_file_that_does_not_exist_is_not_a_crash(tmp_path):
    """The planner can name a file the project never had."""
    root = _repo(tmp_path)

    d = _digests(root, {"scripts/nope.py"})

    assert d == {"scripts/nope.py": None}


def test_a_file_the_coder_creates_counts_as_a_change(tmp_path):
    """dsh6's coder answered a figure task by writing a new script."""
    root = _repo(tmp_path)
    target = "scripts/plot_n_iter_bar.py"

    before = _digests(root, {target})
    (root / target).write_text("import matplotlib\n")
    after = _digests(root, {target})

    assert before[target] is None and after[target] is not None
    assert before[target] != after[target]
