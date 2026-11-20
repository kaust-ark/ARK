"""Recover verifiable records and passages when a bibliography lacks a DOI."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import re
import unicodedata
import urllib.error
from html import unescape
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ark import citation
from ark.latex.utils import body_page_boundary


def source_leads(report, limit=10):
    """Locate explicit paper identities in the survey. Registration still requires a primary record."""
    identifiers, titles = {}, {}
    in_code = False
    for number, line in enumerate(report.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        if in_code:
            continue
        # Opaque grounding redirects can each occupy thousands of characters.
        text = re.sub(r"https?://(?:vertexaisearch\.cloud\.google\.com|www\.google\.com)/[^\s)]*", "", line)
        section = re.search(r'(?:titled|entitled|published|paper|publication|study|conference|\(\d{4}\))', text, re.I)
        quoted = [m[1].strip(" .,") for m in re.finditer(r'["“]([^"”\n]{12,240})["”]', text)]
        quoted = [title for title in quoted if len(title.split()) >= 3]
        matches = re.finditer(r"(?<!\w)(10\.\d{4,9}/[-A-Z0-9._;()/+:]+|arxiv(?:\s*:\s*|\.org/(?:abs|pdf)/)\d{4}\.\d{4,5}(?:v\d+)?)", text, re.I)
        line_ids = set()
        for match in matches:
            # Some legacy DOI suffixes contain angle brackets. Lookup by title
            # when the resolver cannot represent the entire identifier.
            if text[match.end():].startswith(("<", "&lt;", "%3C", "%3c")):
                continue
            query = match[0].rstrip(".,;:")
            while query.endswith(")") and query.count(")") > query.count("("):
                query = query[:-1]
            if query.lower().startswith("arxiv"):
                aid = re.search(r"\d{4}\.\d{4,5}", query)[0]
                query = "10.48550/arXiv." + aid
            line_ids.add(query.lower())
            identifiers.setdefault(query.lower(), {"query": query, "report_line": number})
        if len(line_ids) == 1 and len(quoted) == 1:
            identifiers[next(iter(line_ids))].setdefault("reported_title", quoted[0])
        if section:
            for title in quoted:
                titles.setdefault(title.casefold(), {"query": title, "report_line": number})
    named = {r["reported_title"].casefold() for r in identifiers.values() if "reported_title" in r}
    return [*identifiers.values(), *(v for k, v in titles.items() if k not in named)][:limit]


def doi_original(doi):
    """Read the DOI deposit, including DataCite deposits for arXiv preprints."""
    if not citation._doi_id(doi):
        return None
    raw = citation._http_get("https://doi.org/" + doi,
                             headers={"Accept": "application/vnd.citationstyles.csl+json"}, timeout=5)
    try:
        meta = json.loads(raw or "null")
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict) or citation._doi_id(meta.get("DOI")) != doi.lower():
        return None
    title, abstract = meta.get("title"), meta.get("abstract")
    if not isinstance(title, str) or not isinstance(abstract, str):
        return None
    passage = " ".join(unescape(re.sub(r"<[^>]+>", " ", abstract)).split())
    if title.strip() and passage:
        return {"title": title.strip(), "source": "Deposited author abstract (DOI registry)",
                "url": "https://doi.org/" + doi, "passage": passage[:24000]}
    return None


def survey_originals(leads, cache):
    """Resolve a bounded set of survey DOIs. Only store matching original records."""
    candidates = list(dict.fromkeys(doi for lead in leads if (doi := citation._doi_id(lead["query"]))))[:6]
    missing = [doi for doi in candidates if doi not in cache]
    if missing:
        with ThreadPoolExecutor(max_workers=3) as pool:
            cache.update((doi, record) for doi, record in zip(missing, pool.map(doi_original, missing)) if record)
    return [{**lead, "official_title": record["title"], "abstract_excerpt": record["passage"][:480]}
            if (record := cache.get(citation._doi_id(lead["query"]))) else lead for lead in leads]


def _unused_source_leads(raw_lines, normalized, entries, body_keys, checked, originals):
    """Surface uncited survey leads, including works the author already considered for the bibliography."""
    cited_dois = {citation._doi_id(entries[key].get("doi")) for key in body_keys} - {""}
    cited_titles = {citation._normalize_title(entries[key].get("title", "")) for key in body_keys} - {""}
    candidates = []
    for lead in source_leads("\n".join(raw_lines)):
        doi = citation._doi_id(lead["query"])
        record = originals.get(doi, {}) if doi else {}
        title = record.get("title") or lead.get("reported_title") or ("" if doi else lead["query"])
        if (doi and doi in cited_dois) or (title and citation._normalize_title(title) in cited_titles):
            continue
        candidate = dict(lead)
        if record:
            candidate.update(official_title=record["title"], abstract_excerpt=record["passage"][:360])
        candidates.append((citation._normalize_title(title), doi, candidate))

    # A report can mention a work without quoting its title or including a DOI.
    # Keep a full-title match as a lookup lead, including failed identity checks.
    for key, fields in entries.items():
        title = fields.get("title", "")
        name = citation._normalize_title(title)
        doi = citation._doi_id(fields.get("doi"))
        if key in body_keys or name in cited_titles or (doi and doi in cited_dois) or len(name.split()) < 4:
            continue
        line = next((i for i, words in enumerate(normalized, 1) if f" {name} " in words), None)
        if not line:
            continue
        candidate = next((item for t, d, item in candidates
                          if (doi == d if doi and d else name == t)), None)
        if candidate is None:
            candidate = {"query": doi or title, "report_line": line}
            candidates.append((name, doi, candidate))
        candidate.update(bibliography_key=key, identity_status=checked.get(key, {}).get("status", "unchecked"))
    leads = (item for _, _, item in candidates)
    return sorted(leads, key=lambda item: ("bibliography_key" not in item, "official_title" not in item))[:4]


def body_source_trace(paper, report_path, checked, *, originals=None):
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
    verified_works = {citation._doi_id(entries[key].get("doi")) or citation._normalize_title(entries[key].get("title", ""))
                      for key, row in records.items() if row.get("source") and row.get("report_line")}
    summary = f"{matched} of {len(records)} distinct body citations could be located by title or identifier in the verified Deep Research report."
    if matched < len(records):
        summary += " Inspect the survey for relevant comparisons and check claims against their original sources. Supplemental papers may legitimately come from outside the survey."
    # Keep the agent tool result bounded for a full-length survey. Sample both groups.
    samples = [key for flag in (True, False)
               for key in [k for k, v in records.items() if ("report_line" in v) == flag][:8]]
    unused = _unused_source_leads(raw_lines, normalized, entries, pages, checked, originals or {})
    if unused:
        summary += (" Inspect unused leads for the origin of the method, its assumptions and the closest comparison. "
                    "Verify the original before citing a work at the claim it supports.")
    return {"report": "auto_research/state/deep_research.md", "references_line": ref_line,
            "matched": matched, "verified_works": len(verified_works - {""}), "total": len(records), "summary": summary,
            "body_citations": {key: records[key] for key in sorted(samples)},
            "unused_leads": unused}

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


class _SpringerRedirect(HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        # Springer Link may do a cookie check through its own identity provider.
        article = parts.netloc == "link.springer.com" and parts.path.startswith("/article/")
        login_check = parts.netloc == "idp.springer.com" and parts.path in ("/authorize", "/transit")
        if parts.scheme != "https" or not (article or login_check):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _SpringerAbstract(HTMLParser):
    def __init__(self):
        super().__init__()
        self.head = False
        self.metadata = {"citation_doi": [], "citation_title": [], "robots": []}
        self.section = ""
        self.content_depth = 0
        self.paragraph = None
        self.paragraphs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "head":
            self.head = True
        elif tag == "meta" and self.head and attrs.get("name") in self.metadata:
            self.metadata[attrs["name"]].append(attrs.get("content") or "")
        elif tag == "section":
            section_id = attrs.get("aria-labelledby") or ""
            self.section = section_id if attrs.get("data-title") == "Abstract" and re.fullmatch(r"Abs\d+", section_id) else ""
            self.content_depth = 0
            self.paragraph = None
        elif tag == "div" and self.section and (self.content_depth or attrs.get("id") == self.section + "-content"):
            self.content_depth += 1
        elif tag == "p" and self.content_depth:
            self.paragraph = []
        elif tag in ("script", "style") and self.paragraph is not None:
            self.paragraph = None

    def handle_data(self, data):
        if self.paragraph is not None:
            self.paragraph.append(data)

    def handle_endtag(self, tag):
        if tag == "head":
            self.head = False
        elif tag == "p" and self.paragraph is not None:
            self.paragraphs.append(" ".join("".join(self.paragraph).split()))
            self.paragraph = None
        elif tag == "div" and self.content_depth:
            self.content_depth -= 1
        elif tag == "section":
            self.section, self.content_depth, self.paragraph = "", 0, None


def springer_abstract(doi, title):
    """Read the publisher's article abstract only when its exact DOI and title agree."""
    doi = citation._doi_id(doi)
    if not re.fullmatch(r"10\.(?:1007|1023)/[a-z0-9][a-z0-9.():_-]*", doi) or not title:
        return None
    url = "https://link.springer.com/article/" + doi
    request = Request(url, headers={"User-Agent": "Idea2Paper-librarian/1.0", "Accept": "text/html"})
    limit = 2_000_000
    try:
        with build_opener(_SpringerRedirect()).open(request, timeout=8) as response:
            final = urlsplit(response.geturl())
            length = int(response.headers.get("Content-Length") or 0)
            if (response.status != 200 or final.scheme != "https" or final.netloc != "link.springer.com"
                    or unquote(final.path).lower() != "/article/" + doi
                    or response.headers.get("Content-Type", "").split(";")[0].strip().lower() != "text/html"
                    or not 0 <= length <= limit):
                return None
            robots = response.headers.get("X-Robots-Tag", "")
            body = response.read(limit + 1)
        if len(body) > limit or (length and len(body) != length):
            return None
        page = _SpringerAbstract()
        page.feed(body.decode("utf-8"))
        page.close()
    except (OSError, ValueError, HTTPException):
        return None
    meta = page.metadata
    titles = {t.strip() for t in meta["citation_title"]}
    if (re.search(r"\b(?:noindex|nosnippet|none)\b", " ".join([robots, *meta["robots"]]), re.I)
            or {citation._doi_id(v) for v in meta["citation_doi"]} != {doi} or len(titles) != 1):
        return None
    printed = titles.pop()
    passage = "\n\n".join(p for p in page.paragraphs if p)
    if citation._normalize_title(printed) == citation._normalize_title(title) and 80 <= len(passage) <= 24000:
        return {"title": printed, "url": url, "source": "Published author abstract (Springer Link)", "passage": passage}
    return None


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
            pages = []
            for page in doc.pages(stop=min(16, len(doc))):
                # Line-by-line layout sorting interleaves two-column prose with
                # neighboring figures. Keep each original text block together.
                blocks = page.get_text("blocks", sort=True)
                pages.append("\n\n".join(block[4].strip() for block in blocks if block[6] == 0)[:9000])
            return pages
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        return None


def repository_passage(doi, title):
    """Read the primary PDF for a verified DOI and confirm its title on the rendered paper."""
    doi = citation._doi_id(doi)
    if not doi or not title:
        return None
    urls = []
    arxiv_id = r"(?:[a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?"
    acl_id = r"(?:[A-Z]\d{2}-\d{3,5}|\d{4}\.[a-z0-9-]+\.\d+)"
    if doi.startswith("10.48550/arxiv.") and re.fullmatch(arxiv_id, doi.removeprefix("10.48550/arxiv."), re.I):
        urls.append("https://arxiv.org/pdf/" + doi.removeprefix("10.48550/arxiv."))
    elif doi.startswith("10.18653/v1/") and re.fullmatch(acl_id, doi.removeprefix("10.18653/v1/"), re.I):
        ident = doi.removeprefix("10.18653/v1/")
        urls.append("https://aclanthology.org/" + (ident.capitalize() if ident[0].isalpha() else ident) + ".pdf")
    else:
        record = citation._http_get_json("https://api.semanticscholar.org/graph/v1/paper/DOI:" +
                                         quote(doi, safe="/") + "?fields=externalIds,title,openAccessPdf", 4) or {}
        ids = record.get("externalIds") or {}
        if citation._doi_id(ids.get("DOI")) != doi or citation.title_similarity(title, record.get("title") or "") < .92:
            return None
        if re.fullmatch(acl_id, str(ids.get("ACL", ""))):
            urls.append("https://aclanthology.org/" + ids["ACL"] + ".pdf")
        if re.fullmatch(arxiv_id, str(ids.get("ArXiv", "")), re.I):
            urls.append("https://arxiv.org/pdf/" + ids["ArXiv"])
        if url := _public_pdf((record.get("openAccessPdf") or {}).get("url")):
            urls.append(url)
    def printed(text):
        return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())
    expected = printed(title)
    for url in dict.fromkeys(urls):
        pages = _paper_excerpt(url)
        if pages and len(expected) >= 6 and expected in printed(" ".join(pages[:2])):
            passage = "\n\n".join(f"[Page {i}]\n{page}" for i, page in enumerate(pages, 1))[:80000]
            if len(passage) > 350:
                return {"title":title, "url":url, "source":"Text extracted from the original paper", "passage":passage}
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
