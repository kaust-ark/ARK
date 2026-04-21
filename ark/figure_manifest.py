"""Figure manifest: tracks provenance and metadata of generated figures.

Each figure records:
  - source: where it came from (paperbanana, nano_banana, matplotlib, manual)
  - protected: whether it should be preserved across matplotlib re-runs
  - placement: "single_column" or "full_width" — dictates figure vs figure*
  - scalable: whether \\includegraphics[width=...] resizing is safe
               (AI-generated bitmaps: yes; matplotlib vector PDFs with
               baked-in text: no — shrinking there makes labels unreadable)
  - width_in: physical width of the source file in inches (informational)

This manifest is the single source of truth for figure metadata. The writer
agent reads placement/scalable to pick the right LaTeX environment and to
decide whether a figure can be shrunk during page compression. Generators
(matplotlib script, PaperBanana pipeline) write to it. Compression skill
reads scalable to gate lossy resize operations.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

AI_SOURCES = {"paperbanana", "nano_banana"}
MANIFEST_FILE = "figure_manifest.json"

# Heuristic column-vs-full boundary in inches. Double-column venues have
# column width ~3.2-3.5in; single-column venues have textwidth ~6-7in.
# A figure narrower than 4.5in is almost always a single-column figure.
_COLUMN_BOUNDARY_IN = 4.5


def _infer_width_in(figures_dir: Path, filename: str) -> Optional[float]:
    """Read the physical width of a figure file in inches.

    PDFs carry a native point size (72 pt = 1 in); we read that directly.
    Raster files (PNG, JPEG) have only pixel dimensions; we convert at
    matplotlib's default 300 DPI, a reasonable proxy for authoring intent.

    Detection is by magic bytes, not file extension — PaperBanana writes
    JPEG content with a `.png` extension, so a naive `suffix == ".png"`
    branch and hand-rolled PNG parser reads garbage. Pillow handles both
    formats uniformly and follows the real content.

    Returns None if the file can't be read or is in an unrecognized format.
    Fails soft: any exception returns None rather than propagating.
    """
    fpath = figures_dir / filename
    if not fpath.exists():
        return None
    try:
        # Sniff the first few bytes to route to the right reader.
        with open(fpath, "rb") as f:
            head = f.read(8)

        # PDF: "%PDF-" signature → use PyMuPDF for the page MediaBox.
        if head[:5] == b"%PDF-":
            try:
                import fitz  # type: ignore
            except ImportError:
                return None
            doc = fitz.open(str(fpath))
            try:
                if doc.page_count == 0:
                    return None
                return doc[0].rect.width / 72.0
            finally:
                doc.close()

        # Raster (PNG signature, JPEG SOI, or any other image Pillow
        # recognizes). Rely on Pillow to read real pixel dimensions
        # regardless of what the extension claims.
        is_png = head[:8] == b"\x89PNG\r\n\x1a\n"
        is_jpeg = head[:2] == b"\xff\xd8"
        if is_png or is_jpeg or fpath.suffix.lower() in (".png", ".jpg", ".jpeg"):
            try:
                from PIL import Image  # type: ignore
            except ImportError:
                return None
            with Image.open(str(fpath)) as im:
                return im.width / 300.0  # matplotlib savefig default DPI

        return None
    except Exception:
        return None


def _infer_placement(width_in: Optional[float]) -> str:
    """Map physical width to LaTeX placement class.

    < 4.5in → single_column (column width in 2-col venues is ~3.2-3.5in).
    ≥ 4.5in → full_width (spans both columns in 2-col, or the full page in 1-col).

    When width is unknown, return single_column as the safe default — a
    single-column figure placed in figure* just wastes horizontal space,
    but a full-width figure placed in figure gets squeezed to unreadable.
    """
    if width_in is None:
        return "single_column"
    return "full_width" if width_in >= _COLUMN_BOUNDARY_IN else "single_column"


def _infer_scalable(source: str) -> bool:
    """Whether \\includegraphics[width=...] resizing preserves readability.

    AI-generated figures (PaperBanana / Nano-Banana) are rasterized PNGs —
    resizing them via LaTeX is fine, labels scale proportionally.

    matplotlib figures are vector PDFs with fonts baked in at authoring
    time. Resizing via LaTeX shrinks the text glyphs along with the rest,
    often to sub-6pt, and violates ICML/NeurIPS legibility rules. Those
    figures must be regenerated with a different figsize instead.
    """
    return source in AI_SOURCES


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


def register_figure(manifest: dict, filename: str, source: str,
                    figures_dir: Optional[Path] = None,
                    width_in: Optional[float] = None,
                    placement: Optional[str] = None,
                    scalable: Optional[bool] = None,
                    **kwargs) -> None:
    """Register a figure in the manifest.

    source: "paperbanana", "nano_banana", "matplotlib", or "manual"

    Optional metadata — any field the caller does not specify is inferred
    from the source file (requires figures_dir) or from the source type:

        figures_dir:  passed so width_in can be read from the file
        width_in:     physical width in inches
        placement:    "single_column" | "full_width" (from width heuristic)
        scalable:     True for AI bitmaps, False for matplotlib vectors-with-text

    Extra keyword arguments are passed through into the manifest entry,
    preserving backward compatibility with callers that attach ad-hoc fields.
    """
    figures = manifest.setdefault("figures", {})

    # Auto-infer missing fields
    if width_in is None and figures_dir is not None:
        width_in = _infer_width_in(figures_dir, filename)
    if placement is None:
        placement = _infer_placement(width_in)
    if scalable is None:
        scalable = _infer_scalable(source)

    entry = {
        "source": source,
        "protected": source in AI_SOURCES,
        "placement": placement,
        "scalable": scalable,
    }
    if width_in is not None:
        entry["width_in"] = round(width_in, 2)
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

    Rules, applied in order:
      1. Any .pdf with the `fig_` prefix is a matplotlib output — PaperBanana
         writes .png only in the current pipeline, so a PDF settles it.
      2. A .png whose stem has a matching .pdf sibling is the matplotlib PNG
         preview (same figure rendered as raster for web/Slack), NOT an AI
         generation — even if >150KB. Catching this is important: without
         it, every matplotlib figure's PNG counterpart gets misclassified
         as `nano_banana` and inherits the wrong `scalable=True`.
      3. A .png with `fig_` prefix, no .pdf sibling, and >150KB is
         AI-generated (heuristic — will be overwritten once `register_figure`
         is called explicitly for the real source).
      4. Everything else is `matplotlib`.

    `register_figure` is called with `figures_dir` so placement / width_in
    / scalable all auto-populate from the file itself.
    """
    manifest = {"figures": {}}
    if not figures_dir.exists():
        return manifest

    # Pass 1: collect stems that have a matplotlib .pdf — authoritative marker.
    matplotlib_stems = {
        f.stem for f in figures_dir.iterdir()
        if f.suffix == ".pdf" and f.name.startswith("fig_")
    }

    for f in figures_dir.iterdir():
        if f.suffix not in (".png", ".pdf", ".jpg"):
            continue
        if f.name == MANIFEST_FILE:
            continue

        if f.suffix == ".pdf":
            source = "matplotlib"
        elif f.suffix == ".png" and f.stem in matplotlib_stems:
            # PNG companion of a matplotlib PDF → same figure rendered as raster
            source = "matplotlib"
        else:
            is_ai = (
                f.suffix == ".png"
                and f.name.startswith("fig_")
                and f.stat().st_size > 150_000
            )
            source = "nano_banana" if is_ai else "matplotlib"

        register_figure(
            manifest, f.name, source=source,
            figures_dir=figures_dir,
        )
    return manifest
