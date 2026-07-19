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


def _paper(title):
    p = MagicMock()
    p.title = title
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
