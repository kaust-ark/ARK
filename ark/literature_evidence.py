"""Recover verifiable records and passages when a bibliography lacks a DOI."""

import re
import urllib.error
from html import unescape
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ark import citation

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
