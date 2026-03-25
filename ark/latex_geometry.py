"""LaTeX template geometry extraction and matplotlib configuration.

Provides venue-specific page dimensions so that matplotlib figures
are generated at the exact size expected by the LaTeX template,
eliminating scaling artifacts (wrong font size, overflow, etc.).
"""

import json
from pathlib import Path

# Venue presets: measured from actual template .cls files
# All lengths in inches unless noted; 1pt = 1/72 inch
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
        "columnwidth_in": 3.5,
        "textwidth_in": 7.25,
        "columnsep_in": 0.25,
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
        "columnwidth_in": 3.3,
        "textwidth_in": 6.85,
        "columnsep_in": 0.25,
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
        "columnwidth_in": 3.375,
        "textwidth_in": 7.0,
        "columnsep_in": 0.25,
        "font_size_pt": 10,
        "font_family": "serif",
        "columns": 2,
    },
}

# Aliases for common names
VENUE_ALIASES = {
    "sigplan": "acmart-sigplan",
    "acmsmall": "acmart-small",
    "acmlarge": "acmart-large",
    "acm": "sigconf",
    "euromlsys": "acmart-sigplan",
    "pldi": "acmart-sigplan",
    "asplos": "acmart-large",
    "sosp": "acmart-large",
    "iclr": "neurips",
    "infocom": "ieee",
}


def get_geometry(venue_format: str) -> dict:
    """Get page geometry for a venue format.

    Args:
        venue_format: Venue name or format string (e.g. "sigplan", "ieee", "neurips")

    Returns:
        Dict with columnwidth_in, textwidth_in, font_size_pt, etc.
    """
    key = venue_format.lower().strip()
    key = VENUE_ALIASES.get(key, key)
    if key in VENUE_PRESETS:
        return dict(VENUE_PRESETS[key])
    # Default to acmart-sigplan if unknown
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
