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
    1. Add tight_layout(pad=1.5) before every savefig
    2. Rotate x-tick labels 45 degrees for x-tick overlaps
    3. Increase figsize height for density issues
    4. Remove in-figure set_title calls (LaTeX caption owns titles)
    5. Increase figsize WIDTH (×1.25) when suggestion fires
    6. Inject adjustText for annotation/text repositioning
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

    # Fix 5: Increase figure WIDTH (×1.25) when detector suggests it.
    # Bookkeeping marker prevents stacking width increases across re-runs.
    # Distinct from Fix 3 which only grows height.
    if "increase_figure_width" in all_suggestions and "_overlap_fix_width" not in script_content:
        def _increase_width(match):
            w = float(match.group(1))
            h = float(match.group(2))
            return f"figsize=({w * 1.25:.2f}, {h})"
        new_content = re.sub(
            r'figsize=\(([0-9.]+),\s*([0-9.]+)\)',
            _increase_width,
            script_content,
        )
        if new_content != script_content:
            # Add an idempotency marker as a comment near the top of the file
            new_content = "# _overlap_fix_width applied\n" + new_content
            script_content = new_content

    # Fix 6: Inject adjustText to reposition annotations/text away from
    # ticks and other text. Particularly useful when an in-plot statistic
    # annotation (e.g., "χ²(...) p=...") collides with a ytick label.
    # adjustText is lazy-imported; if unavailable, the wrapper is a no-op
    # so paper compilation isn't blocked by a missing optional dep.
    if "use_adjustText_or_reposition" in all_suggestions and "_overlap_fix_adjusttext" not in script_content:
        adjusttext_setup = (
            "# _overlap_fix_adjusttext\n"
            "try:\n"
            "    from adjustText import adjust_text as _ark_adjust_text\n"
            "except ImportError:  # pragma: no cover - optional dep\n"
            "    _ark_adjust_text = None\n\n"
        )
        # Inject setup once near the top of the file (after the first
        # non-comment, non-import statement is risky; placing at the
        # top right after the shebang/docstring is safest).
        script_content = adjusttext_setup + script_content

        # Inject adjust_text() call before every savefig (idempotent —
        # wrapped in try/except + None-check on the imported callable).
        script_content = re.sub(
            r'(\s*)(\w+\.savefig\()',
            r'\1if _ark_adjust_text is not None:\n'
            r'\1    try:\n'
            r'\1        for _ax in plt.gcf().get_axes():\n'
            r'\1            _texts = list(_ax.texts)\n'
            r'\1            if _texts:\n'
            r'\1                _ark_adjust_text(_texts, ax=_ax,\n'
            r'\1                    only_move={"text": "y", "static": "y"})\n'
            r'\1    except Exception: pass\n'
            r'\1\2',
            script_content,
        )

    if script_content != original_content:
        script_path.write_text(script_content)
        return True

    return False


def check_and_fix_figures(script_path: Path, figures_dir: Path,
                           figure_config: dict = None, log_fn=None,
                           state_dir: Path = None,
                           max_attempts: int = 3) -> dict:
    """Full pipeline: detect overlaps, apply fixes, re-run if needed.

    Layer-1 (this function): cheap regex-based fixes that handle known
    patterns (xtick crowding, in-plot title pollution, undersized
    figsize, annotation collisions). Up to ``max_attempts`` rounds.

    Layer-2 (caller): when Layer-1 still leaves overlaps after the
    attempt budget, this function writes
    ``state_dir/unresolved_overlaps.yaml`` so the next planner pass can
    convert each entry into a FIGURE_CODE_REQUIRED task and let the
    coder agent redesign the figure intelligently. The caller does not
    need to do anything extra; the planner reads that file as part of
    its standard inputs.

    Args:
        script_path: Path to the plotting script
        figures_dir: Directory for figure output
        figure_config: Geometry config dict
        log_fn: Optional logging function(msg, level)
        state_dir: ``auto_research/state/`` for escalation handoff;
            if None, no escalation file is written (legacy callers).
        max_attempts: Layer-1 retry budget before escalating.

    Returns:
        Final overlap report dict, with extra key
        ``"escalation_needed": bool`` indicating that Layer-2 (the
        coder agent) must take over.
    """
    def _log(msg, level="INFO"):
        if log_fn:
            log_fn(msg, level)

    # Step 1: Detect overlaps
    _log("Checking figures for text overlaps...")
    report = detect_overlaps_from_script(script_path, figures_dir, figure_config)

    if report.get("error"):
        _log(f"Overlap detection error: {report['error']}", "WARN")
        report["escalation_needed"] = False
        return report

    total = report.get("summary", {}).get("total", 0)
    with_overlaps = report.get("summary", {}).get("with_overlaps", 0)
    initial_with_overlaps = with_overlaps

    if with_overlaps == 0:
        _log(f"No overlaps detected in {total} figures")
        report["escalation_needed"] = False
        return report

    _log(f"Found overlaps in {with_overlaps}/{total} figures")
    for fig_info in report.get("figures", []):
        if fig_info["has_overlaps"]:
            _log(f"  {fig_info['name']}: {fig_info['overlap_count']} overlaps, "
                 f"density={fig_info['density']}, suggestions={fig_info.get('suggestions', [])}")

    # Steps 2..N: apply Layer-1 fixes up to max_attempts. Each round
    # may pick a fresh combination of fixes if the script changed.
    current_report = report
    for attempt in range(1, max_attempts + 1):
        _log(f"Applying auto-fixes to plotting script... (attempt {attempt}/{max_attempts})")
        fixed = apply_auto_fixes(script_path, current_report, figure_config)
        if not fixed:
            _log("No auto-fixes could be applied this round")
            break
        _log("Auto-fixes applied, re-running script...")
        current_report = detect_overlaps_from_script(script_path, figures_dir, figure_config)
        remaining = current_report.get("summary", {}).get("with_overlaps", 0)
        if remaining == 0:
            _log(f"All overlaps resolved after auto-fix on attempt {attempt}")
            current_report["escalation_needed"] = False
            return current_report
        _log(f"After attempt {attempt}: {remaining} figures still have overlaps "
             f"(started with {initial_with_overlaps})")

    # Layer-1 exhausted without clean result → escalate to Layer-2.
    remaining = current_report.get("summary", {}).get("with_overlaps", 0)
    if remaining == 0:
        current_report["escalation_needed"] = False
        return current_report

    _log(f"Layer-1 auto-fix exhausted; escalating {remaining} figure(s) to coder agent",
         "WARN")
    current_report["escalation_needed"] = True

    if state_dir is not None:
        try:
            _write_overlap_escalation(current_report, Path(state_dir), log_fn)
        except Exception as e:
            _log(f"Failed to write escalation handoff: {e}", "WARN")

    return current_report


def _write_overlap_escalation(report: dict, state_dir: Path, log_fn=None) -> None:
    """Write ``unresolved_overlaps.yaml`` for the planner to pick up.

    Each entry describes one figure with its specific overlap details,
    which suggestions Layer-1 already tried, and a short rationale that
    the planner can paste into a FIGURE_CODE_REQUIRED task description.
    """
    import yaml

    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = state_dir / "unresolved_overlaps.yaml"

    entries = []
    for fig in report.get("figures", []):
        if not fig.get("has_overlaps"):
            continue
        # Deduplicate identical entries (the script can re-run and
        # produce repeated identical reports)
        overlaps_summary = []
        seen = set()
        for ov in fig.get("overlaps", []):
            key = (ov.get("type1"), ov.get("text1"), ov.get("type2"), ov.get("text2"))
            if key in seen:
                continue
            seen.add(key)
            overlaps_summary.append({
                "axis": ov.get("axis"),
                "kind": f"{ov.get('type1')} vs {ov.get('type2')}",
                "text1": (ov.get("text1") or "")[:120],
                "text2": (ov.get("text2") or "")[:120],
                "severity": ov.get("severity"),
            })
        entry = {
            "figure": fig.get("name"),
            "density": fig.get("density"),
            "tried_suggestions": fig.get("suggestions", []),
            "overlaps": overlaps_summary,
            "redesign_hints": [
                "Move the colliding annotation outside the plot area "
                "(e.g., into the figure caption or a textbox below the axes).",
                "Reduce annotation fontsize to 7pt and reposition with "
                "ax.text(..., transform=ax.transAxes) at a corner.",
                "Redesign the layout (e.g., 2x2 grouped bar instead of "
                "single axes with overlapping text).",
            ],
        }
        entries.append(entry)

    payload = {
        "version": 1,
        "generated_by": "ark.figure_overlap.check_and_fix_figures",
        "n_figures": len(entries),
        "figures": entries,
    }
    out_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    if log_fn:
        log_fn(f"Wrote overlap escalation: {out_path} ({len(entries)} figure(s))",
               "INFO")
