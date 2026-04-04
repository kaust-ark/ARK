"""Figure manifest: tracks provenance of generated figures.

Each figure records its source (paperbanana, nano_banana, matplotlib, manual)
so the system can protect AI-generated concept figures from being overwritten
when matplotlib scripts are re-run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

AI_SOURCES = {"paperbanana", "nano_banana"}
MANIFEST_FILE = "figure_manifest.json"


def load_manifest(figures_dir: Path) -> dict:
    """Load manifest from figures_dir, or auto-generate from existing files."""
    manifest_path = figures_dir / MANIFEST_FILE
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # Auto-migrate: build manifest from existing files
    return _build_manifest_from_existing(figures_dir)


def save_manifest(figures_dir: Path, manifest: dict) -> None:
    """Write manifest to disk."""
    manifest_path = figures_dir / MANIFEST_FILE
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def register_figure(manifest: dict, filename: str, source: str, **kwargs) -> None:
    """Register a figure in the manifest.

    source: "paperbanana", "nano_banana", "matplotlib", or "manual"
    """
    figures = manifest.setdefault("figures", {})
    entry = {
        "source": source,
        "protected": source in AI_SOURCES,
    }
    entry.update(kwargs)
    figures[filename] = entry


def get_protected_files(manifest: dict) -> set[str]:
    """Return set of filenames that are protected (AI-generated)."""
    return {
        fname for fname, info in manifest.get("figures", {}).items()
        if info.get("protected")
    }


def backup_protected(figures_dir: Path, manifest: dict) -> dict[str, bytes]:
    """Read all protected files into memory for later restoration."""
    backups = {}
    for fname in get_protected_files(manifest):
        fpath = figures_dir / fname
        if fpath.exists():
            backups[fname] = fpath.read_bytes()
    return backups


def restore_protected(figures_dir: Path, backups: dict[str, bytes],
                      log_fn=None) -> None:
    """Restore any protected files that were overwritten."""
    for fname, original_data in backups.items():
        fpath = figures_dir / fname
        if not fpath.exists() or fpath.read_bytes() != original_data:
            fpath.write_bytes(original_data)
            if log_fn:
                log_fn(f"Restored protected figure: {fname}", "INFO")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_manifest_from_existing(figures_dir: Path) -> dict:
    """Heuristic migration for projects without a manifest.

    PNGs with 'fig_' prefix and size >150KB are likely AI-generated.
    Everything else is assumed to be matplotlib.
    """
    manifest = {"figures": {}}
    if not figures_dir.exists():
        return manifest

    for f in figures_dir.iterdir():
        if f.suffix not in (".png", ".pdf", ".jpg"):
            continue
        if f.name == MANIFEST_FILE:
            continue
        # Heuristic: large PNGs with fig_ prefix are likely AI-generated
        is_ai = (
            f.suffix == ".png"
            and f.name.startswith("fig_")
            and f.stat().st_size > 150_000
        )
        register_figure(
            manifest, f.name,
            source="nano_banana" if is_ai else "matplotlib",
        )
    return manifest
