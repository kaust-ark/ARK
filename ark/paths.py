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


def get_primary_ip() -> str:
    """Return the IP of the interface that routes outbound traffic.

    Uses socket.connect() on a UDP socket — no packet is sent, but the kernel
    resolves the source IP for the route to 8.8.8.8. More reliable than
    socket.gethostname() on hosts where the hostname resolves to 127.0.1.1.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return socket.gethostname()
    finally:
        s.close()
