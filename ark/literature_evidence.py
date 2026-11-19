"""Recover verifiable records and passages when a bibliography lacks a DOI."""

import hashlib
import json
import re
import urllib.error
from html import unescape
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ark import citation
from ark.latex.utils import body_page_boundary


def body_source_trace(paper, report_path, checked):
    """Trace rendered body links to report identities and verified source files, without judging entailment."""
    import fitz

    entries = {entry["key"]: entry["fields"] for entry in citation.parse_bib(str(paper / "references.bib"))}
    pages = {}
    with fitz.open(paper / "main.pdf") as doc:
        boundary = body_page_boundary(doc)
        if boundary is None:
            return None  # We cannot distinguish body citations from references and appendix citations.
        end, bottoms = boundary
        for page in doc:
            if page.number > end or (page.number == end and not any(bottoms)):
                break
            for link in page.get_links():
                dest = link.get("nameddest") or ""
                target_page = link.get("page")
                if not dest.startswith("cite.") or not isinstance(target_page, int) or not end <= target_page < len(doc):
                    continue
                box = link["from"]
                if page.number == end and (box.y0 + box.y1) / 2 > bottoms[int(box.x0 >= page.rect.width / 2)]:
                    continue
                key = dest[5:]
                if key not in entries:  # biblatex includes the reference section in its destination.
                    key = re.sub(r"^\d+@", "", key)
                if key in entries:
                    pages.setdefault(key, set()).add(page.number + 1)
    if not pages:
        return None  # A valid venue may render citations without named hyperlink annotations.

    raw_lines = report_path.read_text().splitlines()
    normalized = [" " + citation._normalize_title(line) + " " for line in raw_lines]
    records = {}
    for key, body_pages in sorted(pages.items()):
        fields = entries[key]
        row = {"pages": sorted(body_pages)}
        evidence = checked.get(key, {})
        verified = evidence.get("status") in {"VERIFIED", "CORRECTED", "SINGLE_SOURCE"}
        source = (evidence.get("source") or "") if verified else ""
        if not verified:
            row["identity_verified"] = False
        ids = [fields.get("doi"), fields.get("url"), fields.get("eprint")]
        if re.fullmatch(r"sources/[0-9a-f]{24}\.txt", source):
            path = paper.parent / source
            if path.resolve() == path and path.is_file():
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() == evidence.get("source_sha256"):
                    row["source"] = source
                    # An excerpt can mention other studies. Only the record's own URL identifies this work.
                    text = content.decode(errors="replace")
                    if text.startswith("{"):
                        try:
                            parsed = json.loads(text)
                        except ValueError:
                            parsed = None
                        if isinstance(parsed, dict):
                            ids.append(parsed.get("url"))
                    else:
                        ids.extend(text.splitlines()[2:3])
        title = citation._normalize_title(fields.get("title", ""))
        identities = [doi for value in ids if (doi := citation._doi_id(value))]
        arxiv = re.search(r"(?:arxiv[:/]\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(?!\d)",
                          " ".join(str(value) for value in ids if value), re.I)
        if arxiv:
            identities.append(arxiv[1])
        elif re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", fields.get("eprint", "")):
            identities.append(fields["eprint"].split("v")[0])
        for identity in identities:
            pattern = re.compile(r"(?<!\w)" + re.escape(identity) + r"(?![\w/-]|\.[\w/-])", re.I)
            if line := next((i for i, raw in enumerate(raw_lines, 1) if pattern.search(raw)), None):
                row["report_line"] = line
                break
        if "report_line" not in row and len(title.split()) >= 4:
            if line := next((i for i, text in enumerate(normalized, 1) if f" {title} " in text), None):
                row["report_line"] = line
        records[key] = row
    ref_line = next((i for i, raw in enumerate(raw_lines, 1)
                     if re.fullmatch(r"#+\s*(?:references|bibliography|sources)\s*", raw, re.I)), None)
    matched = sum("report_line" in row for row in records.values())
    summary = f"{matched} of {len(records)} distinct body citations could be located by title or identifier in the verified Deep Research report."
    if matched < len(records):
        summary += " Inspect the survey for relevant comparisons and check claims against their original sources. Supplemental papers may legitimately come from outside the survey."
    # Keep the agent tool result bounded for a full-length survey. Sample both groups.
    samples = [key for flag in (True, False)
               for key in [k for k, v in records.items() if ("report_line" in v) == flag][:8]]
    return {"report": "auto_research/state/deep_research.md", "references_line": ref_line,
            "matched": matched, "total": len(records), "summary": summary,
            "body_citations": {key: records[key] for key in sorted(samples)}}

# Only fetch public PDFs from known research repositories. Other locations still
# contribute bibliographic identifiers and explicitly labelled indexed abstracts.
_REPOSITORIES = {
    "arxiv.org",
    "aclanthology.org",
    "proceedings.mlr.press",
    "jmlr.org",
    "www.jmlr.org",
    "openreview.net",
    "papers.nips.cc",
    "proceedings.neurips.cc",
    "www.ecva.net",
    "openaccess.thecvf.com",
}


def _public_pdf(url):
    parts = urlsplit(url or "")
    if (
        parts.scheme not in {"http", "https"}
        or parts.netloc.lower() not in _REPOSITORIES
        or not parts.path.startswith("/")
        or "\\" in parts.path
    ):
        return None
    return urlunsplit(("https", parts.hostname, parts.path, parts.query, ""))


class _RepositoryRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _public_pdf(newurl) is None:
            return None
        return super().redirect_request(req, fp, code, msg, headers, _public_pdf(newurl))


def _paper_excerpt(url):
    import fitz

    max_bytes = 12 * 1024 * 1024
    request = Request(url, headers={"User-Agent": "Idea2Paper-librarian/1.0"})
    try:
        with build_opener(_RepositoryRedirect()).open(request, timeout=12) as response:
            if int(response.headers.get("Content-Length") or 0) > max_bytes:
                return None
            body = response.read(max_bytes + 1)
        if len(body) > max_bytes or not body.startswith(b"%PDF-"):
            return None
        with fitz.open(stream=body, filetype="pdf") as doc:
            if not len(doc):
                return None
            return [doc[page].get_text(sort=True)[:9000] for page in range(min(16, len(doc)))]
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        return None


def crossref_record(fields):
    """Disambiguate short titles with the author and publication year."""
    year = citation._as_year(fields.get("year"))
    title = fields.get("title", "").replace("{", "").replace("}", "").strip()
    author = citation._first_author_surname(fields.get("author", "").split(" and "))
    if not (title and author and year):
        return None
    query = urlencode(
        {
            "query.title": title,
            "query.author": author,
            "rows": 5,
            "filter": f"from-pub-date:{year-1}-01-01,until-pub-date:{year+1}-12-31",
        }
    )
    data = citation._http_get_json("https://api.crossref.org/works?" + query)
    for item in ((data or {}).get("message") or {}).get("items", []):
        names = [
            str(a.get("given", "")) + " " + a["family"] for a in item.get("author", []) if a.get("family")
        ]
        date = (item.get("issued", {}).get("date-parts") or [[]])[0]
        doi = citation._doi_id(item.get("DOI"))
        if not (date and names and doi):
            continue
        hit = citation.Paper(title=(item.get("title") or [""])[0], authors=names, year=date[0], doi=doi)
        if citation.title_similarity(title, hit.title) >= 0.92 and citation._same_paper(title, fields, [hit]):
            record = {
                "title": hit.title,
                "authors": names,
                "year": hit.year,
                "url": "https://doi.org/" + doi,
                "index": "Crossref",
            }
            abstract = unescape(re.sub(r"<[^>]+>", " ", item.get("abstract") or "")).strip()
            if abstract:
                record.update(source="Publisher-deposited abstract (Crossref)", passage=abstract)
            return record
    return None


def resolve(entry):
    fields = entry["fields"]
    title = fields.get("title", "").replace("{", "").replace("}", "").strip()
    if not title or not fields.get("author") or not citation._as_year(fields.get("year")):
        return None
    query = urlencode(
        {
            "search": title,
            "per_page": 6,
            "select": "id,display_name,doi,publication_year,authorships,locations,abstract_inverted_index",
        }
    )
    data = citation._http_get_json("https://api.openalex.org/works?" + query)
    for item in (data or {}).get("results", []):
        authors = [
            a["author"]["display_name"]
            for a in item.get("authorships", [])
            if (a.get("author") or {}).get("display_name")
        ]
        candidate = citation.Paper(
            title=item.get("display_name") or "",
            year=item.get("publication_year"),
            authors=authors,
            doi=citation._doi_id(item.get("doi")) or None,
        )
        if (
            not authors
            or citation.title_similarity(title, candidate.title) < 0.92
            or citation._same_paper(title, fields, [candidate]) is None
        ):
            continue
        if not candidate.year:
            expected = fields["author"].split(" and ")
            if (
                len(expected) < 2
                or len(authors) < 2
                or citation.title_similarity(title, candidate.title) < 0.99
                or not citation._same_author(
                    citation._first_author_surname(expected[1:]), citation._first_author_surname(authors[1:])
                )
            ):
                continue
        record = {
            "title": candidate.title,
            "authors": authors,
            "year": candidate.year,
            "index": item["id"],
            "url": "https://doi.org/" + candidate.doi if candidate.doi else item["id"],
        }
        for location in item.get("locations") or []:
            url = _public_pdf(location.get("pdf_url"))
            if not url and (location.get("landing_page_url") or "").endswith(".pdf"):
                url = _public_pdf(location["landing_page_url"])
            if not url:
                continue
            pages = _paper_excerpt(url)
            if pages and sum(len(p.strip()) for p in pages) > 350:
                record.update(
                    source="Text extracted from the original paper",
                    url=url,
                    passage="\n\n".join(f"[Page {i}]\n{page}" for i, page in enumerate(pages, 1))[:80000],
                )
                return record
        index = item.get("abstract_inverted_index") or {}
        words = sorted((position, word) for word, positions in index.items() for position in positions)
        if words:
            record.update(source="Author abstract indexed by OpenAlex", passage=" ".join(w for _, w in words))
        return record
    return None
