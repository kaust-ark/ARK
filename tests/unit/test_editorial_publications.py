"""Count works with current original passages behind actual PDF body links."""
import hashlib
import json

import fitz

from ark import literature_evidence


def _trace(tmp_path, rows, report_text):
    paper = tmp_path / "paper"
    paper.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()
    entries, checked = [], {}
    for key, fields, url, options in rows:
        items = ", ".join(f"{name}={{{value}}}" for name, value in fields.items())
        entries.append(f"@{options.get('kind', 'article')}{{{key}, {items}}}")
        file = sources / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt")
        # The passage is free to *cite* other works. Only its own record URL identifies it.
        passage = options.get("passage", "Uses the method in https://doi.org/10.1111/journal.")
        if options.get("legacy"):
            file.write_text(f"Original abstract\n{fields['title']}\n{url}\n\n{passage}")
        else:
            file.write_text(json.dumps({"title": fields["title"], "url": url, "passage": passage}))
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        checked[key] = {"source": str(file.relative_to(tmp_path)), "status": options.get("status", "VERIFIED"),
                        "source_sha256": "stale" if options.get("stale") else digest}
    (paper / "references.bib").write_text("\n".join(entries))
    report = tmp_path / "report.md"
    report.write_text(report_text)
    with fitz.open() as doc:
        for heading in ("Experiments", "References", "Appendix"):
            doc.new_page().insert_text((60, 80), heading)
        dest = doc.page_xref(1)
        names = " ".join(f"(cite.{k}) [{dest} 0 R /XYZ 0 500 0]" for k in sorted(checked))
        doc.xref_set_key(doc.pdf_catalog(), "Names", f"<< /Dests << /Names [{names}] >> >>")
        for n, (key, _, _, options) in enumerate(rows):
            page = doc[2 if options.get("appendix") else 0]
            page.insert_link({"kind": fitz.LINK_NAMED, "from": fitz.Rect(70, 100+n*15, 100, 110+n*15),
                              "nameddest": "cite." + key})
        doc.save(paper / "main.pdf")
    return literature_evidence.body_source_trace(paper, report, checked)


def test_body_aliases_join_doi_preprint_title_and_authors(tmp_path):
    rows = [
        ("a", {"title": "First archive version", "eprint": "2410.12345v3"}, "https://arxiv.org/pdf/2410.12345v3.pdf", {}),
        ("b", {"title": "Final Paper on Inference", "author": "Ada Jones and Bo Wu",
               "doi": "10.1111/JOURNAL"}, "https://publisher.example/b", {}),
        # The published version bridges two groups already seen, using its own source URL.
        ("c", {"title": "Changed archive title", "url": "https://doi.org/10.1111/journal"},
         "https://arxiv.org/abs/2410.12345v2", {"legacy": True}),
        ("d", {"title": "{Final} Paper on Inference!", "author": "Jones, Ada and Wu, Bo",
               "doi": "10.48550/arXiv.2411.22345"}, "https://publisher.example/d", {}),
        ("e", {"title": "Another archive revision", "url": "https://arxiv.org/pdf/2411.22345v2.pdf"},
         "https://publisher.example/e", {}),
        ("abbrev", {"title": "Final Paper on Inference", "author": "A. Jones and Wu, B."},
         "https://publisher.example/shortbyline", {}),
        # The same title with the same first author and a different coauthor is a different work.
        ("f", {"title": "Final Paper on Inference", "author": "Ada Jones and Fei Wu"},
         "https://publisher.example/f", {}),
        # Legacy arXiv IDs have a subject prefix and a version-independent identity.
        ("g", {"title": "Legacy source", "doi": "10.48550/arXiv.hep-th/9901001"}, "https://example.org/g", {}),
        ("h", {"title": "Legacy revision", "eprint": "hep-th/9901001v2"}, "https://example.org/h", {}),
    ]
    trace = _trace(tmp_path, rows, "arXiv:2410.12345v4 and arXiv:hep-th/9901001v2.\n")
    assert trace["total"] == 9
    assert trace["verified_publications"] == 3
    assert trace["verified_works"] == 2


def test_body_aliases_require_live_primary_passages_and_body_links(tmp_path):
    first = {"title": "An exact paper title", "doi": "10.1111/journal", "author": "Ada Jones"}
    second = {"title": "A different paper title", "doi": "10.2222/second", "author": "Ada Jones"}
    rows = [
        ("one", first, "https://doi.org/10.1111/journal", {}),
        ("two", second, "https://doi.org/10.2222/second", {}),
    ]
    # Each of these would join the two publications by title/author and DOI if admitted.
    bridge = dict(first, doi=second["doi"])
    for key, options in (("stale", {"stale": True}), ("unchecked", {"status": "NEEDS-CHECK"}),
                         ("supplement", {"appendix": True}), ("data", {"kind": "dataset"}),
                         ("empty", {"passage": "  "})):
        rows.append((key, bridge, f"https://example.org/{key}", options))
    trace = _trace(tmp_path, rows, "doi:10.1111/journal\n")
    assert trace["verified_publications"] == 2
    assert trace["verified_works"] == 1
    assert "supplement" not in trace["body_citations"]
    for key in ("stale", "unchecked", "empty"):
        assert "source" not in trace["body_citations"][key]


def test_ambiguous_initials_do_not_join_two_known_authors(tmp_path):
    title = "An estimation procedure"
    rows = [
        ("jane", {"title": title, "author": "Jane Lee", "doi": "10.2222/jane"}, "https://example.org/jane", {}),
        ("john", {"title": title, "author": "Lee, John", "doi": "10.2222/john"}, "https://example.org/john", {}),
        ("initial", {"title": title, "author": "J. Lee"}, "https://doi.org/10.2222/jane", {}),
    ]
    trace = _trace(tmp_path, rows, "doi:10.2222/jane")
    assert trace["verified_publications"] == 2
    assert trace["verified_works"] == 1
