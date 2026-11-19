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

    def test_adjacent_single_line_entries_and_nested_tex_title(self, tmp_path):
        bib = tmp_path / "references.bib"
        bib.write_text("% @misc{ignored, title={A comment}}\n"
                       "@misc{wine, title={Wine}, year={1992}}\n"
                       "@article{skl, title={Machine Learning in {Python}}, year={2011}}\n")
        entries = parse_bib(str(bib))
        assert [(e["key"], e["fields"]["title"]) for e in entries] == [
            ("wine", "Wine"), ("skl", "Machine Learning in {Python}")]
        assert entries[0]["raw"].endswith("1992}}")

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
        fix_bib(str(bib), results)
        assert bib.read_text().count("% [NEEDS-CHECK") == 1
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




class TestSamePaperAuthorCheck:
    """The first-author veto must compare people, not encodings."""

    TITLE = "Scaling Laws for Sparse Mixture of Experts Routing"

    def _pick(self, bib_author, api_authors, year=2024):
        from ark.citation import Paper, _same_paper
        cand = Paper(title=self.TITLE, authors=api_authors, year=year)
        return _same_paper(self.TITLE, {"author": bib_author, "year": str(year)}, [cand])

    def test_dblp_homonym_number_is_not_the_surname(self):
        assert self._pick("Wang, Wei and Li, Bo", ["Wei Wang 0001", "Bo Li"]) is not None

    def test_bibtex_accents_match_unicode(self):
        assert self._pick('M{\\"u}ller, Thomas', ["Thomas Müller"]) is not None

    def test_braced_surname_matches(self):
        assert self._pick("{Vaswani}, Ashish", ["Ashish Vaswani"]) is not None

    def test_a_one_letter_misspelling_of_a_long_surname_resolves(self):
        assert self._pick("Vaswany, Ashish", ["Ashish Vaswani"]) is not None

    @pytest.mark.parametrize("bib, api", [("Li, Wei", "Wei Liu"), ("Chen, Yu", "Yu Cheng"),
                                          ("Huang, Lei", "Lei Hung"), ("Smith, John", "John Doe")])
    def test_a_different_person_is_still_vetoed(self, bib, api):
        assert self._pick(bib, [api]) is None


def test_bootstrap_reuses_complete_existing_paper_without_adding_placeholder(tmp_path, monkeypatch):
    from ark import citation
    import yaml

    bib = tmp_path / "references.bib"
    existing = ("@article{pedregosa2011, title={Scikit-learn: Machine Learning in {Python}}, "
                "author={Pedregosa, Fabian and Others}, year={2011}, journal={JMLR}}\n")
    bib.write_text(existing)
    literature = tmp_path / "literature.yaml"
    searched = []
    monkeypatch.setattr(citation, "_search_by_title", lambda title, fields, query: searched.append(title))
    kwargs = dict(titles=["Scikit-learn: Machine Learning in Python"], bib_path=str(bib),
                  literature_path=str(literature), authors=["Pedregosa"], years=[2011])
    for _ in range(2):
        assert not citation.bootstrap_citations(**kwargs).needs_check
    assert not searched
    assert bib.read_text() == existing
    refs = yaml.safe_load(literature.read_text())["references"]
    assert [(r["bibtex_key"], r["source"]) for r in refs] == [
        ("pedregosa2011", "existing bibliography")]
    kwargs["authors"] = ["Different"]
    assert citation.bootstrap_citations(**kwargs).needs_check == kwargs["titles"]
    assert searched


def test_bootstrap_repairs_legacy_claim_keeps_cited_key_and_reuses_receipt(tmp_path, monkeypatch):
    from ark import citation as c
    import yaml

    title = "Inference for the Generalization Error"
    doi = "10.1023/a:1024068626366"
    old_title = "mlr3inferr: Inference on the Generalization Error"
    old = f"@misc{{Fischer_2025, title={{{old_title}}}, author={{Sebastian Fischer}}, year={{2025}}}}"
    new = (f"@article{{Nadeau_2003, title={{{title}}}, author={{Claude Nadeau and Yoshua Bengio}}, "
           f"year={{2003}}, journal={{Machine Learning}}, doi={{{doi}}}}}")
    bib, lit = tmp_path / "r.bib", tmp_path / "lit.yaml"
    bib.write_text("% [ARK:source=crossref]\n" + old)
    lit.write_text(yaml.safe_dump({"references": [{"bibtex_key": "Fischer_2025", "title": old_title,
                                                   "context": f"Original result (DOI: {doi})."}]}))
    requests = []
    monkeypatch.setattr(c, "_fetch_bibtex_from_doi", lambda x: requests.append(x) or new)
    monkeypatch.setattr(c, "_search_by_title", lambda *args: pytest.fail("Unexpected title search"))
    for _ in range(2):
        resolved = c.bootstrap_citations([title], str(bib), str(lit), authors=["Nadeau"], years=[2003],
                                         dois=["https://doi.org/" + doi], contexts=["A rephrased claim."])
        assert resolved.found_keys == ["Fischer_2025"] and not resolved.needs_check
    assert requests == [doi]
    [entry] = c.parse_bib(str(bib))
    assert entry["fields"]["author"].startswith("Claude Nadeau") and entry["fields"]["doi"] == doi
    [ref] = yaml.safe_load(lit.read_text())["references"]
    assert ref["requested_title"] == title and ref["doi"] == doi


def test_claim_with_multiple_dois_cannot_rebind_existing_work(tmp_path, monkeypatch):
    from ark import citation as c
    import yaml

    title = "Estimation of Generalization Error"
    old_title = "Estimation of the Generalization Error"
    old = f"@article{{Original, title={{{old_title}}}, author={{Smith}}, year={{2015}}, journal={{Journal}}}}"
    doi = "10.1234/second"
    new = f"@article{{Second, title={{{title}}}, author={{Wang}}, year={{2020}}, doi={{{doi}}}}}"
    ctx = f"Compare the methods (DOI: 10.1234/first and DOI: {doi})."
    b, l = tmp_path / "refs.bib", tmp_path / "lit.yaml"
    b.write_text("% [ARK:source=crossref]\n" + old)
    l.write_text(yaml.safe_dump({"references": [{"bibtex_key": "Original", "title": old_title, "context": ctx}]}))
    monkeypatch.setattr(c, "_fetch_bibtex_from_doi", lambda x: new if x == doi else pytest.fail(x))
    out = c.bootstrap_citations([title], str(b), str(l), authors=["Wang"], years=[2020], contexts=[ctx], dois=[doi])
    assert out.found_keys == ["Second"]
    assert [(k["key"], k["fields"]["author"]) for k in c.parse_bib(str(b))] == [
        ("Original", "Smith"), ("Second", "Wang")]
    assert len(yaml.safe_load(l.read_text())["references"]) == 2


def test_bootstrap_validates_fetched_bib_and_keeps_each_key_with_its_own_claim(tmp_path, monkeypatch):
    import yaml
    from ark import citation as c

    titles = ["Paper whose metadata endpoint failed", "Paper with confirmed measurements"]
    papers = {title: c.Paper(title, [surname], 2010, source="dblp", confirmed_by=["dblp"])
              for title, surname in zip(titles, ("Red", "Blue"))}
    monkeypatch.setattr(c, "_search_by_title", lambda title, *args: papers[title])
    def official(paper):
        name = "Unrelated" if paper is papers[titles[0]] else "Blue"
        return f"@article{{{name}2010,title={{{paper.title}}},author={{{name}}},year={{2010}},journal={{J}}}}"
    monkeypatch.setattr(c, "fetch_bibtex", official)
    b, l = tmp_path / "refs.bib", tmp_path / "lit.yaml"
    for _ in range(2):
        result = c.bootstrap_citations(titles, str(b), str(l), authors=["Red", "Blue"], years=[2010, 2010],
                                       contexts=["earlier claim", "later claim"])
        assert result.needs_check == [titles[0]] and result.found_keys == ["Blue2010"]
    data = yaml.safe_load(l.read_text())
    assert len(c.parse_bib(str(b))) == 2
    assert [(r["title"], r["context"], r["bibtex_key"]) for r in data["references"]] == [
        (titles[1], "later claim", "Blue2010")]
    assert [r["title"] for r in data["needs_check"]] == [titles[0]]


def test_title_search_filters_homonyms_and_uses_original_title_for_arxiv(tmp_path, monkeypatch):
    from ark import citation as c

    title = "On Calibration of Modern Neural Networks"
    wrong = c.Paper(title, ["A. Smith"], 2025)
    right = c.Paper(title, ["Chuan Guo"], 2017, arxiv_id="1706.04599", venue="arXiv")
    for source in ("_search_dblp", "_search_crossref", "_search_semantic_scholar"):
        monkeypatch.setattr(c, source, lambda *args, **kwargs: [wrong])
    monkeypatch.setattr(c.time, "sleep", lambda _: None)
    def arxiv(query, max_results, title_only):
        assert title_only and query == "on calibration of modern"
        return [wrong, right]
    monkeypatch.setattr(c, "_search_arxiv", arxiv)
    assert c._search_by_title(title, {"author": "Guo", "year": 2017}) is right
