"""Programmatic overlap detection and auto-fix for matplotlib figures.

Detects overlapping text elements (tick labels, annotations, legends) in
generated matplotlib figures using bounding box intersection checks.
Applies automatic fixes (rotation, resizing, repositioning) and generates
a report for the visualizer agent.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


def detect_overlaps_in_figure(figure_path: Path) -> dict:
    """Detect text overlaps in a saved matplotlib figure by re-opening it.

    Args:
        figure_path: Path to a PNG figure file.

    Returns:
        Dict with overlap info: {"has_overlaps": bool, "overlaps": [...], "density": float}
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from PIL import Image
    except ImportError:
        return {"has_overlaps": False, "overlaps": [], "density": 0, "error": "matplotlib not available"}

    # We can't re-open a PNG as a matplotlib figure with axes/text objects.
    # This function is for figures that are still in memory (see detect_overlaps_from_script).
    return {"has_overlaps": False, "overlaps": [], "density": 0,
            "note": "Static PNG analysis not supported; use detect_overlaps_from_script"}


def detect_overlaps_from_script(script_path: Path, figures_dir: Path,
                                 figure_config: dict = None) -> dict:
    """Run a plotting script with overlap detection injected.

    Executes the script in a subprocess with a monkey-patched plt.savefig
    that checks for overlaps before saving.

    Args:
        script_path: Path to the Python plotting script
        figures_dir: Directory where figures are saved
        figure_config: Dict with geometry (columnwidth_in, font_size_pt, etc.)

    Returns:
        {"figures": [{"name": str, "has_overlaps": bool, "overlaps": [...], ...}],
         "summary": {"total": int, "with_overlaps": int}}
    """
    script_path = Path(script_path).resolve()
    figures_dir = Path(figures_dir).resolve()

    if not script_path.exists():
        return {"figures": [], "summary": {"total": 0, "with_overlaps": 0},
                "error": f"Script not found: {script_path}"}

    # Build the overlap detection wrapper script
    report_path = figures_dir / "overlap_report.json"
    wrapper = _build_detection_wrapper(script_path, report_path, figure_config)

    # Write wrapper to a temp file (avoids shell arg length limits)
    import tempfile
    wrapper_file = Path(tempfile.mktemp(suffix="_overlap_check.py"))
    try:
        wrapper_file.write_text(wrapper)
        result = subprocess.run(
            [sys.executable, str(wrapper_file)],
            capture_output=True, text=True, timeout=120,
            cwd=str(script_path.parent),
            env={**__import__("os").environ, "MPLBACKEND": "Agg"},
        )
        # Only treat as hard failure if no report was generated
        # (warnings on stderr are normal for matplotlib)
    except subprocess.TimeoutExpired:
        return {"figures": [], "summary": {"total": 0, "with_overlaps": 0},
                "error": "Script timed out"}
    finally:
        wrapper_file.unlink(missing_ok=True)

    # Read the generated report
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
            return report
        except json.JSONDecodeError:
            pass

    return {"figures": [], "summary": {"total": 0, "with_overlaps": 0}}


def _build_detection_wrapper(script_path: Path, report_path: Path,
                              figure_config: dict = None) -> str:
    """Build a Python script that wraps the original plotting script with overlap detection."""
    col_w = (figure_config or {}).get("columnwidth_in", 5.5)
    font_size = (figure_config or {}).get("font_size_pt", 10)

    return f'''
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import json
from pathlib import Path

_overlap_report = {{"figures": [], "summary": {{"total": 0, "with_overlaps": 0}}}}
_original_savefig = plt.Figure.savefig

def _check_overlaps(fig, fname, **kwargs):
    """Check for text overlaps in a figure before saving."""
    name = Path(str(fname)).stem if fname else "unknown"
    canvas = fig.canvas
    if canvas is None:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        canvas = FigureCanvasAgg(fig)
    renderer = canvas.get_renderer()

    overlaps = []

    for ax_idx, ax in enumerate(fig.get_axes()):
        # Collect all text bounding boxes
        text_items = []

        # X-axis tick labels
        for label in ax.get_xticklabels():
            txt = label.get_text().strip()
            if txt:
                try:
                    bb = label.get_window_extent(renderer)
                    if bb.width > 0 and bb.height > 0:
                        text_items.append(("xtick", txt, bb))
                except Exception:
                    pass

        # Y-axis tick labels
        for label in ax.get_yticklabels():
            txt = label.get_text().strip()
            if txt:
                try:
                    bb = label.get_window_extent(renderer)
                    if bb.width > 0 and bb.height > 0:
                        text_items.append(("ytick", txt, bb))
                except Exception:
                    pass

        # Text annotations
        for text_obj in ax.texts:
            txt = text_obj.get_text().strip()
            if txt:
                try:
                    bb = text_obj.get_window_extent(renderer)
                    if bb.width > 0 and bb.height > 0:
                        text_items.append(("annotation", txt, bb))
                except Exception:
                    pass

        # Axis labels
        for label_obj, label_type in [(ax.xaxis.label, "xlabel"), (ax.yaxis.label, "ylabel")]:
            txt = label_obj.get_text().strip()
            if txt:
                try:
                    bb = label_obj.get_window_extent(renderer)
                    if bb.width > 0 and bb.height > 0:
                        text_items.append((label_type, txt, bb))
                except Exception:
                    pass

        # Title
        if ax.get_title().strip():
            try:
                bb = ax.title.get_window_extent(renderer)
                if bb.width > 0 and bb.height > 0:
                    text_items.append(("title", ax.get_title().strip(), bb))
            except Exception:
                pass

        # Pairwise overlap check
        for i in range(len(text_items)):
            for j in range(i + 1, len(text_items)):
                type_i, txt_i, bb_i = text_items[i]
                type_j, txt_j, bb_j = text_items[j]
                if bb_i.overlaps(bb_j):
                    # Calculate overlap severity
                    intersection = matplotlib.transforms.Bbox.intersection(bb_i, bb_j)
                    if intersection is not None:
                        overlap_area = intersection.width * intersection.height
                        min_area = min(bb_i.width * bb_i.height, bb_j.width * bb_j.height)
                        severity = min(1.0, overlap_area / max(min_area, 1e-6))
                    else:
                        severity = 0.1

                    if severity > 0.05:  # Ignore trivial overlaps (<5%)
                        overlaps.append({{
                            "axis": ax_idx,
                            "type1": type_i, "text1": txt_i[:30],
                            "type2": type_j, "text2": txt_j[:30],
                            "severity": round(severity, 3),
                        }})

    # Calculate text density
    fig_bb = fig.get_window_extent(renderer)
    fig_area = max(fig_bb.width * fig_bb.height, 1)
    text_area = 0
    for ax in fig.get_axes():
        for items in [ax.get_xticklabels(), ax.get_yticklabels(), ax.texts]:
            for t in items:
                try:
                    bb = t.get_window_extent(renderer)
                    text_area += bb.width * bb.height
                except Exception:
                    pass
    density = min(1.0, text_area / fig_area)

    fig_report = {{
        "name": name,
        "has_overlaps": len(overlaps) > 0,
        "overlap_count": len(overlaps),
        "overlaps": overlaps[:20],  # Cap at 20
        "density": round(density, 3),
    }}

    # Generate suggestions
    suggestions = []
    xtick_overlaps = [o for o in overlaps if o["type1"] == "xtick" or o["type2"] == "xtick"]
    if xtick_overlaps:
        suggestions.append("rotate_xtick_labels_45")
    ytick_overlaps = [o for o in overlaps if o["type1"] == "ytick" or o["type2"] == "ytick"]
    if ytick_overlaps:
        suggestions.append("increase_figure_width")
    annotation_overlaps = [o for o in overlaps if "annotation" in o["type1"] or "annotation" in o["type2"]]
    if annotation_overlaps:
        suggestions.append("use_adjustText_or_reposition")
    if density > 0.5:
        suggestions.append("increase_figsize_or_reduce_fontsize")
    fig_report["suggestions"] = suggestions

    _overlap_report["figures"].append(fig_report)
    _overlap_report["summary"]["total"] += 1
    if overlaps:
        _overlap_report["summary"]["with_overlaps"] += 1

    # Call original savefig
    _original_savefig(fig, fname, **kwargs)

# Monkey-patch savefig
plt.Figure.savefig = _check_overlaps

# Run the original script
import runpy
runpy.run_path("{script_path.resolve()}", run_name="__main__")

# Restore and write report
plt.Figure.savefig = _original_savefig
report_path = Path("{report_path}")
report_path.write_text(json.dumps(_overlap_report, indent=2))
'''


def apply_auto_fixes(script_path: Path, overlap_report: dict,
                      figure_config: dict = None) -> bool:
    """Apply automatic fixes to a plotting script based on overlap report.

    Modifies the script in-place. Returns True if any fixes were applied.

    Fix strategies (applied in order):
    1. Add constrained_layout=True to figure creation
    2. Rotate x-tick labels 45 degrees for x-tick overlaps
    3. Increase figsize height for density issues
    4. Add tight_layout with generous padding
    """
    if not overlap_report.get("figures"):
        return False

    figures_with_issues = [f for f in overlap_report["figures"] if f["has_overlaps"]]
    if not figures_with_issues:
        return False

    script_content = script_path.read_text()
    original_content = script_content
    col_w = (figure_config or {}).get("columnwidth_in", 5.5)
    font_size = (figure_config or {}).get("font_size_pt", 10)

    # Collect all suggestions
    all_suggestions = set()
    for fig_info in figures_with_issues:
        all_suggestions.update(fig_info.get("suggestions", []))

    # Fix 1: Add tight_layout before every savefig if not already present
    # (more reliable than constrained_layout which can conflict with manual positioning)
    if "tight_layout" not in script_content and "_overlap_fix_tight" not in script_content:
        script_content = re.sub(
            r'(\s*)(\w+\.savefig\()',
            r'\1try: plt.tight_layout(pad=1.5)  # _overlap_fix_tight\n\1except Exception: pass\n\1\2',
            script_content,
        )

    # Fix 2: Rotate x-tick labels if overlap detected
    if "rotate_xtick_labels_45" in all_suggestions:
        # Add rotation before each savefig call (handles fig.savefig, plt.savefig, etc.)
        savefig_pattern = r'(\s*)(\w+\.savefig\()'
        if re.search(savefig_pattern, script_content):
            # Only insert once per savefig (avoid duplicating on re-runs)
            if '_overlap_fix_rotation' not in script_content:
                script_content = re.sub(
                    r'(\s*)(\w+\.savefig\()',
                    r'\1try:  # _overlap_fix_rotation\n'
                    r'\1    for _ax in plt.gcf().get_axes():\n'
                    r'\1        _ax.tick_params(axis="x", rotation=45)\n'
                    r'\1        plt.setp(_ax.get_xticklabels(), ha="right")\n'
                    r'\1except Exception: pass\n'
                    r'\1\2',
                    script_content,
                )

    # Fix 3: Increase figsize for density issues
    if "increase_figsize_or_reduce_fontsize" in all_suggestions:
        # Find figsize patterns and increase height by 30%
        def _increase_height(match):
            w = float(match.group(1))
            h = float(match.group(2))
            return f"figsize=({w}, {h * 1.3:.2f})"
        script_content = re.sub(
            r'figsize=\(([0-9.]+),\s*([0-9.]+)\)',
            _increase_height,
            script_content,
        )

    # Fix 4: Remove in-figure titles (LaTeX caption handles this)
    # Replace ax.set_title(...) with empty title
    if "_overlap_fix_notitle" not in script_content:
        # Comment out set_title calls
        script_content = re.sub(
            r'^(\s*)(ax\w*\.set_title\()',
            r'\1# \2  # _overlap_fix_notitle (removed: LaTeX caption handles titles)',
            script_content,
            flags=re.MULTILINE,
        )

    if script_content != original_content:
        script_path.write_text(script_content)
        return True

    return False


def check_and_fix_figures(script_path: Path, figures_dir: Path,
                           figure_config: dict = None, log_fn=None) -> dict:
    """Full pipeline: detect overlaps, apply fixes, re-run if needed.

    Args:
        script_path: Path to the plotting script
        figures_dir: Directory for figure output
        figure_config: Geometry config dict
        log_fn: Optional logging function(msg, level)

    Returns:
        Final overlap report dict.
    """
    def _log(msg, level="INFO"):
        if log_fn:
            log_fn(msg, level)

    # Step 1: Detect overlaps
    _log("Checking figures for text overlaps...")
    report = detect_overlaps_from_script(script_path, figures_dir, figure_config)

    if report.get("error"):
        _log(f"Overlap detection error: {report['error']}", "WARN")
        return report

    total = report.get("summary", {}).get("total", 0)
    with_overlaps = report.get("summary", {}).get("with_overlaps", 0)

    if with_overlaps == 0:
        _log(f"No overlaps detected in {total} figures")
        return report

    _log(f"Found overlaps in {with_overlaps}/{total} figures")
    for fig_info in report.get("figures", []):
        if fig_info["has_overlaps"]:
            _log(f"  {fig_info['name']}: {fig_info['overlap_count']} overlaps, "
                 f"density={fig_info['density']}, suggestions={fig_info.get('suggestions', [])}")

    # Step 2: Apply auto-fixes
    _log("Applying auto-fixes to plotting script...")
    fixed = apply_auto_fixes(script_path, report, figure_config)

    if not fixed:
        _log("No auto-fixes could be applied")
        return report

    _log("Auto-fixes applied, re-running script...")

    # Step 3: Re-run and re-detect
    report2 = detect_overlaps_from_script(script_path, figures_dir, figure_config)
    remaining = report2.get("summary", {}).get("with_overlaps", 0)

    if remaining == 0:
        _log(f"All overlaps resolved after auto-fix!")
    else:
        _log(f"After auto-fix: {remaining} figures still have overlaps (was {with_overlaps})")

    return report2
