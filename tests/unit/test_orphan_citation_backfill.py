"""_resolve_orphan_citations: backfill references.bib for cited-but-missing keys.

The recurring missing-references failure: main.tex cites keys that aren't in
references.bib, so every one renders "?". Backfill resolves each orphan key
against academic DBs and re-keys the real BibTeX to the cite key.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class _Compiler:
    # minimal host exposing the mixin's attributes the method needs
    from ark.latex.compiler import CompilerMixin as _M
    _CITE_CMD_RE = _M._CITE_CMD_RE
    _BIB_KEY_RE = _M._BIB_KEY_RE
    _resolve_orphan_citations = _M._resolve_orphan_citations

    def __init__(self, latex_dir):
        self.latex_dir = Path(latex_dir)
        self.logs = []

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))


def _paper(title):
    p = MagicMock()
    p.title = title
    return p


def test_backfill_rekeys_real_bibtex_to_cite_key(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"Body \cite{pedregosa2011scikit} and \cite{hastie2009elements}.")
    (tmp_path / "references.bib").write_text("")  # empty — both are orphans
    c = _Compiler(tmp_path)

    with patch("ark.citation.search_papers", return_value=[_paper("Scikit-learn: ML in Python")]), \
         patch("ark.citation.fetch_bibtex", return_value="@article{PedregosaFabian2011,\n  title = {Scikit-learn}\n}"):
        n = c._resolve_orphan_citations()

    assert n == 2
    bib = (tmp_path / "references.bib").read_text()
    # entries re-keyed to the ORIGINAL cite keys so \cite resolves
    assert "@article{pedregosa2011scikit," in bib
    assert "@article{hastie2009elements," in bib


def test_no_orphans_when_all_cited_keys_present(tmp_path):
    (tmp_path / "main.tex").write_text(r"\cite{known}.")
    (tmp_path / "references.bib").write_text("@book{known, title={X}}")
    c = _Compiler(tmp_path)
    with patch("ark.citation.search_papers") as sp:
        n = c._resolve_orphan_citations()
    assert n == 0
    sp.assert_not_called()


def test_unresolvable_key_is_skipped_not_faked(tmp_path):
    (tmp_path / "main.tex").write_text(r"\cite{madeup2099nonsense}.")
    (tmp_path / "references.bib").write_text("")
    c = _Compiler(tmp_path)
    with patch("ark.citation.search_papers", return_value=[]):  # DB finds nothing
        n = c._resolve_orphan_citations()
    assert n == 0
    assert "madeup2099nonsense" not in (tmp_path / "references.bib").read_text()
