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


def get_available_venues() -> list[str]:
    if not _TEMPLATES_ROOT.exists():
        return []
    return sorted(d.name for d in _TEMPLATES_ROOT.iterdir() if d.is_dir())


def has_venue_template(venue_format: str) -> bool:
    """Return True if a bundled template exists for this venue format."""
    return (_TEMPLATES_ROOT / venue_format).exists()


def copy_venue_template(venue_format: str, dest_paper_dir: Path) -> bool:
    """Copy venue LaTeX skeleton into dest_paper_dir.

    Returns True if a matching template was found and copied.
    """
    dest_paper_dir.mkdir(parents=True, exist_ok=True)

    src = _TEMPLATES_ROOT / venue_format
    if not src.exists():
        # No bundled template — caller should handle waiting_template flow
        return False

    for item in src.iterdir():
        dst = dest_paper_dir / item.name
        if item.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
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
\bibliographystyle{plain}
\bibliography{references}
\end{document}
""")
    bib = dest / "references.bib"
    if not bib.exists():
        bib.write_text("")
