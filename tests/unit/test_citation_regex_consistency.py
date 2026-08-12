"""The citation-detection regex must be ONE definition, shared by every caller.

Regression for a two-step silent deletion of valid, API-verified references.
`cleanup_unused` (ark/citation.py) used a narrow r"\\cite[pt]?\\{...}" that could
not see `\\citep[e.g.,][]{key}` — natbib's standard form, used by every ACL /
ICLR / TMLR / MLSys paper. So:

  1. cleanup_unused judged the entry uncited and DELETED it from references.bib;
  2. the next compile_latex() ran _prune_undefined_citations, whose WIDER regex
     did see the command, found the key now undefined, and stripped the \\cite
     from the prose.

A real reference vanished from both files while each module looked correct in
isolation. The bug needed a realistic bib to reproduce (multi-line entries so
parse_bib works, >1 entry so prune's "don't nuke an empty bib" guard at
compiler.py stays off), which is why single-module unit tests never caught it.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ark.citation import (
    CITE_CMD_RE,
    cited_keys_in_dir,
    cited_keys_in_tex,
    cleanup_unused,
)
from ark.latex.compiler import CompilerMixin


# Multi-line so parse_bib() sees them; three entries so prune's empty-bib guard
# never fires and `stale2019unused` gives cleanup something legitimate to do.
BIB = """\
@inproceedings{vaswani2017,
  title={Attention Is All You Need},
  author={Vaswani, Ashish},
  year={2017}
}

@inproceedings{devlin2019bert,
  title={BERT: Pre-training of Deep Bidirectional Transformers},
  author={Devlin, Jacob},
  year={2019}
}

@article{stale2019unused,
  title={Nobody Cites This},
  author={Ghost, Anon},
  year={2019}
}
"""

# Every form the writer agent legitimately emits. Each cites `vaswani2017`.
CITE_FORMS = [
    r"\cite{vaswani2017}",
    r"\citep{vaswani2017}",
    r"\citet{vaswani2017}",
    r"\citep[e.g.,][]{vaswani2017}",       # the form that triggered the bug
    r"\citep[see][p.~5]{vaswani2017}",
    r"\citet[Section 3]{vaswani2017}",
    r"\parencite{vaswani2017}",            # biblatex
    r"\autocite{vaswani2017}",
    r"\textcite{vaswani2017}",
    r"\citeauthor{vaswani2017}",
    r"\citeyear{vaswani2017}",
    r"\nocite{vaswani2017}",
]


def _paper(tmp_path: Path, tex_body: str, bib: str = BIB) -> Path:
    latex_dir = tmp_path / "paper"
    latex_dir.mkdir(parents=True, exist_ok=True)
    (latex_dir / "references.bib").write_text(bib)
    (latex_dir / "main.tex").write_text(tex_body)
    return latex_dir


def _prune(latex_dir: Path) -> int:
    """Run the real _prune_undefined_citations against `latex_dir`."""
    stub = MagicMock(spec=CompilerMixin)
    stub.latex_dir = latex_dir
    stub.log = MagicMock()
    stub._CITE_CMD_RE = CompilerMixin._CITE_CMD_RE
    stub._BIB_KEY_RE = CompilerMixin._BIB_KEY_RE
    return CompilerMixin._prune_undefined_citations.__get__(stub)()


def _bib_keys(latex_dir: Path) -> set[str]:
    from ark.citation import parse_bib

    return {e["key"] for e in parse_bib(str(latex_dir / "references.bib"))}


# ── the invariant ────────────────────────────────────────────────────────────

def test_compiler_shares_the_one_regex():
    """Not "equivalent patterns" — the identical object, so they cannot drift."""
    assert CompilerMixin._CITE_CMD_RE is CITE_CMD_RE


@pytest.mark.parametrize("form", CITE_FORMS)
def test_both_sides_agree_a_key_is_cited(form):
    """cleanup_unused and prune must never disagree about a citation command."""
    assert "vaswani2017" in cited_keys_in_tex(form), (
        f"{form} not recognised as citing vaswani2017"
    )
    assert CITE_CMD_RE.search(form), f"{form} not matched by the shared regex"


# ── the actual failure, end to end ───────────────────────────────────────────

@pytest.mark.parametrize("form", CITE_FORMS)
def test_cited_reference_survives_a_full_iteration(tmp_path, form):
    """The two-step deletion: cleanup (end of review) then prune (next compile).

    A key cited in ANY supported form must still be in references.bib AND still
    be in the prose after both passes.
    """
    latex_dir = _paper(tmp_path, f"Transformers changed NLP {form}.\n")

    # step 1 — what _run_citation_verification does at its end
    removed = cleanup_unused(str(latex_dir / "references.bib"), str(latex_dir))
    assert "vaswani2017" not in removed, f"{form}: cleanup deleted a cited entry"

    # step 2 — what compile_latex does at its start, next round
    pruned = _prune(latex_dir)
    assert pruned == 0, f"{form}: prune stripped a valid citation"

    assert "vaswani2017" in _bib_keys(latex_dir)
    assert "vaswani2017" in (latex_dir / "main.tex").read_text()


# ── the cleanup must still do its job (no "fix" by disabling it) ─────────────

def test_genuinely_unused_entry_is_still_removed(tmp_path):
    latex_dir = _paper(
        tmp_path, r"Only \cite{devlin2019bert} and \citep[cf.][]{vaswani2017}." "\n"
    )
    removed = cleanup_unused(str(latex_dir / "references.bib"), str(latex_dir))
    assert removed == ["stale2019unused"]
    assert _bib_keys(latex_dir) == {"vaswani2017", "devlin2019bert"}


def test_hallucinated_key_is_still_pruned(tmp_path):
    latex_dir = _paper(
        tmp_path,
        "Real \\cite{devlin2019bert}. Invented \\citep[e.g.,][]{zhang2023fake}.\n",
    )
    assert _prune(latex_dir) == 1
    main = (latex_dir / "main.tex").read_text()
    assert "zhang2023fake" not in main
    assert "devlin2019bert" in main


def test_partially_valid_command_keeps_the_good_keys(tmp_path):
    latex_dir = _paper(
        tmp_path, r"\citep[e.g.,][]{vaswani2017,zhang2023fake,devlin2019bert}" "\n"
    )
    assert _prune(latex_dir) == 1
    main = (latex_dir / "main.tex").read_text()
    assert "vaswani2017" in main and "devlin2019bert" in main
    assert "zhang2023fake" not in main


# ── \nocite{*} is a wildcard, not a key ──────────────────────────────────────

def test_nocite_star_protects_the_whole_bib(tmp_path):
    """`\\nocite{*}` prints every entry — none of them is "unused"."""
    latex_dir = _paper(tmp_path, "Body text.\n\\nocite{*}\n")
    assert cleanup_unused(str(latex_dir / "references.bib"), str(latex_dir)) == []
    assert _bib_keys(latex_dir) == {"vaswani2017", "devlin2019bert", "stale2019unused"}


def test_nocite_star_is_not_stripped_as_an_undefined_key(tmp_path):
    latex_dir = _paper(tmp_path, "Body text.\n\\nocite{*}\n")
    assert _prune(latex_dir) == 0
    assert r"\nocite{*}" in (latex_dir / "main.tex").read_text()


# ── the other callers of the shared extractor ────────────────────────────────

def test_critical_citation_check_sees_natbib_optional_args(tmp_path):
    """_enforce_critical_citations used the narrow regex too: a MUST-CITE paper
    written as \\citep[e.g.,][]{key} was reported missing, burning a writer call
    to add a citation already in the text."""
    latex_dir = _paper(tmp_path, r"Prior work \citep[e.g.,][]{vaswani2017}." "\n")
    assert "vaswani2017" in cited_keys_in_dir(str(latex_dir))


def test_delivery_contract_counts_biblatex_citations():
    """A biblatex-only paper counted 0 cites, so the references_nonempty hard
    gate downgraded itself to "skipped" on exactly the papers that needed it."""
    tex = r"Claim \parencite{vaswani2017} and \autocite{devlin2019bert}." "\n"
    assert len(cited_keys_in_tex(tex)) == 2


# ── things that merely look like citations ───────────────────────────────────

@pytest.mark.parametrize("text", [
    r"\citation{foo}",          # bibtex .aux internal, not a document command
    r"excite{foo}",             # no leading backslash
    r"\site{foo}",
    "plain prose with cite in it",
])
def test_non_citation_text_is_not_treated_as_a_key(text):
    assert "foo" not in cited_keys_in_tex(text)
