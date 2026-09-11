"""`ark env lock` must protect an env without reaching into the shared cache.

2026-08-31 the lock ran `chmod -R a-w` over the whole site-packages tree.
Conda hardlinks package files into a package cache shared by every env on the
host, so the mode change followed those links: 811 cache files belonging to
python, pip, setuptools and wheel were left read-only for everyone, found on
2026-09-10 while investigating an unrelated provisioning failure.

Directories are the right target anyway. Creating, replacing or deleting a
file needs write permission on its directory, which is what actually stops
pip, and directories are never shared between envs.
"""

import os
import stat
from argparse import Namespace
from pathlib import Path

import pytest

from ark import cli


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """An env whose file is hardlinked into a 'shared package cache'."""
    sp = tmp_path / "envs" / "ark-base" / "lib" / "python3.11" / "site-packages"
    (sp / "pkg").mkdir(parents=True)
    cache = tmp_path / "pkgs" / "pkg-1.0" / "site-packages" / "pkg"
    cache.mkdir(parents=True)
    cached_file = cache / "__init__.py"
    cached_file.write_text("x = 1\n")
    cached_file.chmod(0o644)
    # How conda materialises a package into an env: a hardlink, one inode.
    env_file = sp / "pkg" / "__init__.py"
    os.link(cached_file, env_file)
    return Namespace(sp=sp, cached_file=cached_file, env_file=env_file)


def _run(env_cmd, sp, monkeypatch):
    # raising=True on purpose: when this helper was written against the
    # wrong name with raising=False, the patch silently did nothing and the
    # test chmod'd the REAL shared ark-base.
    assert "/tmp" in str(sp) or "pytest" in str(sp), f"refusing to run against {sp}"
    monkeypatch.setattr(cli, "_shared_env_site_packages", lambda name: sp)
    return cli.cmd_env(Namespace(env_cmd=env_cmd, env="ark-base"))


class TestLockLeavesTheSharedCacheAlone:
    def test_lock_does_not_change_file_modes(self, fake_env, monkeypatch):
        before = _mode(fake_env.cached_file)
        _run("lock", fake_env.sp, monkeypatch)
        assert _mode(fake_env.cached_file) == before, \
            "lock reached through the hardlink into the shared package cache"
        assert _mode(fake_env.env_file) == before

    def test_lock_makes_directories_read_only(self, fake_env, monkeypatch):
        _run("lock", fake_env.sp, monkeypatch)
        assert not _mode(fake_env.sp) & stat.S_IWUSR, "site-packages stayed writable"
        assert not _mode(fake_env.sp / "pkg") & stat.S_IWUSR, "subdir stayed writable"

    def test_locked_directory_refuses_new_files(self, fake_env, monkeypatch):
        """The point of the lock: pip cannot drop a file in."""
        _run("lock", fake_env.sp, monkeypatch)
        with pytest.raises(PermissionError):
            (fake_env.sp / "pkg" / "evil.py").write_text("nope")

    def test_unlock_restores_writability(self, fake_env, monkeypatch):
        _run("lock", fake_env.sp, monkeypatch)
        _run("unlock", fake_env.sp, monkeypatch)
        (fake_env.sp / "pkg" / "ok.py").write_text("fine")
        assert (fake_env.sp / "pkg" / "ok.py").exists()
