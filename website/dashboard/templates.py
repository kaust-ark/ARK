"""Venue LaTeX template management.

Copies a pre-built venue skeleton from venue_templates/<venue>/ into
the project's paper/ directory. Falls back to the 'article' skeleton
if the requested venue is not found.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# venue_templates/ lives at the ARK repo root
_TEMPLATES_ROOT = Path(__file__).parent.parent.parent / "venue_templates"

# Back-compat: pre-consolidation venue_format keys → the engine dir that now
# holds the shared template files. The venue_templates/ tree was consolidated
# to one directory per LaTeX engine (acl/acmart/usenix/ieee/cvpr/...), but
# existing projects' saved venue_format, and the webapp's hardcoded "Test"
# venue (data-format="euromlsys"), still pass the old per-venue keys. Resolve
# them here so copy/has lookups keep working. A literal dir match always wins.
_VENUE_FORMAT_ALIASES = {
    # *ACL family → acl.sty
    "emnlp": "acl", "naacl": "acl", "eacl": "acl", "aacl": "acl", "coling": "acl",
    # ACM acmart family (different \documentclass options, one .cls)
    "euromlsys": "acmart", "sigplan": "acmart", "sosp": "acmart",
    "eurosys": "acmart", "asplos": "acmart", "acm": "acmart",
    # IEEEtran
    "infocom": "ieee",
    # USENIX systems/security
    "osdi": "usenix", "nsdi": "usenix", "atc": "usenix", "fast": "usenix",
    # CVF
    "iccv": "cvpr", "wacv": "cvpr",
}


def _resolve_venue_format(venue_format: str) -> str:
    """Map a (possibly legacy) venue_format onto its bundled engine dir name.

    A directory that literally matches ``venue_format`` always wins; only when
    no such dir exists do we consult the legacy alias table.
    """
    if (_TEMPLATES_ROOT / venue_format).exists():
        return venue_format
    return _VENUE_FORMAT_ALIASES.get(venue_format, venue_format)


def _is_safe_venue_format(venue_format: str) -> bool:
    """A venue_format is only ever a bundled template *directory name*.

    Reject anything that could escape ``_TEMPLATES_ROOT`` via a path separator,
    parent reference, or absolute path. Without this, ``_TEMPLATES_ROOT / value``
    resolves outside the templates tree (``/ "/etc"`` collapses to ``/etc``;
    ``"../.."`` walks upward), letting a caller copy arbitrary server files into
    a project and exfiltrate them (path traversal).
    """
    if not isinstance(venue_format, str) or not venue_format:
        return False
    if "/" in venue_format or "\\" in venue_format or ".." in venue_format:
        return False
    return True


def _resolved_template_dir(venue_format: str) -> "Path | None":
    """Resolve venue_format to its bundled template dir, or None if unsafe/absent.

    Enforces that the resolved directory is a direct child of ``_TEMPLATES_ROOT``
    so a crafted venue_format cannot traverse outside the templates tree.
    """
    if not _is_safe_venue_format(venue_format):
        return None
    name = _resolve_venue_format(venue_format)
    if not _is_safe_venue_format(name):  # alias table is static, but re-check
        return None
    root = _TEMPLATES_ROOT.resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_dir():
        return None
    return candidate

# test_fixtures/ (cheap "test project" preset) also lives at the repo root.
_TEST_FIXTURES_ROOT = Path(__file__).parent.parent.parent / "test_fixtures" / "cheap_run"


def read_test_idea() -> str:
    """Return the canned idea text for the cheap test-project preset."""
    f = _TEST_FIXTURES_ROOT / "idea.md"
    return f.read_text() if f.exists() else ""


def copy_test_fixtures(code_dir: Path, seed_deep_research: bool = True,
                       seed_figures: bool = False) -> bool:
    """Seed a project with cheap test-project fixtures (the "Test" venue).

    - ``seed_deep_research``: drop the canned Deep Research report into the
      project state dir so the pipeline SKIPS the (expensive) live Deep Research
      call — Step 2 skips when ``deep_research.md`` already exists.
    - ``seed_figures``: drop a canned concept figure + its spec + manifest into
      ``paper/figures/`` so the AI-figure phase's Phase-0 check sees the concept
      figures already exist and SKIPS generation (zero PaperBanana cost) while
      the writer still includes the figure — i.e. reuse an existing figure.

    The idea text is seeded via the project's ``idea`` field by the caller
    (read_test_idea), not here. Returns True if anything was copied.
    """
    code_dir = Path(code_dir)
    did = False
    if seed_deep_research:
        src = _TEST_FIXTURES_ROOT / "deep_research.md"
        if src.exists():
            state_dir = code_dir / "auto_research" / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, state_dir / "deep_research.md")
            did = True
    if seed_figures:
        fig_src = _TEST_FIXTURES_ROOT / "figures"
        if fig_src.exists():
            dest = code_dir / "paper" / "figures"
            dest.mkdir(parents=True, exist_ok=True)
            for item in fig_src.iterdir():
                if item.is_file():
                    shutil.copy2(item, dest / item.name)
            did = True
    return did


def get_available_venues() -> list[str]:
    if not _TEMPLATES_ROOT.exists():
        return []
    return sorted(d.name for d in _TEMPLATES_ROOT.iterdir() if d.is_dir())


def has_venue_template(venue_format: str) -> bool:
    """Return True if a bundled template exists for this venue format."""
    return _resolved_template_dir(venue_format) is not None


def copy_venue_template(venue_format: str, dest_paper_dir: Path) -> bool:
    """Copy venue LaTeX skeleton into dest_paper_dir.

    Returns True if a matching template was found and copied.
    """
    dest_paper_dir.mkdir(parents=True, exist_ok=True)

    src = _resolved_template_dir(venue_format)
    if src is None:
        # No bundled template (or an unsafe/escaping venue_format) — caller
        # should handle the waiting_template flow.
        return False

    for item in src.iterdir():
        dst = dest_paper_dir / item.name
        if item.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)

    # Auto-extract any .zip files (e.g. NeurIPS Styles.zip containing .sty files)
    import zipfile
    for zf_path in dest_paper_dir.glob("*.zip"):
        try:
            with zipfile.ZipFile(zf_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = Path(info.filename).name
                    if Path(name).suffix.lower() in (".sty", ".cls", ".bst"):
                        dst = dest_paper_dir / name
                        if not dst.exists():
                            with zf.open(info) as src_f, open(dst, "wb") as dst_f:
                                dst_f.write(src_f.read())
        except Exception:
            pass

    return True


def _write_minimal_skeleton(dest: Path):
    """Fallback: write a bare-bones article main.tex."""
    main_tex = dest / "main.tex"
    if main_tex.exists():
        return
    main_tex.write_text(r"""\documentclass[12pt]{article}
\usepackage{amsmath,amssymb,graphicx,hyperref}
\title{Title}
\author{Author}
\date{\today}
\begin{document}
\maketitle
\begin{abstract}
Abstract goes here.
\end{abstract}
\section{Introduction}
\section{Method}
\section{Experiments}
\section{Conclusion}
\bibliographystyle{plainnat}
\bibliography{references}
\end{document}
""")
    bib = dest / "references.bib"
    if not bib.exists():
        bib.write_text("")
