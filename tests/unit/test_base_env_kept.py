"""A launch never creates the shared base env over itself.

2026-10-26: a project with no env of its own (they are removed once a run
ends, so every Continue takes this path) falls back to ark-base, after asking
`conda env list` whether ark-base exists. The listing timed out, the timeout
read as "missing", and `conda env create --yes` deleted ark-base and built it
again from environment.yml, which pinned seven platform packages to older
versions. The same path had done it once before, from the dev webapp on
2026-08-16.
"""
import subprocess
import sys
from unittest import mock

import pytest

from website.dashboard import jobs

_REAL_RUN = subprocess.run


@pytest.fixture
def conda(tmp_path, monkeypatch):
    """A stand-in conda under <root>/condabin/conda that logs each call.
    `env list` answers per FAKE_CONDA_LIST; `env create` makes the env dir."""
    root = tmp_path / "conda"
    (root / "condabin").mkdir(parents=True)
    exe = root / "condabin" / "conda"
    exe.write_text(f"""#!{sys.executable}
import json, os, sys
open(os.environ["FAKE_CONDA_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["env", "list"]:
    mode = os.environ["FAKE_CONDA_LIST"]
    if mode == "fails":
        sys.exit(1)
    if mode == "garbage":
        print("not json")
        sys.exit(0)
    print(json.dumps({{"envs": []}}))
if args[:2] == ["env", "create"]:
    name = args[args.index("-n") + 1]
    root = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))
    os.makedirs(os.path.join(root, "envs", name, "conda-meta"))
""")
    exe.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    monkeypatch.setenv("FAKE_CONDA_LOG", str(log))
    monkeypatch.setenv("FAKE_CONDA_LIST", "missing")
    return root, exe, log


def _creates(log):
    return [c for c in log.read_text().splitlines() if c.startswith("env create")]


def test_an_existing_env_is_kept_when_the_listing_fails(conda, tmp_path, monkeypatch):
    root, exe, log = conda
    (root / "envs" / "ark-base" / "conda-meta").mkdir(parents=True)
    monkeypatch.setenv("FAKE_CONDA_LIST", "fails")
    ok, msg = jobs._ensure_named_conda_env(str(exe), "ark-base", tmp_path / "p.log")
    assert ok and "already exists" in msg
    assert _creates(log) == []


@pytest.mark.parametrize("how", ["timeout", "fails", "garbage"])
def test_a_listing_that_cannot_answer_creates_nothing(conda, tmp_path, monkeypatch, how):
    """The env may live outside <root>/envs, so the listing is still asked.
    When it cannot answer, the env is used as it is."""
    root, exe, log = conda

    def run(cmd, *a, **kw):
        if how == "timeout" and cmd[1:3] == ["env", "list"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return _REAL_RUN(cmd, *a, **kw)
    monkeypatch.setenv("FAKE_CONDA_LIST", how if how != "timeout" else "missing")
    with mock.patch.object(jobs.subprocess, "run", run):
        ok, msg = jobs._ensure_named_conda_env(str(exe), "ark-base", tmp_path / "p.log")
    assert ok and "as is" in msg
    assert _creates(log) == []


def test_a_missing_env_is_created_without_yes(conda, tmp_path):
    """--yes is what let conda remove an env that was there."""
    root, exe, log = conda
    ok, msg = jobs._ensure_named_conda_env(str(exe), "ark-base", tmp_path / "p.log")
    assert ok and "created env 'ark-base'" in msg, msg
    (create,) = _creates(log)
    assert "--yes" not in create.split() and "-y" not in create.split()
