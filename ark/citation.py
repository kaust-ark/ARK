"""ARK Citation Module — API-first citation search, retrieval, and verification.

Design: LLM never touches references.bib. All BibTeX entries come from
DBLP / CrossRef official APIs. LLM only selects papers from a candidate list.

Search cascade: DBLP → CrossRef → arXiv → Semantic Scholar
BibTeX source:  DBLP rec/{key}.bib  or  DOI content-negotiation
Verification:   every iteration, dual-source cross-confirmation
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════

@dataclass
class Paper:
    title: str
    authors: list
    year: int
    venue: str = ""
    doi: Optional[str] = None
    dblp_key: Optional[str] = None
    arxiv_id: Optional[str] = None
    pages: Optional[str] = None
    volume: Optional[str] = None
    abstract: Optional[str] = None
    citation_count: Optional[int] = None
    bibtex: Optional[str] = None
    source: str = ""                    # "dblp" / "crossref" / "arxiv" / "s2"
    confirmed_by: list = field(default_factory=list)


@dataclass
class VerificationResult:
    status: str          # VERIFIED / CORRECTED / NEEDS-CHECK / SINGLE_SOURCE
    entry_key: str
    original_bibtex: str
    corrected_bibtex: Optional[str] = None
    details: str = ""


# ═══════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════

_CROSSREF_MAILTO = "jihao.xin@kaust.edu.sa"
_REQUEST_TIMEOUT = 15  # seconds
_DBLP_DELAY = 0.3      # polite delay between DBLP requests
_CROSSREF_DELAY = 0.2
_ARK_SOURCE_TAG = "[ARK:source="   # tag in bib comments to mark our entries
_SIMILARITY_THRESHOLD = 0.82


# ═══════════════════════════════════════════════════════════
#  HTTP helper
# ═══════════════════════════════════════════════════════════

def _http_get(url: str, headers: dict | None = None, timeout: int = _REQUEST_TIMEOUT) -> str | None:
    """Simple HTTP GET, returns response body or None on error."""
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", "ARK-Research/0.1 (mailto:jihao.xin@kaust.edu.sa)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return None


def _http_get_json(url: str, timeout: int = _REQUEST_TIMEOUT) -> dict | None:
    body = _http_get(url, timeout=timeout)
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# ═══════════════════════════════════════════════════════════
#  Title / author matching
# ═══════════════════════════════════════════════════════════

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/extra whitespace for comparison."""
    t = re.sub(r"[^\w\s]", " ", title.lower())
    return " ".join(t.split())


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def _first_author_surname(authors: list) -> str:
    """Extract first author's surname (last token) in lowercase."""
    if not authors:
        return ""
    name = authors[0] if isinstance(authors[0], str) else str(authors[0])
    parts = name.replace(",", " ").split()
    return parts[-1].lower() if parts else ""


# ═══════════════════════════════════════════════════════════
#  DBLP search + BibTeX fetch
# ═══════════════════════════════════════════════════════════

def _search_dblp(query: str, max_results: int = 10) -> list[Paper]:
    """Search DBLP publication API."""
    encoded = urllib.parse.quote(query)
    url = f"https://dblp.org/search/publ/api?q={encoded}&format=json&h={max_results}"
    data = _http_get_json(url)
    if not data:
        return []

    papers = []
    try:
        hits = data.get("result", {}).get("hits", {}).get("hit", [])
        if not isinstance(hits, list):
            hits = [hits]
        for hit in hits:
            info = hit.get("info", {})
            # Authors: can be dict (single) or list
            raw_authors = info.get("authors", {}).get("author", [])
            if isinstance(raw_authors, dict):
                raw_authors = [raw_authors]
            authors = [a.get("text", a) if isinstance(a, dict) else str(a) for a in raw_authors]

            # Extract arXiv ID from DBLP volume field (e.g. "abs/2106.09685")
            volume = info.get("volume", "")
            arxiv_id = None
            if volume and volume.startswith("abs/"):
                arxiv_id = volume[4:]

            papers.append(Paper(
                title=info.get("title", "").rstrip("."),
                authors=authors,
                year=int(info.get("year", 0)),
                venue=info.get("venue", ""),
                doi=info.get("doi"),
                dblp_key=info.get("key"),
                arxiv_id=arxiv_id,
                pages=info.get("pages"),
                volume=volume if not volume.startswith("abs/") else None,
                source="dblp",
                confirmed_by=["dblp"],
            ))
    except Exception:
        pass
    return papers


def _fetch_bibtex_from_dblp(dblp_key: str) -> str | None:
    """Fetch official BibTeX from DBLP: dblp.org/rec/{key}.bib"""
    url = f"https://dblp.org/rec/{dblp_key}.bib"
    bib = _http_get(url)
    if bib and "@" in bib:
        return bib.strip()
    return None


# ═══════════════════════════════════════════════════════════
#  CrossRef search + BibTeX fetch
# ═══════════════════════════════════════════════════════════

def _search_crossref(query: str, max_results: int = 10) -> list[Paper]:
    """Search CrossRef works API."""
    encoded = urllib.parse.quote(query)
    url = (
        f"https://api.crossref.org/works?query.bibliographic={encoded}"
        f"&rows={max_results}&mailto={_CROSSREF_MAILTO}"
    )
    data = _http_get_json(url)
    if not data:
        return []

    papers = []
    try:
        items = data.get("message", {}).get("items", [])
        for item in items:
            title_list = item.get("title", [])
            title = title_list[0] if title_list else ""
            authors = []
            for a in item.get("author", []):
                given = a.get("given", "")
                family = a.get("family", "")
                authors.append(f"{given} {family}".strip())
            date_parts = item.get("published-print", item.get("published-online", {})).get("date-parts", [[0]])
            year = date_parts[0][0] if date_parts and date_parts[0] else 0
            container = item.get("container-title", [])

            papers.append(Paper(
                title=title,
                authors=authors,
                year=int(year) if year else 0,
                venue=container[0] if container else "",
                doi=item.get("DOI"),
                pages=item.get("page"),
                volume=item.get("volume"),
                abstract=item.get("abstract"),
                source="crossref",
                confirmed_by=["crossref"],
            ))
    except Exception:
        pass
    return papers


def _fetch_bibtex_from_doi(doi: str) -> str | None:
    """Fetch official BibTeX via DOI content negotiation."""
    url = f"https://doi.org/{doi}"
    headers = {"Accept": "application/x-bibtex"}
    bib = _http_get(url, headers=headers)
    if bib and "@" in bib:
        return bib.strip()
    return None


# ═══════════════════════════════════════════════════════════
#  arXiv search (metadata only, no BibTeX)
# ═══════════════════════════════════════════════════════════

def _search_arxiv(query: str, max_results: int = 5) -> list[Paper]:
    """Search arXiv API for metadata (title, authors, abstract, arxiv_id)."""
    encoded = urllib.parse.quote(query)
    url = f"http://export.arxiv.org/api/query?search_query=all:{encoded}&max_results={max_results}"
    body = _http_get(url, timeout=20)
    if not body:
        return []

    papers = []
    try:
        # Simple XML parsing (avoid lxml dependency)
        entries = re.findall(r"<entry>(.*?)</entry>", body, re.DOTALL)
        for entry in entries:
            title_m = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
            title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""

            authors = re.findall(r"<name>(.*?)</name>", entry)

            published_m = re.search(r"<published>(.*?)</published>", entry)
            year = int(published_m.group(1)[:4]) if published_m else 0

            abstract_m = re.search(r"<summary>(.*?)</summary>", entry, re.DOTALL)
            abstract = re.sub(r"\s+", " ", abstract_m.group(1)).strip() if abstract_m else None

            id_m = re.search(r"<id>http://arxiv.org/abs/(.*?)</id>", entry)
            arxiv_id = id_m.group(1) if id_m else None
            # Strip version suffix for cleaner ID
            if arxiv_id and "v" in arxiv_id:
                arxiv_id = re.sub(r"v\d+$", "", arxiv_id)

            doi_m = re.search(r'<link.*?href="http://dx.doi.org/(.*?)"', entry)
            doi = doi_m.group(1) if doi_m else None

            papers.append(Paper(
                title=title,
                authors=authors,
                year=year,
                venue="arXiv",
                doi=doi,
                arxiv_id=arxiv_id,
                abstract=abstract,
                source="arxiv",
                confirmed_by=["arxiv"],
            ))
    except Exception:
        pass
    return papers


# ═══════════════════════════════════════════════════════════
#  Semantic Scholar search (metadata only)
# ═══════════════════════════════════════════════════════════

def _search_semantic_scholar(query: str, max_results: int = 5) -> list[Paper]:
    """Search Semantic Scholar for metadata (abstract, citation count)."""
    encoded = urllib.parse.quote(query)
    fields = "title,authors,year,venue,externalIds,abstract,citationCount"
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={encoded}&limit={max_results}&fields={fields}"
    data = _http_get_json(url)
    if not data:
        return []

    papers = []
    try:
        for item in data.get("data", []):
            authors = [a.get("name", "") for a in item.get("authors", [])]
            ext_ids = item.get("externalIds", {}) or {}
            papers.append(Paper(
                title=item.get("title", ""),
                authors=authors,
                year=item.get("year") or 0,
                venue=item.get("venue", ""),
                doi=ext_ids.get("DOI"),
                arxiv_id=ext_ids.get("ArXiv"),
                abstract=item.get("abstract"),
                citation_count=item.get("citationCount"),
                source="s2",
                confirmed_by=["s2"],
            ))
    except Exception:
        pass
    return papers


# ═══════════════════════════════════════════════════════════
#  Deduplication & cross-confirmation
# ═══════════════════════════════════════════════════════════

def _dedup_key(paper: Paper) -> str:
    """Generate a dedup key from DOI or normalized title."""
    if paper.doi:
        return f"doi:{paper.doi.lower()}"
    return f"title:{_normalize_title(paper.title)}"


def _is_published(paper: Paper) -> bool:
    """Check if a paper is a published (non-preprint) version."""
    venue_lower = paper.venue.lower() if paper.venue else ""
    return venue_lower not in ("", "arxiv", "corr") and "preprint" not in venue_lower


def _merge_papers(existing: Paper, new: Paper) -> Paper:
    """Merge metadata from new into existing (fill gaps, extend confirmed_by).

    If existing is a preprint and new is a published version, the published
    version's metadata (venue, year, dblp_key) takes priority.
    """
    # Published version takes priority over preprint
    if not _is_published(existing) and _is_published(new):
        existing.venue = new.venue
        existing.year = new.year
        if new.dblp_key:
            existing.dblp_key = new.dblp_key
        if new.pages:
            existing.pages = new.pages
        if new.volume:
            existing.volume = new.volume
        existing.source = new.source

    for src in new.confirmed_by:
        if src not in existing.confirmed_by:
            existing.confirmed_by.append(src)
    if not existing.abstract and new.abstract:
        existing.abstract = new.abstract
    if not existing.citation_count and new.citation_count:
        existing.citation_count = new.citation_count
    if not existing.doi and new.doi:
        existing.doi = new.doi
    if not existing.dblp_key and new.dblp_key:
        existing.dblp_key = new.dblp_key
    if not existing.arxiv_id and new.arxiv_id:
        existing.arxiv_id = new.arxiv_id
    return existing


def _cross_confirm(papers: list[Paper]) -> list[Paper]:
    """Cross-confirm papers: for each DBLP paper, search CrossRef by title (and vice versa).

    This enriches confirmed_by and fills missing fields (DOI, abstract, etc.).
    Only does cross-confirmation for papers confirmed by a single source.
    """
    for paper in papers:
        if len(paper.confirmed_by) >= 2:
            continue

        if "dblp" in paper.confirmed_by and "crossref" not in paper.confirmed_by:
            # Confirm via CrossRef
            if paper.doi:
                cr_bib = _fetch_bibtex_from_doi(paper.doi)
                if cr_bib:
                    paper.confirmed_by.append("crossref")
                    time.sleep(_CROSSREF_DELAY)
            else:
                cr_results = _search_crossref(paper.title, max_results=3)
                time.sleep(_CROSSREF_DELAY)
                for cr in cr_results:
                    if title_similarity(paper.title, cr.title) >= _SIMILARITY_THRESHOLD:
                        paper.confirmed_by.append("crossref")
                        if not paper.doi and cr.doi:
                            paper.doi = cr.doi
                        if not paper.abstract and cr.abstract:
                            paper.abstract = cr.abstract
                        break

        elif "crossref" in paper.confirmed_by and "dblp" not in paper.confirmed_by:
            # Confirm via DBLP
            dblp_results = _search_dblp(paper.title, max_results=3)
            time.sleep(_DBLP_DELAY)
            for d in dblp_results:
                if title_similarity(paper.title, d.title) >= _SIMILARITY_THRESHOLD:
                    paper.confirmed_by.append("dblp")
                    if not paper.dblp_key and d.dblp_key:
                        paper.dblp_key = d.dblp_key
                    break

    return papers


# ═══════════════════════════════════════════════════════════
#  Main search entry point
# ═══════════════════════════════════════════════════════════

def search_papers(query: str, max_results: int = 15) -> list[Paper]:
    """Cascade search: DBLP → CrossRef → arXiv → Semantic Scholar.

    Returns deduplicated, cross-confirmed papers with abstracts and citation counts.
    """
    seen: dict[str, Paper] = {}

    # DBLP (primary)
    for p in _search_dblp(query, max_results=max_results):
        key = _dedup_key(p)
        if key in seen:
            _merge_papers(seen[key], p)
        else:
            seen[key] = p
    time.sleep(_DBLP_DELAY)

    # CrossRef (secondary)
    for p in _search_crossref(query, max_results=max_results):
        key = _dedup_key(p)
        if key in seen:
            _merge_papers(seen[key], p)
        else:
            seen[key] = p
    time.sleep(_CROSSREF_DELAY)

    # arXiv (supplement: abstracts)
    for p in _search_arxiv(query, max_results=5):
        key = _dedup_key(p)
        if key in seen:
            _merge_papers(seen[key], p)
        else:
            seen[key] = p

    # Semantic Scholar (supplement: abstracts + citation count)
    for p in _search_semantic_scholar(query, max_results=5):
        key = _dedup_key(p)
        if key in seen:
            _merge_papers(seen[key], p)
        else:
            seen[key] = p

    papers = list(seen.values())

    # Cross-confirm single-source papers (only top candidates to limit API calls)
    single_source = [p for p in papers if len(p.confirmed_by) == 1]
    if single_source:
        _cross_confirm(single_source[:10])

    # Sort: dual-confirmed first, then by citation count
    def sort_key(p):
        confirmed = len(p.confirmed_by)
        citations = p.citation_count or 0
        return (-confirmed, -citations)

    papers.sort(key=sort_key)
    return papers[:max_results]


# ═══════════════════════════════════════════════════════════
#  Fetch official BibTeX for a Paper
# ═══════════════════════════════════════════════════════════

def _fetch_bibtex_from_arxiv(arxiv_id: str) -> str | None:
    """Fetch BibTeX directly from arXiv: arxiv.org/bibtex/{id}

    Adds a note field with arXiv ID so it renders in all .bst styles
    (plainnat etc. don't support the eprint field natively).
    """
    url = f"https://arxiv.org/bibtex/{arxiv_id}"
    bib = _http_get(url)
    if bib and "@" in bib:
        stripped = _strip_bibtex_fields(bib.strip())
        # Add note with arXiv info for .bst files that don't support eprint
        if "note" not in stripped.lower():
            stripped = stripped.rstrip().rstrip("}")
            stripped += f"  note = {{arXiv:{arxiv_id}}},\n}}"
        return stripped
    return None


# Whitelist of BibTeX fields to keep, per entry type.
# Everything else is stripped.
_KEEP_FIELDS = {
    "inproceedings": {"author", "title", "booktitle", "year"},
    "article":       {"author", "title", "journal", "year", "volume", "number", "pages"},
    "misc":          {"author", "title", "year", "eprint", "archiveprefix", "primaryclass"},
    "phdthesis":     {"author", "title", "school", "year"},
    "techreport":    {"author", "title", "institution", "year"},
}
# Default whitelist for unknown entry types
_KEEP_FIELDS_DEFAULT = {"author", "title", "year", "booktitle", "journal", "volume"}


def _normalize_bib_format(bib: str) -> str:
    """Convert one-line BibTeX (common from CrossRef) to multi-line format.

    E.g. '@article{key, title={T}, author={A}, year={2024}}' becomes:
    '@article{key,\n  title={T},\n  author={A},\n  year={2024},\n}'
    """
    # Check if it's likely one-line (no newlines between fields)
    stripped = bib.strip()
    if "\n" in stripped and not stripped.count("\n") < 3:
        return bib  # already multi-line

    # Find the entry header: @type{key,
    header_match = re.match(r"(@\w+\{[^,]+),\s*", stripped)
    if not header_match:
        return bib

    header = header_match.group(1)
    rest = stripped[header_match.end():]

    # Remove trailing }
    if rest.endswith("}"):
        rest = rest[:-1].rstrip()

    # Split fields: each field is name={value} or name=value
    fields = []
    depth = 0
    current = ""
    for ch in rest:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            field = current.strip()
            if field:
                fields.append(field)
            current = ""
        else:
            current += ch
    if current.strip():
        fields.append(current.strip())

    # Rebuild as multi-line
    lines = [f"{header},"]
    for f in fields:
        if not f.endswith(","):
            f += ","
        lines.append(f"  {f}")
    lines.append("}")

    return "\n".join(lines)


def _strip_bibtex_fields(bib: str) -> str:
    """Keep only essential fields for AI conference papers. Strip everything else.

    Uses brace-depth tracking to correctly handle multi-line fields that contain
    commas (e.g. booktitle with city, state, country).
    """
    # Normalize one-line BibTeX to multi-line first
    bib = _normalize_bib_format(bib)

    # Detect entry type
    type_match = re.match(r"@(\w+)\s*\{", bib.strip())
    entry_type = type_match.group(1).lower() if type_match else ""
    keep = _KEEP_FIELDS.get(entry_type, _KEEP_FIELDS_DEFAULT)

    lines = bib.split("\n")
    result = []
    current_field_kept = None  # True = keeping, False = skipping, None = no active field
    brace_depth = 0  # track nested braces within a field value

    for line in lines:
        stripped = line.strip()

        # Always keep the @type{key line and closing }
        if stripped.startswith("@") or stripped == "}":
            result.append(line)
            current_field_kept = None
            brace_depth = 0
            continue

        # Check if this line starts a new field
        field_match = re.match(r"(\w+)\s*=", stripped)
        if field_match:
            field_name = field_match.group(1).lower()
            current_field_kept = field_name in keep
            # Count braces to track when field value ends
            brace_depth = 0
            for ch in stripped[stripped.index("=") + 1:]:
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
            if current_field_kept:
                result.append(line)
            if brace_depth <= 0:
                current_field_kept = None  # field complete on this line
        else:
            # Continuation line — track braces
            for ch in stripped:
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
            if current_field_kept is True:
                result.append(line)
            if brace_depth <= 0:
                current_field_kept = None  # field complete

    cleaned = "\n".join(result)

    # Remove volume if it equals year (e.g. TMLR where volume=2025, year=2025)
    vol_match = re.search(r"volume\s*=\s*\{(\d{4})\}", cleaned)
    year_match = re.search(r"year\s*=\s*\{(\d{4})\}", cleaned)
    if vol_match and year_match and vol_match.group(1) == year_match.group(1):
        cleaned = re.sub(r"\s*volume\s*=\s*\{\d{4}\},?\n?", "\n", cleaned)

    return cleaned




def fetch_bibtex(paper: Paper) -> str | None:
    """Fetch official BibTeX for a paper.

    Priority: DBLP published > DBLP by title search > DOI > arXiv.
    DBLP is preferred because it has correct capitalization ({GANs}, {BERT}, etc.).
    arXiv endpoint is only used when no published version exists.
    """
    # 1. DBLP by key (best: clean formatting, correct capitalization)
    if paper.dblp_key:
        bib = _fetch_bibtex_from_dblp(paper.dblp_key)
        if bib:
            # If DBLP returns CoRR (arXiv preprint) and we have arxiv_id,
            # use arXiv's own BibTeX instead
            if "journal      = {CoRR}" in bib and paper.arxiv_id:
                arxiv_bib = _fetch_bibtex_from_arxiv(paper.arxiv_id)
                if arxiv_bib:
                    return arxiv_bib
            return _strip_bibtex_fields(bib)
        time.sleep(_DBLP_DELAY)

    # 2. DBLP by title search (paper may have been found via CrossRef/S2 without dblp_key)
    if not paper.dblp_key and paper.title:
        dblp_results = _search_dblp(paper.title, max_results=3)
        time.sleep(_DBLP_DELAY)
        for d in dblp_results:
            if title_similarity(paper.title, d.title) >= _SIMILARITY_THRESHOLD and d.dblp_key:
                bib = _fetch_bibtex_from_dblp(d.dblp_key)
                if bib:
                    if "journal      = {CoRR}" in bib and (paper.arxiv_id or d.arxiv_id):
                        aid = paper.arxiv_id or d.arxiv_id
                        arxiv_bib = _fetch_bibtex_from_arxiv(aid)
                        if arxiv_bib:
                            return arxiv_bib
                    return _strip_bibtex_fields(bib)
                break

    # 3. arXiv (before DOI — DOI content negotiation loses arXiv fields)
    if paper.arxiv_id and not _is_published(paper):
        arxiv_bib = _fetch_bibtex_from_arxiv(paper.arxiv_id)
        if arxiv_bib:
            return arxiv_bib

    # 4. DOI content negotiation (CrossRef)
    if paper.doi:
        bib = _fetch_bibtex_from_doi(paper.doi)
        if bib:
            return _strip_bibtex_fields(bib)
        time.sleep(_CROSSREF_DELAY)

    # 5. arXiv (last resort, even for published papers if nothing else worked)
    if paper.arxiv_id:
        bib = _fetch_bibtex_from_arxiv(paper.arxiv_id)
        if bib:
            return bib

    return None


# ═══════════════════════════════════════════════════════════
#  BibTeX parsing
# ═══════════════════════════════════════════════════════════

def parse_bib(bib_path: str) -> list[dict]:
    """Parse a .bib file into a list of entry dicts.

    Each dict has: key, entry_type, fields (dict), raw (original text),
    preceding_comments (comment lines above the entry).
    """
    content = Path(bib_path).read_text(errors="replace")
    entries = []

    # Match complete BibTeX entries: @type{key, ... }
    # We track position to capture preceding comments
    pattern = re.compile(
        r"(@\w+\s*\{)([^,\s]+)\s*,\s*(.*?)\n\}",
        re.DOTALL,
    )

    for match in pattern.finditer(content):
        entry_type_raw = match.group(1).strip().lstrip("@").rstrip("{").strip().lower()
        entry_key = match.group(2).strip()
        body = match.group(3)

        # Extract fields
        fields = {}
        # Match field = {value} or field = "value" or field = number
        field_pattern = re.compile(
            r"(\w+)\s*=\s*(?:\{((?:[^{}]|\{[^{}]*\})*)\}|\"([^\"]*)\"|(\d+))",
        )
        for fm in field_pattern.finditer(body):
            fname = fm.group(1).lower()
            fval = fm.group(2) if fm.group(2) is not None else (fm.group(3) if fm.group(3) is not None else fm.group(4))
            fields[fname] = fval.strip() if fval else ""

        # Capture preceding comments (lines starting with %)
        start = match.start()
        preceding = ""
        lines_before = content[:start].split("\n")
        comment_lines = []
        for line in reversed(lines_before):
            stripped = line.strip()
            if stripped.startswith("%"):
                comment_lines.insert(0, stripped)
            elif stripped == "":
                continue
            else:
                break
        preceding = "\n".join(comment_lines)

        entries.append({
            "key": entry_key,
            "entry_type": entry_type_raw,
            "fields": fields,
            "raw": match.group(0),
            "preceding_comments": preceding,
        })

    return entries


# ═══════════════════════════════════════════════════════════
#  BibTeX cleanup: remove unused entries
# ═══════════════════════════════════════════════════════════

def cleanup_unused(bib_path: str, tex_dir: str) -> list[str]:
    """Remove entries from .bib that are not cited in any .tex file.

    Returns list of removed keys.
    """
    bib_file = Path(bib_path)
    tex_path = Path(tex_dir)
    if not bib_file.exists():
        return []

    # Collect all cited keys from .tex files
    cited_keys = set()
    for tex_file in tex_path.glob("**/*.tex"):
        tex_content = tex_file.read_text(errors="replace")
        # Match \cite{key}, \citep{key}, \citet{key}, \cite{a,b,c}, etc.
        for m in re.finditer(r"\\cite[pt]?\{([^}]+)\}", tex_content):
            for key in m.group(1).split(","):
                cited_keys.add(key.strip())

    # Parse bib and filter
    entries = parse_bib(bib_path)
    if not entries:
        return []

    content = bib_file.read_text(errors="replace")
    removed = []
    for entry in entries:
        if entry["key"] not in cited_keys:
            # Remove this entry (and its preceding comments) from content
            # Remove preceding [ARK:...] or [NEEDS-CHECK] comments too
            to_remove = entry["raw"]
            if entry["preceding_comments"]:
                to_remove = entry["preceding_comments"] + "\n" + to_remove
            content = content.replace(to_remove, "")
            # Also try removing just the raw entry (in case comments didn't match)
            content = content.replace(entry["raw"], "")
            removed.append(entry["key"])

    if removed:
        # Clean up excessive blank lines
        content = re.sub(r"\n{3,}", "\n\n", content)
        bib_file.write_text(content)

    return removed


# ═══════════════════════════════════════════════════════════
#  Verification
# ═══════════════════════════════════════════════════════════

def _is_ark_managed(entry: dict) -> bool:
    """Check if an entry was written by the ARK citation system."""
    return _ARK_SOURCE_TAG in entry.get("preceding_comments", "")


def _is_preprint(entry: dict) -> bool:
    """Check if entry looks like an arXiv preprint."""
    journal = entry.get("fields", {}).get("journal", "").lower()
    note = entry.get("fields", {}).get("note", "").lower()
    return "arxiv" in journal or "arxiv" in note or "preprint" in journal


def verify_bib(bib_path: str) -> list[VerificationResult]:
    """Verify each entry in references.bib against DBLP + CrossRef.

    Skips entries tagged with [ARK:source=...].
    Detects preprint → published upgrades.
    """
    entries = parse_bib(bib_path)
    results = []

    for entry in entries:
        key = entry["key"]
        fields = entry["fields"]
        title = fields.get("title", "")
        raw = entry["raw"]

        # Skip ARK-managed entries (already from API)
        if _is_ark_managed(entry):
            results.append(VerificationResult(
                status="VERIFIED", entry_key=key,
                original_bibtex=raw, details="ARK-managed entry",
            ))
            continue

        if not title:
            results.append(VerificationResult(
                status="NEEDS-CHECK", entry_key=key,
                original_bibtex=raw, details="No title field",
            ))
            continue

        # Try DOI lookup first
        doi = fields.get("doi")
        found_in = []
        best_bibtex = None

        if doi:
            bib = _fetch_bibtex_from_doi(doi)
            if bib:
                found_in.append("crossref")
                best_bibtex = _strip_bibtex_fields(bib)
            time.sleep(_CROSSREF_DELAY)

        # Search DBLP by title
        dblp_results = _search_dblp(title, max_results=3)
        time.sleep(_DBLP_DELAY)
        dblp_match = None
        for d in dblp_results:
            if title_similarity(title, d.title) >= _SIMILARITY_THRESHOLD:
                dblp_match = d
                found_in.append("dblp")
                # For arXiv papers found on DBLP: use arXiv's own BibTeX
                if d.dblp_key and "corr" in (d.venue or "").lower() and d.arxiv_id:
                    arxiv_bib = _fetch_bibtex_from_arxiv(d.arxiv_id)
                    if arxiv_bib:
                        best_bibtex = arxiv_bib
                elif d.dblp_key:
                    dblp_bib = _fetch_bibtex_from_dblp(d.dblp_key)
                    time.sleep(_DBLP_DELAY)
                    if dblp_bib:
                        best_bibtex = _strip_bibtex_fields(dblp_bib)
                break

        # If not found via DOI or DBLP, try CrossRef title search
        if not found_in:
            cr_results = _search_crossref(title, max_results=3)
            time.sleep(_CROSSREF_DELAY)
            for cr in cr_results:
                if title_similarity(title, cr.title) >= _SIMILARITY_THRESHOLD:
                    found_in.append("crossref")
                    if cr.doi:
                        bib = _fetch_bibtex_from_doi(cr.doi)
                        time.sleep(_CROSSREF_DELAY)
                        if bib:
                            best_bibtex = _strip_bibtex_fields(bib)
                    break

        # Determine result
        if not found_in:
            results.append(VerificationResult(
                status="NEEDS-CHECK", entry_key=key,
                original_bibtex=raw,
                details="Not found in DBLP or CrossRef",
            ))
        elif len(found_in) >= 2:
            # Check if correction needed (preprint upgrade or metadata fix)
            if best_bibtex and best_bibtex.strip() != raw.strip():
                # Preprint → published upgrade
                if _is_preprint(entry) and dblp_match and "arxiv" not in dblp_match.venue.lower():
                    results.append(VerificationResult(
                        status="CORRECTED", entry_key=key,
                        original_bibtex=raw, corrected_bibtex=best_bibtex,
                        details=f"Upgraded from preprint to published version ({dblp_match.venue} {dblp_match.year})",
                    ))
                else:
                    results.append(VerificationResult(
                        status="VERIFIED", entry_key=key,
                        original_bibtex=raw,
                        details=f"Confirmed by {', '.join(found_in)}",
                    ))
            else:
                results.append(VerificationResult(
                    status="VERIFIED", entry_key=key,
                    original_bibtex=raw,
                    details=f"Confirmed by {', '.join(found_in)}",
                ))
        else:
            # Single source
            if best_bibtex and _is_preprint(entry) and dblp_match and "arxiv" not in dblp_match.venue.lower():
                results.append(VerificationResult(
                    status="CORRECTED", entry_key=key,
                    original_bibtex=raw, corrected_bibtex=best_bibtex,
                    details=f"Upgraded from preprint to published version ({dblp_match.venue} {dblp_match.year})",
                ))
            else:
                results.append(VerificationResult(
                    status="SINGLE_SOURCE", entry_key=key,
                    original_bibtex=raw,
                    corrected_bibtex=best_bibtex,
                    details=f"Found in {found_in[0]} only",
                ))

    return results


def parse_bib_string(bib_string: str) -> list[dict]:
    """Parse a BibTeX string (not file) into entry dicts. Same format as parse_bib."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".bib", delete=False) as f:
        f.write(bib_string)
        f.flush()
        result = parse_bib(f.name)
    Path(f.name).unlink(missing_ok=True)
    return result


# ═══════════════════════════════════════════════════════════
#  Fix bib based on verification results
# ═══════════════════════════════════════════════════════════

def fix_bib(bib_path: str, results: list[VerificationResult]) -> None:
    """Apply verification results: overwrite CORRECTED entries, tag NEEDS-CHECK."""
    bib_file = Path(bib_path)
    content = bib_file.read_text(errors="replace")

    for r in results:
        if r.status == "CORRECTED" and r.corrected_bibtex:
            # Replace original with corrected + ARK source tag
            tagged = f"% {_ARK_SOURCE_TAG}verified]\n{r.corrected_bibtex}"
            content = content.replace(r.original_bibtex, tagged)

        elif r.status == "NEEDS-CHECK":
            # Add note field inside the entry so it shows in PDF
            if "note" not in r.original_bibtex.lower():
                modified_entry = r.original_bibtex.rstrip().rstrip("}")
                # Ensure last field has a trailing comma before adding note
                modified_entry = modified_entry.rstrip()
                if modified_entry and not modified_entry.endswith(","):
                    modified_entry += ","
                modified_entry += "\n" + r'  note = {\textcolor{red}{[NEEDS-CHECK: citation not verified]}}' + "\n}"
                content = content.replace(r.original_bibtex, modified_entry)

    bib_file.write_text(content)


# ═══════════════════════════════════════════════════════════
#  Write papers to bib
# ═══════════════════════════════════════════════════════════

def append_papers_to_bib(bib_path: str, papers: list[Paper]) -> list[str]:
    """Fetch official BibTeX for each paper and append to references.bib.

    Returns list of cite keys that were successfully added.
    """
    bib_file = Path(bib_path)
    if not bib_file.exists():
        bib_file.parent.mkdir(parents=True, exist_ok=True)
        bib_file.write_text("% ARK auto-managed references\n\n")

    existing_keys = {e["key"] for e in parse_bib(bib_path)}

    added_keys = []
    new_entries = []

    for paper in papers:
        bib = fetch_bibtex(paper)
        if not bib:
            # Can't get official BibTeX — skip (don't fabricate)
            continue

        # Extract key from fetched BibTeX
        key_match = re.search(r"@\w+\{([^,\s]+)", bib)
        if not key_match:
            continue
        cite_key = key_match.group(1).strip()

        if cite_key in existing_keys:
            continue

        # Store BibTeX on the paper object for literature.yaml
        paper.bibtex = bib

        source = paper.dblp_key and "dblp" or (paper.doi and "crossref" or paper.source)
        tagged = f"% {_ARK_SOURCE_TAG}{source}]\n{bib}"
        new_entries.append(tagged)
        added_keys.append(cite_key)
        existing_keys.add(cite_key)
        time.sleep(0.2)

    if new_entries:
        with open(bib_path, "a") as f:
            f.write("\n\n" + "\n\n".join(new_entries) + "\n")

    return added_keys


def _write_needs_check_to_bib(bib_path: str, titles: list[str],
                              authors_list: list[str] = None,
                              years_list: list = None) -> list[str]:
    """Write [NEEDS-CHECK] placeholder entries to references.bib.

    Includes author and year if available (from LLM extraction).
    The [NEEDS-CHECK] note tells human reviewers this citation needs manual checking.

    Returns list of generated cite keys.
    """
    bib_file = Path(bib_path)
    if not bib_file.exists():
        bib_file.parent.mkdir(parents=True, exist_ok=True)
        bib_file.write_text("% ARK auto-managed references\n\n")

    existing_keys = {e["key"] for e in parse_bib(bib_path)}

    added_keys = []
    new_entries = []

    for idx, title in enumerate(titles):
        author = (authors_list[idx] if authors_list and idx < len(authors_list) else "") or ""
        year = (years_list[idx] if years_list and idx < len(years_list) else 0) or 0

        # Generate a cite key from title: lowercase, no spaces, first 3 words
        words = re.sub(r"[^\w\s]", "", title.lower()).split()
        cite_key = "".join(words[:3]) if words else "unknown"
        base_key = cite_key
        counter = 2
        while cite_key in existing_keys:
            cite_key = f"{base_key}{counter}"
            counter += 1

        # Build entry with whatever info we have
        fields = [f"  title = {{{title}}}"]
        if author:
            fields.append(f"  author = {{{author} et al.}}")
        if year:
            fields.append(f"  year = {{{year}}}")
        fields.append(r"  note = {\textcolor{red}{[NEEDS-CHECK: citation not verified]}}")

        entry = (
            f"% [NEEDS-CHECK] Not found in DBLP, CrossRef, or Semantic Scholar\n"
            f"@misc{{{cite_key},\n"
            + ",\n".join(fields) + ",\n"
            f"}}"
        )
        new_entries.append(entry)
        added_keys.append(cite_key)
        existing_keys.add(cite_key)

    if new_entries:
        with open(bib_path, "a") as f:
            f.write("\n\n" + "\n\n".join(new_entries) + "\n")

    return added_keys


def regenerate_bib_from_literature(literature_path: str, bib_path: str) -> None:
    """Regenerate references.bib entirely from literature.yaml.

    This is the enforcement mechanism: literature.yaml is the single source of truth.
    Any entries writer added to bib outside of our system are discarded.
    NEEDS-CHECK entries get a note field for PDF visibility.
    """
    import yaml

    lit_file = Path(literature_path)
    if not lit_file.exists():
        return

    try:
        data = yaml.safe_load(lit_file.read_text()) or {}
    except Exception:
        return

    # Collect all BibTeX from literature.yaml references
    entries = []
    _updated = False

    for ref in data.get("references", []):
        if not isinstance(ref, dict):
            continue
        bibtex = ref.get("bibtex")
        key = ref.get("bibtex_key", "")
        source = ref.get("source", "")
        if not key:
            continue

        # If bibtex not stored, try to re-fetch it
        if not bibtex:
            title = ref.get("title", "")
            if title:
                paper = _search_by_title(title)
                if paper:
                    bibtex = fetch_bibtex(paper)
                    if bibtex:
                        ref["bibtex"] = bibtex  # cache for next time
                        _updated = True

        if bibtex:
            tag = f"% [ARK:source={source}]" if source else "% [ARK:source=verified]"
            entries.append(f"{tag}\n{bibtex}")

    # Add NEEDS-CHECK entries
    for nc in data.get("needs_check", []):
        if not isinstance(nc, dict):
            continue
        key = nc.get("bibtex_key", "")
        title = nc.get("title", "")
        author = nc.get("authors", "")
        year = nc.get("year", 0)
        if not key or not title:
            continue

        fields = [f"  title = {{{title}}}"]
        if author:
            fields.append(f"  author = {{{author} et al.}}")
        if year:
            fields.append(f"  year = {{{year}}}")
        fields.append(r"  note = {\textcolor{red}{[NEEDS-CHECK: citation not verified]}}")

        entry = (
            f"% [NEEDS-CHECK]\n"
            f"@misc{{{key},\n"
            + ",\n".join(fields) + ",\n"
            f"}}"
        )
        entries.append(entry)

    # Write bib
    bib_file = Path(bib_path)
    bib_file.parent.mkdir(parents=True, exist_ok=True)
    bib_file.write_text("% ARK auto-managed references\n% Generated from literature.yaml — do not edit manually\n\n"
                        + "\n\n".join(entries) + "\n")


# ═══════════════════════════════════════════════════════════
#  Query extraction from planner issues
# ═══════════════════════════════════════════════════════════

def extract_search_queries(issue_title: str, issue_description: str) -> list[str]:
    """Extract search queries from a planner issue (deterministic, no LLM).

    Focuses on extracting full paper titles and author-year patterns.
    Avoids single-word acronyms that produce garbage search results.
    """
    text = f"{issue_title} {issue_description}"
    queries = []
    seen = set()

    def _add(q):
        q = q.strip().strip(",").strip(".")
        if len(q) > 8 and q.lower() not in seen:
            queries.append(q)
            seen.add(q.lower())

    # 1. Quoted exact names: "Deep Learning with Differential Privacy"
    for m in re.findall(r'"([^"]{8,})"', text):
        _add(m)

    # 2. Parenthesized names with author/year: PATE-GAN (Jordon et al. 2019, ICLR)
    #    Extract what's before the parenthesis as the paper/method name
    for m in re.finditer(r'(\b[A-Z][\w-]+(?:\s+[\w-]+)*)\s*\(([^)]*(?:et al|20\d{2})[^)]*)\)', text):
        name = m.group(1).strip()
        context = m.group(2).strip()
        if len(name) > 3:
            _add(f"{name} {context}")

    # 3. "Title" (Author et al., Year) patterns
    for m in re.finditer(r'"([^"]{8,})"\s*\(([^)]+)\)', text):
        _add(f"{m.group(1)} {m.group(2)}")

    # 4. "Author et al. (Year)" with preceding paper description
    for m in re.finditer(r'(\b[A-Z][\w\s,:-]{10,}?)\s*(?:by\s+)?(\w+\s+et\s+al\.?\s*[\(,]\s*20\d{2})', text):
        title_part = m.group(1).strip()
        author_year = m.group(2).strip()
        _add(f"{title_part} {author_year}")

    # 5. Full paper titles with colon pattern: "Word Word: More Words"
    for m in re.finditer(r'\b([A-Z][A-Za-z]+(?:\s+[A-Za-z]+){2,}:\s+[A-Z][A-Za-z]+(?:\s+[A-Za-z]+){2,})', text):
        _add(m.group(1))

    return queries


# ═══════════════════════════════════════════════════════════
#  Format candidates for researcher agent
# ═══════════════════════════════════════════════════════════

def format_candidates_for_agent(papers: list[Paper]) -> str:
    """Format papers as a numbered list for the researcher agent to select from."""
    if not papers:
        return "(No papers found from academic databases.)"

    lines = []
    for i, p in enumerate(papers, 1):
        authors_str = ", ".join(p.authors[:3])
        if len(p.authors) > 3:
            authors_str += " et al."

        confirmed = ", ".join(p.confirmed_by) if p.confirmed_by else "unconfirmed"
        citation_str = f" | Citations: {p.citation_count}" if p.citation_count else ""

        lines.append(f"[{i}] \"{p.title}\"")
        lines.append(f"    Authors: {authors_str}, {p.year} | Venue: {p.venue}{citation_str}")
        lines.append(f"    Confirmed by: {confirmed}")
        if p.abstract:
            # Truncate abstract
            abstract = p.abstract[:300].strip()
            if len(p.abstract) > 300:
                abstract += "..."
            lines.append(f"    Abstract: {abstract}")
        lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  Parse researcher agent selection
# ═══════════════════════════════════════════════════════════

def parse_agent_selection(agent_output: str, candidates: list[Paper]) -> list[Paper]:
    """Parse the researcher agent's output to extract selected paper indices.

    Recognizes formats: "SELECTED: 1, 5, 11" or "[1]" "[5]" "[11]" on separate lines.
    """
    selected = []

    # Try "SELECTED: 1, 5, 11" format
    sel_match = re.search(r"SELECTED:\s*(.+?)(?:\n|$)", agent_output, re.IGNORECASE)
    if sel_match:
        nums = re.findall(r"\d+", sel_match.group(1))
        selected = [int(n) for n in nums]

    # Also collect [N] patterns throughout the output
    bracket_nums = re.findall(r"\[(\d+)\]", agent_output)
    for n in bracket_nums:
        num = int(n)
        if num not in selected:
            selected.append(num)

    # Map to papers (1-indexed)
    result = []
    for idx in selected:
        if 1 <= idx <= len(candidates):
            result.append(candidates[idx - 1])

    return result


# ═══════════════════════════════════════════════════════════
#  Update literature.yaml with selected papers
# ═══════════════════════════════════════════════════════════

def update_literature_yaml(literature_path: str, papers: list[Paper],
                           cite_keys: list[str], agent_output: str,
                           contexts: list[str] = None) -> None:
    """Update literature.yaml with newly added citations for the writer agent."""
    import yaml
    from datetime import datetime

    lit_file = Path(literature_path)
    if lit_file.exists():
        try:
            data = yaml.safe_load(lit_file.read_text()) or {}
        except Exception:
            data = {}
    else:
        lit_file.parent.mkdir(parents=True, exist_ok=True)
        data = {}

    if "references" not in data:
        data["references"] = []

    existing_titles = {r.get("title", "").lower() for r in data["references"] if isinstance(r, dict)}

    for idx, (paper, key) in enumerate(zip(papers, cite_keys)):
        if paper.title.lower() in existing_titles:
            continue
        ctx = (contexts[idx] if contexts and idx < len(contexts) else "") or ""
        entry = {
            "title": paper.title,
            "authors": ", ".join(paper.authors[:3]) + (" et al." if len(paper.authors) > 3 else ""),
            "year": paper.year,
            "venue": paper.venue,
            "bibtex_key": key,
            "source": f"API ({', '.join(paper.confirmed_by)})",
            "added_date": datetime.now().strftime("%Y-%m-%d"),
        }
        if paper.abstract:
            entry["abstract"] = paper.abstract[:500]
        if paper.bibtex:
            entry["bibtex"] = paper.bibtex
        # Mark importance based on Deep Research context
        if ctx:
            entry["context"] = ctx
            ctx_lower = ctx.lower()
            if "must cite" in ctx_lower or "essential" in ctx_lower or "critical" in ctx_lower or "closest prior work" in ctx_lower:
                entry["importance"] = "critical"
        data["references"].append(entry)

    lit_file.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False))


def _write_needs_check_to_literature(literature_path: str, titles: list[str],
                                     cite_keys: list[str] = None,
                                     contexts: list[str] = None,
                                     authors: list[str] = None,
                                     years: list = None) -> None:
    """Append [NEEDS-CHECK] entries to literature.yaml for papers not found in any API."""
    import yaml
    from datetime import datetime

    lit_file = Path(literature_path)
    if lit_file.exists():
        try:
            data = yaml.safe_load(lit_file.read_text()) or {}
        except Exception:
            data = {}
    else:
        lit_file.parent.mkdir(parents=True, exist_ok=True)
        data = {}

    if "needs_check" not in data:
        data["needs_check"] = []

    existing = {e.get("title", "").lower() for e in data["needs_check"] if isinstance(e, dict)}

    for i, title in enumerate(titles):
        if title.lower() not in existing:
            entry = {
                "title": title,
                "status": "[NEEDS-CHECK]",
                "reason": "Not found in DBLP, CrossRef, or Semantic Scholar",
                "added_date": datetime.now().strftime("%Y-%m-%d"),
            }
            if cite_keys and i < len(cite_keys):
                entry["bibtex_key"] = cite_keys[i]
            author = (authors[i] if authors and i < len(authors) else "") or ""
            year = (years[i] if years and i < len(years) else 0) or 0
            if author:
                entry["authors"] = author
            if year:
                entry["year"] = year
            ctx = (contexts[i] if contexts and i < len(contexts) else "") or ""
            if ctx:
                entry["context"] = ctx
                ctx_lower = ctx.lower()
                if "must cite" in ctx_lower or "essential" in ctx_lower or "critical" in ctx_lower or "closest prior work" in ctx_lower:
                    entry["importance"] = "critical"
            data["needs_check"].append(entry)

    lit_file.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False))


# ═══════════════════════════════════════════════════════════
#  Bootstrap citations from a list of paper titles
# ═══════════════════════════════════════════════════════════

@dataclass
class BootstrapResult:
    """Result of bootstrapping citations from paper titles."""
    found: list          # Papers successfully found and added
    found_keys: list     # BibTeX cite keys for found papers
    needs_check: list    # Titles that couldn't be found in any API


def bootstrap_citations(
    titles: list[str],
    bib_path: str,
    literature_path: str,
    search_queries: list[str] = None,
    authors: list[str] = None,
    years: list = None,
    contexts: list[str] = None,
) -> BootstrapResult:
    """Given a list of paper titles, search APIs, fetch BibTeX, write to bib.

    Args:
        titles: Clean paper titles (used for display and [NEEDS-CHECK] entries).
        bib_path: Path to references.bib.
        literature_path: Path to literature.yaml.
        search_queries: Optional richer search queries (title + author + year).
                        If not provided, titles are used as search queries.

    For each title:
    1. Search by query → if found, fetch BibTeX
    2. If not found, try keyword search
    3. If still not found, mark as [NEEDS-CHECK] using the clean title

    Returns BootstrapResult with found papers and needs-check titles.
    """
    found_papers = []
    found_keys = []
    found_contexts = []
    needs_check_titles = []
    needs_check_authors = []
    needs_check_years = []
    needs_check_contexts = []

    for i, title in enumerate(titles):
        title = title.strip()
        if not title:
            continue

        query = (search_queries[i] if search_queries and i < len(search_queries) else title).strip()

        # Round 1: search by query
        paper = _search_by_title(query)

        # Round 2: if not found, try with just the title
        if not paper and query != title:
            paper = _search_by_title(title)

        # Round 3: if still not found, try keyword search
        if not paper:
            keywords = _extract_keywords_from_title(title)
            if keywords and keywords != _normalize_title(title):
                paper = _search_by_title(keywords)

        if paper:
            # Use Deep Research context as fallback abstract if API didn't provide one
            ctx = (contexts[i] if contexts and i < len(contexts) else "") or ""
            if not paper.abstract and ctx:
                paper.abstract = ctx
            found_papers.append(paper)
            found_contexts.append(ctx)
        else:
            needs_check_titles.append(title)
            nc_author = (authors[i] if authors and i < len(authors) else "") or ""
            nc_year = (years[i] if years and i < len(years) else 0) or 0
            nc_ctx = (contexts[i] if contexts and i < len(contexts) else "") or ""
            needs_check_authors.append(nc_author)
            needs_check_years.append(nc_year)
            needs_check_contexts.append(nc_ctx)

    # Fetch BibTeX and write to references.bib
    if found_papers:
        found_keys = append_papers_to_bib(bib_path, found_papers)

    # Write [NEEDS-CHECK] entries to references.bib (with author/year if available)
    needs_check_keys = []
    if needs_check_titles:
        needs_check_keys = _write_needs_check_to_bib(
            bib_path, needs_check_titles, needs_check_authors, needs_check_years
        )

    # Update literature.yaml (found papers + needs-check)
    if found_papers and found_keys:
        update_literature_yaml(literature_path, found_papers, found_keys, "",
                               contexts=found_contexts)
    if needs_check_titles:
        _write_needs_check_to_literature(literature_path, needs_check_titles, needs_check_keys,
                                         needs_check_contexts, needs_check_authors, needs_check_years)

    return BootstrapResult(
        found=found_papers,
        found_keys=found_keys,
        needs_check=needs_check_titles,
    )


def _search_by_title(title: str) -> Paper | None:
    """Search for a specific paper by title across all APIs.

    Uses strict similarity matching first. For short queries (likely abbreviations
    or partial titles), also checks if the query is contained in the result title.

    Returns the best matching Paper or None.
    """
    def _matches(paper: Paper) -> bool:
        return title_similarity(title, paper.title) >= _SIMILARITY_THRESHOLD

    # Try DBLP first — prefer published over preprint if both match
    dblp_results = _search_dblp(title, max_results=5)
    time.sleep(_DBLP_DELAY)
    dblp_matches = [p for p in dblp_results if _matches(p)]
    if dblp_matches:
        # Prefer published version
        published = [p for p in dblp_matches if _is_published(p)]
        best = published[0] if published else dblp_matches[0]
        if not best.abstract:
            _supplement_abstract(best)
        return best

    # Try CrossRef
    cr_results = _search_crossref(title, max_results=3)
    time.sleep(_CROSSREF_DELAY)
    for p in cr_results:
        if _matches(p):
            if not p.abstract:
                _supplement_abstract(p)
            return p

    # Try Semantic Scholar (has good title matching + abstract)
    s2_results = _search_semantic_scholar(title, max_results=3)
    for p in s2_results:
        if _matches(p):
            return p

    return None


def _supplement_abstract(paper: Paper) -> None:
    """Try to get abstract from Semantic Scholar for a paper that lacks one."""
    s2_results = _search_semantic_scholar(paper.title, max_results=1)
    if s2_results and title_similarity(paper.title, s2_results[0].title) >= _SIMILARITY_THRESHOLD:
        if s2_results[0].abstract:
            paper.abstract = s2_results[0].abstract
        if s2_results[0].citation_count and not paper.citation_count:
            paper.citation_count = s2_results[0].citation_count


def _extract_keywords_from_title(title: str) -> str:
    """Extract meaningful keywords from a paper title for broader search.

    Removes common academic filler words, keeps the substance.
    """
    stopwords = {
        "a", "an", "the", "of", "for", "in", "on", "to", "and", "or", "is",
        "are", "was", "were", "with", "by", "from", "at", "as", "its", "it",
        "that", "this", "via", "using", "towards", "toward", "through", "into",
        "between", "among", "based", "new", "novel", "simple", "efficient",
        "approach", "method", "framework", "study", "analysis", "survey",
    }
    words = _normalize_title(title).split()
    keywords = [w for w in words if w.lower() not in stopwords and len(w) > 1]
    return " ".join(keywords[:8])  # Keep top 8 keywords


# ═══════════════════════════════════════════════════════════
#  Mark [NEEDS-CHECK] citations in tex files
# ═══════════════════════════════════════════════════════════

def mark_needs_check_in_tex(bib_path: str, tex_dir: str,
                            literature_path: str = None) -> int:
    """Scan .tex files and add [NEEDS-CHECK] marker after any \\cite of a needs-check entry.

    Reads NEEDS-CHECK keys from literature.yaml (single source of truth).
    Falls back to parsing bib comments if literature.yaml not available.
    Returns number of citations marked.
    """
    import yaml

    needs_check_keys = set()

    # Primary: read from literature.yaml
    if literature_path:
        lit_file = Path(literature_path)
        if lit_file.exists():
            try:
                lit_data = yaml.safe_load(lit_file.read_text()) or {}
                for nc in lit_data.get("needs_check", []):
                    if isinstance(nc, dict) and nc.get("bibtex_key"):
                        needs_check_keys.add(nc["bibtex_key"])
            except Exception:
                pass

    # Fallback: parse bib comments
    if not needs_check_keys:
        entries = parse_bib(bib_path)
        for entry in entries:
            if "[NEEDS-CHECK]" in entry.get("preceding_comments", ""):
                needs_check_keys.add(entry["key"])

    if not needs_check_keys:
        return 0

    marked = 0
    tex_path = Path(tex_dir)
    marker = r"\textcolor{red}{[NEEDS-CHECK]}"

    for tex_file in tex_path.glob("**/*.tex"):
        content = tex_file.read_text(errors="replace")
        new_content = content

        for key in needs_check_keys:
            # Match \cite{key}, \citep{key}, \citet{key} not already followed by marker
            for cite_cmd in ["cite", "citep", "citet"]:
                pattern = rf"(\\{cite_cmd}\{{{key}\}})(?!\s*\\textcolor)"
                # Use a function replacement to avoid \t interpretation issues
                def _add_marker(m, _marker=marker):
                    return m.group(1) + _marker
                new_content, count = re.subn(pattern, _add_marker, new_content)
                marked += count

        if new_content != content:
            # Ensure xcolor package is loaded for \textcolor to work
            if r"\textcolor" in new_content and "xcolor" not in new_content:
                new_content = re.sub(
                    r"(\\documentclass[^\n]*\n)",
                    r"\1\\usepackage{xcolor}\n",
                    new_content,
                    count=1,
                )
            tex_file.write_text(new_content)

    return marked
