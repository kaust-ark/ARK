"""Tests for ark.citation module.

Run:
    pytest tests/test_citation.py -v              # mock tests (no network)
    pytest tests/test_citation.py -m network -v   # network tests (hit real APIs)
"""

import pytest
from pathlib import Path

from ark.citation import (
    parse_bib, cleanup_unused, extract_search_queries, format_candidates_for_agent,
    parse_agent_selection, title_similarity, Paper, VerificationResult, fix_bib,
)


# ═══════════════════════════════════════════════════════════
#  Unit tests (no network)
# ═══════════════════════════════════════════════════════════

class TestParseBib:
    def test_basic(self, tmp_path):
        bib = tmp_path / "test.bib"
        bib.write_text(r"""
@inproceedings{vaswani2017attention,
  author = {Vaswani, Ashish and Shazeer, Noam},
  title = {Attention Is All You Need},
  booktitle = {NeurIPS},
  year = {2017},
}

@article{devlin2019bert,
  author = {Devlin, Jacob},
  title = {BERT: Pre-training of Deep Bidirectional Transformers},
  journal = {NAACL},
  year = {2019},
  volume = {1},
}
""")
        entries = parse_bib(str(bib))
        assert len(entries) == 2
        assert entries[0]["key"] == "vaswani2017attention"
        assert entries[0]["entry_type"] == "inproceedings"
        assert "Attention" in entries[0]["fields"]["title"]
        assert entries[1]["key"] == "devlin2019bert"
        assert entries[1]["fields"]["year"] == "2019"

    def test_empty_file(self, tmp_path):
        bib = tmp_path / "empty.bib"
        bib.write_text("% just a comment\n")
        assert parse_bib(str(bib)) == []

    def test_preserves_preceding_comments(self, tmp_path):
        bib = tmp_path / "tagged.bib"
        bib.write_text(r"""
% [ARK:source=dblp]
@inproceedings{foo2024bar,
  author = {Foo, Bar},
  title = {Some Paper},
  booktitle = {ICML},
  year = {2024},
}
""")
        entries = parse_bib(str(bib))
        assert len(entries) == 1
        assert "[ARK:source=dblp]" in entries[0]["preceding_comments"]


class TestCleanupUnused:
    def test_removes_uncited(self, tmp_path):
        bib = tmp_path / "references.bib"
        bib.write_text(r"""
@article{cited2024,
  author = {A},
  title = {Cited Paper},
  journal = {J},
  year = {2024},
}

@article{uncited2024,
  author = {B},
  title = {Uncited Paper},
  journal = {J},
  year = {2024},
}
""")
        tex = tmp_path / "main.tex"
        tex.write_text(r"\cite{cited2024} is important.")

        removed = cleanup_unused(str(bib), str(tmp_path))
        assert "uncited2024" in removed
        assert "cited2024" not in removed
        # Verify file was updated
        content = bib.read_text()
        assert "uncited2024" not in content
        assert "cited2024" in content

    def test_keeps_all_cited(self, tmp_path):
        bib = tmp_path / "references.bib"
        bib.write_text(r"""
@article{a2024,
  author = {A},
  title = {Paper A},
  journal = {J},
  year = {2024},
}
""")
        tex = tmp_path / "main.tex"
        tex.write_text(r"\cite{a2024}")
        assert cleanup_unused(str(bib), str(tmp_path)) == []

    def test_handles_multicite(self, tmp_path):
        bib = tmp_path / "references.bib"
        bib.write_text(r"""
@article{a2024,
  author = {A},
  title = {A},
  journal = {J},
  year = {2024},
}

@article{b2024,
  author = {B},
  title = {B},
  journal = {J},
  year = {2024},
}

@article{c2024,
  author = {C},
  title = {C},
  journal = {J},
  year = {2024},
}
""")
        tex = tmp_path / "main.tex"
        tex.write_text(r"\cite{a2024,b2024} but not c")
        removed = cleanup_unused(str(bib), str(tmp_path))
        assert "c2024" in removed
        assert "a2024" not in removed
        assert "b2024" not in removed


class TestExtractSearchQueries:
    def test_quoted_names(self):
        queries = extract_search_queries(
            'Missing comparison',
            'Should compare with "Deep Learning with Differential Privacy" and "Time-series Generative Adversarial Networks"'
        )
        assert any("Deep Learning with Differential Privacy" in q for q in queries)
        assert any("Time-series Generative Adversarial Networks" in q for q in queries)

    def test_author_year_pattern(self):
        queries = extract_search_queries(
            "Missing baselines",
            'PATE-GAN (Jordon et al. 2019, ICLR) and CSDI (Tashiro et al. 2021) are needed'
        )
        assert any("PATE-GAN" in q for q in queries)
        assert any("CSDI" in q for q in queries)

    def test_no_single_word_acronyms(self):
        """Should not extract garbage single-word queries like 'DP', 'GAN', 'CCS'."""
        queries = extract_search_queries(
            "Related work insufficient",
            "Need DP-SGD and GAN baselines from CCS and NeurIPS"
        )
        assert "DP" not in queries
        assert "GAN" not in queries
        assert "CCS" not in queries


class TestParseAgentSelection:
    def _make_candidates(self, n=5):
        return [Paper(title=f"Paper {i}", authors=[f"Author {i}"], year=2024) for i in range(1, n+1)]

    def test_selected_format(self):
        candidates = self._make_candidates()
        output = "SELECTED: 1, 3, 5\n[1] Good paper\n[3] Relevant\n[5] Important"
        result = parse_agent_selection(output, candidates)
        assert len(result) == 3
        assert result[0].title == "Paper 1"
        assert result[1].title == "Paper 3"
        assert result[2].title == "Paper 5"

    def test_bracket_format(self):
        candidates = self._make_candidates()
        output = "[2] is relevant\n[4] is also good"
        result = parse_agent_selection(output, candidates)
        assert len(result) == 2

    def test_out_of_range(self):
        candidates = self._make_candidates(3)
        output = "SELECTED: 1, 99"
        result = parse_agent_selection(output, candidates)
        assert len(result) == 1

    def test_empty_selection(self):
        candidates = self._make_candidates()
        output = "No papers seem relevant."
        result = parse_agent_selection(output, candidates)
        assert len(result) == 0


class TestTitleSimilarity:
    def test_exact(self):
        assert title_similarity("Attention Is All You Need", "Attention Is All You Need") == 1.0

    def test_minor_diff(self):
        s = title_similarity("Attention Is All You Need", "attention is all you need")
        assert s >= 0.85

    def test_different(self):
        s = title_similarity("Attention Is All You Need", "BERT Pre-training of Transformers")
        assert s < 0.5


class TestFixBib:
    def test_tags_needs_check(self, tmp_path):
        bib = tmp_path / "references.bib"
        raw = r"""@article{fake2099,
  author = {Nobody},
  title = {Fake Paper},
  journal = {Fake},
  year = {2099},
}"""
        bib.write_text(raw)
        results = [VerificationResult(
            status="NEEDS-CHECK", entry_key="fake2099",
            original_bibtex=raw.strip(), details="Not found",
        )]
        fix_bib(str(bib), results)
        content = bib.read_text()
        assert "NEEDS-CHECK" in content

    def test_replaces_corrected(self, tmp_path):
        bib = tmp_path / "references.bib"
        original = r"""@article{old2021,
  author = {Old},
  title = {Old Title},
  journal = {arXiv},
  year = {2021},
}"""
        corrected = r"""@inproceedings{old2022,
  author = {Old},
  title = {Old Title},
  booktitle = {ICLR},
  year = {2022},
}"""
        bib.write_text(original)
        results = [VerificationResult(
            status="CORRECTED", entry_key="old2021",
            original_bibtex=original.strip(),
            corrected_bibtex=corrected,
            details="Upgraded to published version",
        )]
        fix_bib(str(bib), results)
        content = bib.read_text()
        assert "ICLR" in content
        assert "[ARK:source=" in content


class TestFormatCandidates:
    def test_numbered(self):
        papers = [
            Paper(title="Paper A", authors=["Auth1"], year=2024, venue="ICML",
                  confirmed_by=["dblp", "crossref"], abstract="This is about X."),
            Paper(title="Paper B", authors=["Auth2"], year=2023, venue="NeurIPS"),
        ]
        text = format_candidates_for_agent(papers)
        assert "[1]" in text
        assert "[2]" in text
        assert "Paper A" in text
        assert "This is about X." in text

    def test_empty(self):
        text = format_candidates_for_agent([])
        assert "No papers found" in text


# ═══════════════════════════════════════════════════════════
#  Network tests (hit real APIs)
# ═══════════════════════════════════════════════════════════

@pytest.mark.network
class TestNetworkSearch:
    def test_search_and_fetch(self):
        from ark.citation import search_papers, fetch_bibtex
        papers = search_papers("Attention Is All You Need", max_results=5)
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
