"""Regression tests for _prune_undefined_citations.

Removes \\cite-family commands whose keys aren't present in
references.bib before pdflatex runs, so the rendered PDF doesn't show
"[?]" markers and bibtex doesn't have to chase phantom keys.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def compiler(tmp_path):
    from ark.latex.compiler import CompilerMixin

    latex_dir = tmp_path / "paper"
    latex_dir.mkdir(parents=True)

    stub = MagicMock(spec=CompilerMixin)
    stub.latex_dir = latex_dir
    stub.log = MagicMock()
    stub._CITE_CMD_RE = CompilerMixin._CITE_CMD_RE
    stub._BIB_KEY_RE = CompilerMixin._BIB_KEY_RE
    stub._prune_undefined_citations = (
        CompilerMixin._prune_undefined_citations.__get__(stub)
    )
    return stub


def _write(stub, main_tex_body: str, bib: str = ""):
    (stub.latex_dir / "main.tex").write_text(main_tex_body)
    (stub.latex_dir / "references.bib").write_text(bib)


_BIB_FOO_BAR = """\
@article{foo2020alpha,
  title={Alpha},
  author={Foo, Anna},
  year={2020}
}

@inproceedings{bar2021beta,
  title={Beta},
  author={Bar, Bob},
  year={2021}
}
"""


def test_strips_undefined_single_cite(compiler):
    _write(compiler,
           "Body uses ~\\cite{nonexistent2099} only.\n",
           _BIB_FOO_BAR)
    n = compiler._prune_undefined_citations()
    assert n == 1
    out = (compiler.latex_dir / "main.tex").read_text()
    assert "\\cite" not in out


def test_keeps_defined_cite(compiler):
    src = "See ~\\cite{foo2020alpha} for details.\n"
    _write(compiler, src, _BIB_FOO_BAR)
    n = compiler._prune_undefined_citations()
    assert n == 0
    assert (compiler.latex_dir / "main.tex").read_text() == src


def test_partial_cite_keeps_valid_drops_invalid(compiler):
    _write(compiler,
           "See ~\\cite{foo2020alpha,ghost1999,bar2021beta}.\n",
           _BIB_FOO_BAR)
    n = compiler._prune_undefined_citations()
    assert n == 1
    out = (compiler.latex_dir / "main.tex").read_text()
    assert "\\cite{foo2020alpha,bar2021beta}" in out
    assert "ghost1999" not in out


def test_handles_natbib_optional_args(compiler):
    """\\citep[Sec~3]{key} and \\citet[p.~5][p.~10]{key} forms."""
    _write(compiler, (
        "First~\\citep[Sec~3]{ghost} word. "
        "Second~\\citet[p.~5][p.~10]{foo2020alpha} word.\n"
    ), _BIB_FOO_BAR)
    compiler._prune_undefined_citations()
    out = (compiler.latex_dir / "main.tex").read_text()
    # ghost stripped (entire \citep gone), foo kept with both args
    assert "ghost" not in out
    assert "\\citet[p.~5][p.~10]{foo2020alpha}" in out


def test_handles_biblatex_variants(compiler):
    _write(compiler, (
        "Auto~\\autocite{foo2020alpha}, paren~\\parencite{ghost}, "
        "text~\\textcite{bar2021beta}, foot~\\footcite{ghost2}.\n"
    ), _BIB_FOO_BAR)
    n = compiler._prune_undefined_citations()
    assert n == 2
    out = (compiler.latex_dir / "main.tex").read_text()
    assert "\\autocite{foo2020alpha}" in out
    assert "\\textcite{bar2021beta}" in out
    assert "ghost" not in out


def test_nocite_undefined_pruned(compiler):
    _write(compiler,
           "\\nocite{foo2020alpha,phantom}.\n",
           _BIB_FOO_BAR)
    compiler._prune_undefined_citations()
    out = (compiler.latex_dir / "main.tex").read_text()
    assert "\\nocite{foo2020alpha}" in out
    assert "phantom" not in out


def test_no_op_when_bib_missing(compiler):
    """No references.bib → nothing to compare against → leave file alone."""
    src = "Has ~\\cite{anything} citation.\n"
    (compiler.latex_dir / "main.tex").write_text(src)
    n = compiler._prune_undefined_citations()
    assert n == 0
    assert (compiler.latex_dir / "main.tex").read_text() == src


def test_no_op_when_main_missing(compiler):
    (compiler.latex_dir / "references.bib").write_text(_BIB_FOO_BAR)
    n = compiler._prune_undefined_citations()
    assert n == 0


def test_idempotent_second_run(compiler):
    _write(compiler,
           "Use ~\\cite{ghost1999} once.\n",
           _BIB_FOO_BAR)
    n1 = compiler._prune_undefined_citations()
    n2 = compiler._prune_undefined_citations()
    assert n1 == 1
    assert n2 == 0


def test_whitespace_around_keys(compiler):
    """\\cite{ key1 ,  key2 } should still match by trimmed keys."""
    _write(compiler, "See ~\\cite{ foo2020alpha , ghost ,bar2021beta }.\n",
           _BIB_FOO_BAR)
    compiler._prune_undefined_citations()
    out = (compiler.latex_dir / "main.tex").read_text()
    assert "ghost" not in out
    # Both valid keys preserved (order matters too)
    assert "foo2020alpha" in out
    assert "bar2021beta" in out


def test_bib_with_at_string_macros_ignored(compiler):
    """@string and @comment headers must not be picked up as cite keys."""
    bib = (
        "@string{IEEE = {IEEE Press}}\n\n"
        "@comment{this is a comment}\n\n"
        + _BIB_FOO_BAR
    )
    _write(compiler, "See ~\\cite{IEEE}.\n", bib)
    n = compiler._prune_undefined_citations()
    # @string is conventionally not a citable entry; we strip it.
    # If the user *intended* IEEE as a cite key they need a real entry.
    assert n == 1
    out = (compiler.latex_dir / "main.tex").read_text()
    assert "\\cite" not in out
