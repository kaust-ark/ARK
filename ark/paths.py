"""Centralised path helpers for ARK.

Every runtime artefact (.ark/ config, webapp DB, logs, projects) lives
under the *ARK root* — the repository / install directory — so nothing
is scattered across the home directory.
"""

from __future__ import annotations

from pathlib import Path

# ARK root: parent of the `ark/` package directory.
_ARK_ROOT: Path | None = None


def get_ark_root() -> Path:
    """Return the ARK installation root (where pyproject.toml lives)."""
    global _ARK_ROOT
    if _ARK_ROOT is not None:
        return _ARK_ROOT

    pkg_root = Path(__file__).parent.parent.absolute()
    if (pkg_root / "pyproject.toml").exists() or (pkg_root / "projects").exists():
        _ARK_ROOT = pkg_root
    else:
        # Fallback: current working directory
        _ARK_ROOT = Path.cwd()
    return _ARK_ROOT


def get_config_dir() -> Path:
    """Return the .ark/ config directory under the ARK root."""
    d = get_ark_root() / ".ark"
    d.mkdir(exist_ok=True)
    return d
