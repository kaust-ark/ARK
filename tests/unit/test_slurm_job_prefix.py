"""Each project's experiment-wait loop must count only its OWN jobs.

The wait loop polls squeue and counts every job whose name starts with
`job_prefix`. The old default collapsed to a bare "_" when a project had no
title yet — the normal state at experiment time — so one project waited on
every other project's and every prior run's "_"-named jobs. On 2026-09-12
f028f3ba sat in "waiting for all experiments" for hours counting a different
project's leftover _e* jobs. The prefix must be non-empty and project-unique.
"""

from pathlib import Path

from ark.compute.slurm import SlurmBackend


def _backend(project_name, code_dir: Path):
    return SlurmBackend(config={"experiment_compute_backend": {"type": "slurm"}},
                        project_name=project_name, code_dir=code_dir)


def test_prefix_is_never_bare_underscore_when_title_is_empty(tmp_path):
    proj = tmp_path / "f028f3ba-4148-4350-a274-3a1fb6a8fbe9"
    proj.mkdir()
    p = _backend("", proj).job_prefix
    assert p not in ("_", ""), f"prefix collapsed to {p!r} — collides across projects"
    assert p.startswith("F028F3BA")  # derived from the project id


def test_two_untitled_projects_get_distinct_prefixes(tmp_path):
    a = tmp_path / "aaaaaaaa-1111-2222-3333-444444444444"; a.mkdir()
    b = tmp_path / "bbbbbbbb-5555-6666-7777-888888888888"; b.mkdir()
    pa = _backend("", a).job_prefix
    pb = _backend("", b).job_prefix
    assert pa != pb, "untitled projects must not share a job prefix"


def test_a_real_title_is_still_honored(tmp_path):
    proj = tmp_path / "cccccccc-0000-1111-2222-333333333333"; proj.mkdir()
    assert _backend("MyPaper", proj).job_prefix == "MYPAPER_"


def test_explicit_config_prefix_wins(tmp_path):
    proj = tmp_path / "dddddddd-0000-1111-2222-333333333333"; proj.mkdir()
    b = SlurmBackend(
        config={"experiment_compute_backend": {"type": "slurm", "job_prefix": "CUSTOM_"}},
        project_name="", code_dir=proj)
    assert b.job_prefix == "CUSTOM_"
