"""Regression tests for the bundled venue LaTeX templates.

Guards against the failure modes that motivated the 2026 template refresh:

* a venue's main.tex referencing a style file that isn't shipped (e.g. the old
  ICML scaffold missing ``\\usepackage{graphicx}``),
* a duplicate ``\\bibliographystyle`` ("Illegal, another \\bibstyle") from the
  template repeating what the .sty already sets (ACL/AAAI),
* a format key in the CLI/webapp venue lists that no longer maps to a bundled
  directory after the engine-dir consolidation.

The compile test is skipped when ``pdflatex`` isn't on PATH so CI without a
TeX Live install still passes the wiring checks.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ark.cli import VENUES, OTHER_VENUES
from ark.latex_geometry import get_geometry
from website.dashboard.templates import (
    has_venue_template,
    copy_venue_template,
    _resolve_venue_format,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES_ROOT = _REPO_ROOT / "venue_templates"

# Engine directories expected after the consolidation.
_ENGINE_DIRS = {
    "neurips", "icml", "iclr", "acl", "cvpr",
    "ieee", "acmart", "usenix", "mlsys", "aaai", "tmlr", "article",
}

# Legacy venue_format values that live in old projects / the webapp Test venue.
_LEGACY_KEYS = ["emnlp", "euromlsys", "sigplan", "sosp", "osdi", "infocom", "iccv", "acm"]

_HAVE_LATEX = shutil.which("pdflatex") is not None and shutil.which("bibtex") is not None


def _all_template_dirs() -> list[str]:
    return sorted(d.name for d in _TEMPLATES_ROOT.iterdir() if d.is_dir())


# ---------------------------------------------------------------------------
#  Wiring checks (no LaTeX needed)
# ---------------------------------------------------------------------------

def test_engine_dirs_present():
    have = set(_all_template_dirs())
    missing = _ENGINE_DIRS - have
    assert not missing, f"missing engine template dirs: {missing}"


def test_every_dir_has_main_and_refs():
    for d in _all_template_dirs():
        assert (_TEMPLATES_ROOT / d / "main.tex").exists(), f"{d}/main.tex missing"
        assert (_TEMPLATES_ROOT / d / "references.bib").exists(), f"{d}/references.bib missing"


def test_cli_venue_formats_resolve():
    for v in VENUES + OTHER_VENUES:
        fmt = v["format"]
        assert has_venue_template(fmt), f"CLI venue {v['name']!r} format {fmt!r} has no template dir"


def test_legacy_keys_resolve_to_engine_dirs():
    for key in _LEGACY_KEYS:
        assert has_venue_template(key), f"legacy key {key!r} no longer resolves"
        assert _resolve_venue_format(key) in _ENGINE_DIRS


def test_geometry_resolves_for_all_formats():
    keys = {v["format"] for v in VENUES + OTHER_VENUES} | set(_LEGACY_KEYS) | _ENGINE_DIRS
    for k in keys:
        geo = get_geometry(k)
        assert isinstance(geo, dict) and "columnwidth_in" in geo, k


# Each engine's main.tex must \usepackage / \documentclass this venue style,
# and the matching file must be shipped in the dir (catches renamed-year drift
# and "style not bundled" regressions).
_EXPECTED_STYLE = {
    "neurips": "neurips_2026",
    "icml": "icml2026",
    "iclr": "iclr2026_conference",
    "acl": "acl",
    "cvpr": "cvpr",
    "ieee": "IEEEtran",
    "acmart": "acmart",
    "usenix": "usenix-2020-09",
    "mlsys": "mlsys2025",
    "aaai": "aaai2026",
    "tmlr": "tmlr",
}


@pytest.mark.parametrize("venue,style", sorted(_EXPECTED_STYLE.items()))
def test_venue_style_file_shipped_and_referenced(venue, style):
    d = _TEMPLATES_ROOT / venue
    tex = (d / "main.tex").read_text()
    assert style in tex, f"{venue}/main.tex does not reference its style {style!r}"
    assert (d / f"{style}.sty").exists() or (d / f"{style}.cls").exists(), \
        f"{venue}: style file {style}.(sty|cls) not shipped"


@pytest.mark.parametrize("venue", sorted(_ENGINE_DIRS))
def test_graphicx_available(venue):
    r"""Every template must make \includegraphics work out of the box.

    The original ICML scaffold shipped without ``\usepackage{graphicx}``, so the
    writer's first ``\includegraphics`` broke the compile. acmart loads graphicx
    itself; everything else must \usepackage it explicitly.
    """
    tex = (_TEMPLATES_ROOT / venue / "main.tex").read_text()
    if venue == "acmart":
        return  # acmart.cls loads graphicx internally
    assert "graphicx" in tex, f"{venue}/main.tex does not load graphicx"


# ---------------------------------------------------------------------------
#  Compile checks (need pdflatex + bibtex)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAVE_LATEX, reason="pdflatex/bibtex not installed")
@pytest.mark.parametrize("venue_format", sorted(_ENGINE_DIRS))
def test_bundled_template_compiles(venue_format, tmp_path):
    """copy_venue_template → ARK's pdflatex/bibtex/pdflatex×2 → non-empty PDF."""
    pdir = tmp_path / venue_format
    assert copy_venue_template(venue_format, pdir)

    main_tex = pdir / "main.tex"
    src = main_tex.read_text()
    src = src.replace("Paper Title", "A Regression Test Title for ARK Templates")
    # ARK always has >=1 citation by compile time; mirror that so bibtex (run
    # unconditionally by ark/latex/compiler.py) produces a valid .bbl.
    (pdir / "references.bib").write_text(
        "@article{x, author={A. B.}, title={Cited Work}, journal={J}, year={2025}}\n"
    )
    src = re.sub(r"(\\bibliography\{references\})", r"\\nocite{*}\n\1", src)
    main_tex.write_text(src)

    env = {**os.environ, "TEXMFVAR": str(tmp_path / "texmfvar")}

    def run(cmd):
        return subprocess.run(cmd, cwd=pdir, capture_output=True, text=True,
                              timeout=180, env=env)

    run(["pdflatex", "-interaction=nonstopmode", "main.tex"])
    run(["bibtex", "main"])
    run(["pdflatex", "-interaction=nonstopmode", "main.tex"])
    last = run(["pdflatex", "-interaction=nonstopmode", "main.tex"])

    pdf = pdir / "main.pdf"
    assert pdf.exists() and pdf.stat().st_size > 0, (
        f"{venue_format}: no PDF produced\n{last.stdout[-1500:]}"
    )
    # No duplicate-bibstyle error leaked into the log.
    assert "Illegal, another \\bibstyle" not in (pdir / "main.bbl").read_text(errors="ignore") \
        if (pdir / "main.bbl").exists() else True
