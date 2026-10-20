"""Network tests for ark.citation module.

Run:
    pytest tests/integration/test_citation_network.py -v
"""

import pytest
from pathlib import Path

# ═══════════════════════════════════════════════════════════
#  Network tests (hit real APIs)
# ═══════════════════════════════════════════════════════════

@pytest.mark.network
class TestNetworkSearch:
    def test_search_and_fetch(self):
        """Find a specific known paper among live results, then fetch its BibTeX.

        Identification goes through ``_same_paper`` rather than through the top
        hit, because search RANKING is not a stable thing to assert on. This
        test previously looked for the 2017 "Attention Is All You Need" in the
        first five hits and rotted: DBLP now returns a crowd of later papers
        reusing that exact title, and the original falls past the twentieth
        result. That drift is also why the production path stopped trusting
        rank — so exercise the same discriminators here.
        """
        from ark.citation import search_papers, fetch_bibtex, _same_paper
        title = "Deep Residual Learning for Image Recognition"
        papers = search_papers(title, max_results=10)
        assert len(papers) > 0
        # Real records, not empty shells — the sanity check that catches a
        # parser change or a silently-failing HTTP layer.
        assert all(p.title for p in papers)

        match = _same_paper(title, {"title": title, "year": "2016",
                                    "author": "He, Kaiming"}, papers)
        assert match is not None, [(p.year, p.title) for p in papers]

        bib = fetch_bibtex(match)
        assert bib is not None
        assert bib.lstrip().startswith("@")
        assert "he" in bib.lower()

    def test_verify_real_vs_fake(self, tmp_path):
        from ark.citation import verify_bib
        bib = tmp_path / "references.bib"
        bib.write_text(r"""
@inproceedings{vaswani2017attention,
  author = {Vaswani, Ashish},
  title = {Attention Is All You Need},
  booktitle = {NeurIPS},
  year = {2017},
}

@article{fakepaper2099xyz,
  author = {Nobody, John Q.},
  title = {A Completely Made Up Paper That Does Not Exist Anywhere},
  journal = {Fake Journal of Nonexistence},
  year = {2099},
}
""")
        results = verify_bib(str(bib))
        assert len(results) == 2
        real = [r for r in results if r.entry_key == "vaswani2017attention"][0]
        fake = [r for r in results if r.entry_key == "fakepaper2099xyz"][0]
        assert real.status in ("VERIFIED", "SINGLE_SOURCE", "CORRECTED")
        assert fake.status == "NEEDS-CHECK"
