"""Closed-loop handling of NEEDS-CHECK bib entries.

An LLM sometimes writes references.bib from memory. verify_bib marks what
DBLP/CrossRef can't confirm as NEEDS-CHECK — but marking alone still ships
the guess. The loop closure: replace_unverified_entries retries against the
FULL API set and swaps in real BibTeX on a confident title match;
remove_entries drops what nothing can confirm (caller then prunes \cite).
"""

from unittest.mock import patch, MagicMock

from ark.citation import (
    VerificationResult,
    replace_unverified_entries,
    remove_entries,
)

_ENTRY = """@article{sola1997importance,
  title = {Importance of input data normalization for the application of neural networks to complex industrial problems},
  author = {Sola, J. and Sevilla, J.},
  year = {1997}
}"""

_MARKER = "% [NEEDS-CHECK: citation not verified]\n"


def _paper(title, year=1997, authors=("J. Sola", "J. Sevilla")):
    """An API hit. Year and authors default to the entry's own, because a real
    DBLP/CrossRef record always carries them and the match now uses them to
    tell a same-titled DIFFERENT paper from the one actually cited."""
    p = MagicMock()
    p.title = title
    p.year = year
    p.authors = list(authors)
    return p


def _needs_check(key=_ENTRY.split("{", 1)[1].split(",")[0], raw=_ENTRY):
    return VerificationResult(status="NEEDS-CHECK", entry_key=key,
                              original_bibtex=raw)


def test_confident_match_replaces_entry_rekeyed(tmp_path):
    bib = tmp_path / "references.bib"
    bib.write_text(_MARKER + _ENTRY + "\n")
    real = ("@article{Sola1997ImportanceOI,\n"
            "  title = {Importance of input data normalization for the application of neural networks to complex industrial problems},\n"
            "  journal = {IEEE Transactions on Nuclear Science},\n  year = {1997}\n}")
    with patch("ark.citation.search_papers",
               return_value=[_paper("Importance of input data normalization for the application of neural networks to complex industrial problems")]), \
         patch("ark.citation.fetch_bibtex", return_value=real):
        replaced, unresolved = replace_unverified_entries(str(bib), [_needs_check()])

    assert replaced == ["sola1997importance"] and unresolved == []
    text = bib.read_text()
    # re-keyed to the original cite key, tagged as API-verified, marker gone
    assert "@article{sola1997importance," in text
    assert "[ARK:source=verified-replace]" in text
    assert "NEEDS-CHECK" not in text
    assert "IEEE Transactions on Nuclear Science" in text


def test_no_confident_match_leaves_entry_untouched(tmp_path):
    bib = tmp_path / "references.bib"
    bib.write_text(_MARKER + _ENTRY + "\n")
    with patch("ark.citation.search_papers",
               return_value=[_paper("A completely different paper about databases")]):
        replaced, unresolved = replace_unverified_entries(str(bib), [_needs_check()])
    assert replaced == [] and unresolved == ["sola1997importance"]
    assert bib.read_text() == _MARKER + _ENTRY + "\n"  # untouched


def test_remove_entries_drops_entry_and_marker(tmp_path):
    bib = tmp_path / "references.bib"
    # NB: multi-line — parse_bib's entry regex requires a "\n}" terminator
    keep = "@book{keepme2020,\n  title = {Keep},\n  year = {2020}\n}"
    bib.write_text(keep + "\n\n" + _MARKER + _ENTRY + "\n")
    removed = remove_entries(str(bib), ["sola1997importance"])
    assert removed == ["sola1997importance"]
    text = bib.read_text()
    assert "sola1997importance" not in text
    assert "NEEDS-CHECK" not in text
    assert "keepme2020" in text  # untouched sibling survives


# ── Same title is not the same paper ─────────────────────────────────────────
# Popular titles get reused. DBLP's live results for "Attention Is All You
# Need" are dominated by recent unrelated papers reusing the phrase, with the
# 2017 original nowhere in the first ten hits — so picking the first
# title-similar result replaces a correct reference with a confidently wrong
# one. These pin the discriminators the entry already carries.

def test_same_title_different_year_is_not_accepted(tmp_path):
    bib = tmp_path / "references.bib"
    bib.write_text(_MARKER + _ENTRY + "\n")
    impostor = _paper(  # exact title, but published 27 years later by others
        "Importance of input data normalization for the application of "
        "neural networks to complex industrial problems",
        year=2024, authors=("Nobody, Q.",))
    with patch("ark.citation.search_papers", return_value=[impostor]):
        replaced, unresolved = replace_unverified_entries(str(bib), [_needs_check()])
    assert replaced == [] and unresolved == ["sola1997importance"]
    assert bib.read_text() == _MARKER + _ENTRY + "\n"   # left for a human


def test_same_title_different_author_is_not_accepted(tmp_path):
    bib = tmp_path / "references.bib"
    bib.write_text(_MARKER + _ENTRY + "\n")
    impostor = _paper(
        "Importance of input data normalization for the application of "
        "neural networks to complex industrial problems",
        year=1997, authors=("Someone, Else",))
    with patch("ark.citation.search_papers", return_value=[impostor]):
        replaced, _ = replace_unverified_entries(str(bib), [_needs_check()])
    assert replaced == []


def test_the_real_paper_is_taken_even_when_impostors_rank_first(tmp_path):
    """Ranking is the search engine's business, identity is ours."""
    bib = tmp_path / "references.bib"
    bib.write_text(_MARKER + _ENTRY + "\n")
    title = ("Importance of input data normalization for the application of "
             "neural networks to complex industrial problems")
    hits = [_paper(title, year=2024, authors=("Nobody, Q.",)),
            _paper(title, year=2023, authors=("Other, P.",)),
            _paper(title, year=1997, authors=("Sola, J.", "Sevilla, J."))]
    with patch("ark.citation.search_papers", return_value=hits), \
         patch("ark.citation.fetch_bibtex",
               return_value="@article{x1997,\n  title = {Importance},\n"
                            "  author = {Sola, J.},\n  year = {1997}\n}"):
        replaced, unresolved = replace_unverified_entries(str(bib), [_needs_check()])
    assert replaced == ["sola1997importance"] and unresolved == []
    assert "sola1997importance" in bib.read_text()      # re-keyed, \cite survives


def test_a_source_that_omits_year_and_authors_is_still_usable(tmp_path):
    """Missing evidence is not counter-evidence — arXiv and S2 records are
    routinely sparse, and this second pass exists to reach them."""
    bib = tmp_path / "references.bib"
    bib.write_text(_MARKER + _ENTRY + "\n")
    sparse = _paper(
        "Importance of input data normalization for the application of "
        "neural networks to complex industrial problems",
        year=None, authors=())
    with patch("ark.citation.search_papers", return_value=[sparse]), \
         patch("ark.citation.fetch_bibtex",
               return_value="@article{x,\n  title = {Importance}\n}"):
        replaced, _ = replace_unverified_entries(str(bib), [_needs_check()])
    assert replaced == ["sola1997importance"]
