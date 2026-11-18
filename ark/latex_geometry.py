"""LaTeX template geometry extraction and matplotlib configuration.

Provides venue-specific page dimensions so that matplotlib figures
are generated at the exact size expected by the LaTeX template,
eliminating scaling artifacts (wrong font size, overflow, etc.).
"""

import json
from pathlib import Path

# Venue presets: measured from actual template .cls files
# TeX lengths measured via \the\columnwidth and \the\textwidth (72.27 TeX pt/in).
VENUE_PRESETS = {
    # ACM sigplan (acmart.cls format #6): 2-column, 10pt
    "acmart-sigplan": {
        "columnwidth_in": 3.333,   # 240pt
        "textwidth_in": 7.0,       # 504pt
        "columnsep_in": 0.333,     # 24pt
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 2,
    },
    # ACM small (acmart.cls format #1): 1-column, 10pt
    "acmart-small": {
        "columnwidth_in": 5.5,
        "textwidth_in": 5.5,
        "columnsep_in": 0,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 1,
    },
    # ACM large (acmart.cls format #3): 2-column, 9pt
    "acmart-large": {
        "columnwidth_in": 3.333,
        "textwidth_in": 7.0,
        "columnsep_in": 0.333,
        "font_size_pt": 9,
        "font_family": "serif",
        "columns": 2,
    },
    # IEEE conference: 2-column, 10pt
    "ieee": {
        "columnwidth_in": 3.4869,
        "textwidth_in": 7.13989,
        "columnsep_in": 0.16604,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 2,
    },
    # Springer LNCS: 1-column, 10pt
    "lncs": {
        "columnwidth_in": 4.78,    # 122mm
        "textwidth_in": 4.78,
        "columnsep_in": 0,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 1,
    },
    # NeurIPS: 1-column body, 5.5in text
    "neurips": {
        "columnwidth_in": 5.5,
        "textwidth_in": 5.5,
        "columnsep_in": 0,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 1,
    },
    # ICML: 2-column, 10pt
    "icml": {
        "columnwidth_in": 3.25,
        "textwidth_in": 6.75,
        "columnsep_in": 0.25,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 2,
    },
    # ACM sigconf (SOSP, EuroSys, SIGCOMM, etc.): 2-column, 9pt, letter paper
    "sigconf": {
        "columnwidth_in": 3.33,
        "textwidth_in": 6.92,
        "columnsep_in": 0.25,
        "font_size_pt": 9,
        "font_family": "serif",
        "columns": 2,
    },
    # AAAI: 2-column, 10pt
    "aaai": {
        "columnwidth_in": 3.3125,
        "textwidth_in": 7.0,
        "columnsep_in": 0.375,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 2,
    },
    # MLSys: 2-column, 10pt (similar to ICML)
    "mlsys": {
        "columnwidth_in": 3.25,
        "textwidth_in": 6.75,
        "columnsep_in": 0.25,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 2,
    },
    # USENIX (OSDI/ATC/FAST): 2-column, 10pt, letter paper
    "usenix": {
        "columnwidth_in": 3.335,
        "textwidth_in": 7.0,
        "columnsep_in": 0.33,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 2,
    },
    # *ACL (ACL/EMNLP/NAACL via acl.sty): 2-column, 11pt
    "acl": {
        "columnwidth_in": 3.03149,
        "textwidth_in": 6.29921,
        "columnsep_in": 0.23622,
        "font_size_pt": 11,
        "font_family": "serif",
        "columns": 2,
    },
    # CVF (CVPR/ICCV/WACV via cvpr.sty): 2-column, 10pt
    "cvpr": {
        "columnwidth_in": 3.28125,
        "textwidth_in": 6.875,
        "columnsep_in": 0.3125,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 2,
    },
    # Bundled workshop/technical-report article: letter paper, 1-inch margins.
    "article": {
        "columnwidth_in": 6.5,
        "textwidth_in": 6.5,
        "columnsep_in": 0,
        "font_size_pt": 11,
        "font_family": "serif",
        "columns": 1,
    },
    # TMLR (Transactions on ML Research, JMLR-style): 1-column, 10pt
    "tmlr": {
        "columnwidth_in": 6.5,
        "textwidth_in": 6.5,
        "columnsep_in": 0,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 1,
    },
}

# Historical venue formats accepted by both template copying and plot sizing.
BUNDLED_VENUE_ALIASES = {
    # *ACL family → acl.sty
    "emnlp": "acl", "naacl": "acl", "eacl": "acl", "aacl": "acl", "coling": "acl",
    # All these bundled formats use the sigplan template.
    "euromlsys": "acmart", "sigplan": "acmart", "sosp": "acmart",
    "eurosys": "acmart", "asplos": "acmart", "acm": "acmart",
    # IEEEtran
    "infocom": "ieee",
    # USENIX systems/security
    "osdi": "usenix", "nsdi": "usenix", "atc": "usenix", "fast": "usenix",
    # CVF
    "iccv": "cvpr", "wacv": "cvpr",
}

# Geometry names for document classes and custom template detection.
VENUE_ALIASES = {
    "acmart": "acmart-sigplan",   # consolidated acmart engine ships [sigplan] main.tex (SOSP/EuroSys/ASPLOS/EuroMLSys)
    "acmsmall": "acmart-small",
    "acmlarge": "acmart-large",
    "pldi": "acmart-sigplan",
    "iclr": "neurips",
}


def _preset_key(venue_format: str) -> str:
    key = (venue_format or "").lower().strip()
    engine = BUNDLED_VENUE_ALIASES.get(key, key)
    return VENUE_ALIASES.get(engine, engine)


def get_geometry(venue_format: str, paper_dir=None) -> dict:
    """Get page geometry for a venue format.

    Args:
        venue_format: Venue name or format string (e.g. "sigplan", "ieee", "neurips").
        paper_dir: Optional project ``paper/`` directory. When ``venue_format``
            is unknown (notably ``"custom"`` from user uploads), consult
            ``template_manifest.yaml`` for ``detected_venue_format`` written
            by ``preprocess_custom_template``. Without this, custom NeurIPS
            uploads silently get acmart-sigplan's 2-column 3.33in geometry.

    Returns:
        Dict with columnwidth_in, textwidth_in, font_size_pt, etc.
    """
    key = _preset_key(venue_format)
    if key in VENUE_PRESETS:
        return dict(VENUE_PRESETS[key])

    if paper_dir is not None:
        try:
            import yaml
            from pathlib import Path
            manifest_path = Path(paper_dir) / "template_manifest.yaml"
            if manifest_path.exists():
                data = yaml.safe_load(manifest_path.read_text()) or {}
                detected = _preset_key(data.get("detected_venue_format"))
                if detected in VENUE_PRESETS:
                    return dict(VENUE_PRESETS[detected])
        except Exception:
            pass

    # Fall back to acmart-sigplan if unknown
    return dict(VENUE_PRESETS["acmart-sigplan"])


def get_matplotlib_rcparams(geometry: dict) -> dict:
    """Convert geometry to matplotlib rcParams for publication-quality figures.

    Figures created with these rcParams will have text at the correct
    size relative to the LaTeX template, so no scaling artifacts occur.
    """
    font_size = geometry["font_size_pt"]
    col_w = geometry["columnwidth_in"]

    # Scale factor: matplotlib font sizes should match LaTeX body text
    # when the figure is included at \columnwidth
    return {
        # Font
        "font.size": font_size,
        "font.family": geometry.get("font_family", "serif"),
        # Axes
        "axes.titlesize": font_size,
        "axes.labelsize": font_size,
        "axes.linewidth": 0.8,
        # Ticks
        "xtick.labelsize": font_size - 1,
        "ytick.labelsize": font_size - 1,
        "xtick.major.width": 0.6,
        "ytick.major.width": 0.6,
        # Legend
        "legend.fontsize": font_size - 1,
        "legend.framealpha": 0.8,
        # Figure defaults (single-column)
        "figure.figsize": [col_w, col_w * 0.7],
        "figure.dpi": 300,
        "figure.autolayout": True,
        # Savefig
        "savefig.dpi": 300,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
        # Lines
        "lines.linewidth": 1.2,
        "lines.markersize": 4,
    }


def write_figure_config(geometry: dict, output_path: Path):
    """Write figure configuration JSON for plotting scripts to import.

    The JSON includes raw geometry values and ready-to-use matplotlib rcParams.
    """
    config = {
        "geometry": geometry,
        "matplotlib_rcparams": get_matplotlib_rcparams(geometry),
        "sizes": {
            "single_column": [geometry["columnwidth_in"], geometry["columnwidth_in"] * 0.7],
            "single_column_tall": [geometry["columnwidth_in"], geometry["columnwidth_in"] * 1.0],
            "double_column": [geometry["textwidth_in"], geometry["textwidth_in"] * 0.35],
            "double_column_tall": [geometry["textwidth_in"], geometry["textwidth_in"] * 0.5],
        },
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(config, f, indent=2)
