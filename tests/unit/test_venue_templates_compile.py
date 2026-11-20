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

import fitz
import pytest
from PIL import Image

from ark.cli import VENUES, OTHER_VENUES
from ark.latex_geometry import get_geometry
from website.dashboard.templates import (
    has_venue_template,
    copy_venue_template,
    _resolve_venue_format,
    _VENUE_FORMAT_ALIASES,
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
    for key in set(_LEGACY_KEYS) | set(_VENUE_FORMAT_ALIASES):
        assert has_venue_template(key), f"legacy key {key!r} no longer resolves"
        engine = _resolve_venue_format(key)
        assert engine in _ENGINE_DIRS
        assert get_geometry(key) == get_geometry(engine), key


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
    "icml": "icml2027",
    "iclr": "iclr2027_conference",
    "acl": "acl",
    "cvpr": "cvpr",
    "ieee": "IEEEtran",
    "acmart": "acmart",
    "usenix": "usenix-2020-09",
    "mlsys": "mlsys2025",
    "aaai": "aaai2027",
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

_BIB_ENTRY = """@article{shannon1948,
  author = {Claude E. Shannon},
  title = {A Mathematical Theory of Communication},
  journal = {Bell System Technical Journal},
  volume = {27}, number = {3}, pages = {379--423}, year = {1948}
}
"""

_EXPERIMENT = r"""\section{Experiments}
\makeatletter
\typeout{ARKCOL=\the\columnwidth;ARKTEXT=\the\textwidth;ARKCOLS=\if@twocolumn2\else1\fi}
\makeatother
Following \cite{shannon1948}, see \mbox{figure~record~\ref{fig:signal}}
and \mbox{table~record~\ref{tab:signal}}.
\begin{figure}[ht]
\centering\includegraphics[width=18mm]{signal.png}
\caption{Signal diagram.}\label{fig:signal}
\end{figure}
\begin{table}[ht]
\centering\caption{Signal levels.}\label{tab:signal}
\begin{tabular}{lr}Setting & Level \\ Baseline & 7\end{tabular}
\end{table}
"""
_APPENDIX = r"""\appendix
\section{Measurement details}
The calibration record accompanies the measurements.
"""
_MANUALLY_ANONYMOUS = {"article", "usenix", "ieee"}


@pytest.mark.skipif(not _HAVE_LATEX, reason="pdflatex/bibtex not installed")
@pytest.mark.parametrize("venue_format", sorted(_ENGINE_DIRS))
def test_bundled_template_compiles(venue_format, tmp_path):
    """Exercise the shipped submission scaffold with real rendered paper content."""
    pdir = tmp_path / venue_format
    assert copy_venue_template(venue_format, pdir)
    main_tex = pdir / "main.tex"
    src = main_tex.read_text().replace("Paper Title", "Signal Measurements")
    if venue_format not in _MANUALLY_ANONYMOUS:
        # A pre-filled "Anonymous Author" does not exercise the actual blind style.
        src, count = re.subn(r"Anonymous Author(?:\(s\))?|Anonymous Submission",
                             "Zirconauthor", src)
        assert count > 0, "no author to exercise anonymous mode"

    (pdir / "references.bib").write_text(_BIB_ENTRY)
    Image.new("RGB", (32, 16), "navy").save(pdir / "signal.png")
    assert r"\section{Experiments}" in src
    src = src.replace(r"\section{Experiments}", _EXPERIMENT)
    separate_appendix = venue_format in {"cvpr", "mlsys"}
    if separate_appendix:
        preamble = src.split(r"\section{Introduction}")[0]
        (pdir / "supplement.tex").write_text(preamble + _APPENDIX + r"\end{document}")
    else:
        before = {
            "neurips": r"\input{checklist.tex}",
            "usenix": r"\bibliographystyle{plainurl}",
        }.get(venue_format, r"\end{document}")
        assert before in src
        appendix = _APPENDIX.replace(r"\appendix", r"\appendices") if venue_format == "ieee" else _APPENDIX
        src = src.replace(before, appendix + "\n" + before)
    main_tex.write_text(src)
    env = {**os.environ, "TEXMFVAR": str(tmp_path / "texmfvar")}

    def run(cmd):
        result = subprocess.run(cmd, cwd=pdir, capture_output=True, text=True,
                                errors="replace", timeout=180, env=env)
        assert result.returncode == 0, f"{cmd}:\n{result.stdout[-3500:]}\n{result.stderr[-1000:]}"

    latex = ["pdflatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
             "-file-line-error", "-recorder"]
    run([*latex, "main.tex"])
    run(["bibtex", "main"])
    run([*latex, "main.tex"])
    run([*latex, "main.tex"])

    log = (pdir / "main.log").read_text(errors="replace")
    assert not re.search(r"undefined (?:references|citations)|(?:Citation|Reference) .+ undefined|"
                         r"Rerun to get cross-references right|Overfull \\[hv]box", log, re.I)
    biblog = (pdir / "main.blg").read_text(errors="replace")
    assert "Database file #1: references.bib" in biblog
    assert not re.search(r"Warning--|error message|Illegal, another", biblog, re.I), biblog
    aux = (pdir / "main.aux").read_text(errors="replace")
    assert r"\citation{shannon1948}" in aux
    bib_style = re.search(r"\\bibstyle\{([^}]+)\}", aux)
    assert bib_style and f"The style file: {bib_style[1]}.bst" in biblog
    bbl = (pdir / "main.bbl").read_text(errors="replace")
    assert r"\bibitem" in bbl and "shannon1948" in bbl

    # An installed TeX Live copy must not mask a missing or misnamed bundled file.
    if venue_format in _EXPECTED_STYLE:
        style = _EXPECTED_STYLE[venue_format]
        paths = {
            (pdir / line[6:]).resolve()
            for line in (pdir / "main.fls").read_text().splitlines() if line.startswith("INPUT ")
        }
        assert any(pdir / f"{style}.{ext}" in paths for ext in ("sty", "cls")), style

    match = re.search(r"ARKCOL=([.0-9]+)pt;ARKTEXT=([.0-9]+)pt;ARKCOLS=([12])", log)
    assert match is not None, f"{venue_format}: no document geometry in compile log"
    geometry = get_geometry(venue_format)
    assert geometry["columns"] == int(match[3])
    # 72.27 TeX points per inch; the presets are rounded in inches.
    assert abs(geometry["columnwidth_in"] * 72.27 - float(match[1])) < 0.2
    assert abs(geometry["textwidth_in"] * 72.27 - float(match[2])) < 0.2

    with fitz.open(pdir / "main.pdf") as pdf:
        assert len(pdf) > 0 and not pdf.is_repaired
        text = " ".join(" ".join(page.get_text().split()) for page in pdf).casefold()
        assert any(page.get_image_info() for page in pdf), "no rendered figure"
        # IEEEtran numbers tables with uppercase Roman numerals.
        table_label = "i" if venue_format == "ieee" else "1"
        for fragment in ("signal diagram", "signal levels", "figure record 1", f"table record {table_label}",
                         "baseline 7", "shannon", "mathematical theory"):
            assert fragment in text, (venue_format, fragment)
        assert "??" not in text
        assert "anonymous" in text and "zirconauthor" not in text
        assert "zirconauthor" not in (pdf.metadata["author"] or "").casefold()

    if separate_appendix:
        assert "calibration record" not in text
        run([*latex, "supplement.tex"])
        run([*latex, "supplement.tex"])
        with fitz.open(pdir / "supplement.pdf") as supplement:
            assert "calibration record" in " ".join(page.get_text() for page in supplement).casefold()
    else:
        assert "calibration record" in text
        appendix_at = text.index("calibration record")
        reference_at = text.index("mathematical theory")
        if venue_format == "usenix":
            assert appendix_at < reference_at
        else:
            assert reference_at < appendix_at
        if venue_format == "neurips":
            assert appendix_at < text.index("neurips paper checklist")

    if venue_format == "aaai":
        run([*latex, "ReproducibilityChecklist.tex"])
        with fitz.open(pdir / "ReproducibilityChecklist.pdf") as checklist:
            assert "Reproducibility Checklist" in checklist[0].get_text()
