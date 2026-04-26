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
        from ark.citation import search_papers, fetch_bibtex
        papers = search_papers("Vaswani Attention Is All You Need", max_results=5)
        assert len(papers) > 0
        # Should find the Vaswani paper
        match = [p for p in papers if "attention" in p.title.lower() and p.year == 2017]
        assert len(match) > 0

        # Fetch BibTeX
        bib = fetch_bibtex(match[0])
        assert bib is not None
        assert "@" in bib
        assert "Vaswani" in bib or "vaswani" in bib

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
