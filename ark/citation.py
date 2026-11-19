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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from ark.template_preprocess import find_matching_brace


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

_CROSSREF_MAILTO = "contact@idea2paper.org"
_REQUEST_TIMEOUT = 15  # seconds
_DBLP_DELAY = 0.3      # polite delay between DBLP requests
_CROSSREF_DELAY = 0.2
_ARK_SOURCE_TAG = "[ARK:source="   # tag in bib comments to mark our entries
_SIMILARITY_THRESHOLD = 0.82

# ── The one citation-command regex ───────────────────────────────────────────
# Every "which keys does this .tex cite?" question in the codebase MUST use
# this. It used to be answered by two divergent regexes — a narrow
# r"\\cite[pt]?\{...}" here in cleanup_unused, and the wide one in
# CompilerMixin._CITE_CMD_RE — which silently destroyed valid citations in two
# steps: the narrow regex missed `\citep[e.g.,][]{key}` (natbib's standard form,
# used by ACL/ICLR/TMLR/MLSys), so cleanup_unused judged the entry uncited and
# DELETED it from references.bib; the next compile_latex() ran the wide regex,
# found the now-undefined key, and stripped the \cite from the prose. A real,
# API-verified reference vanished and each module looked correct in isolation.
#
# Matches \cite, \citep, \citet, \nocite, \citeauthor, \citeyear, biblatex's
# \parencite / \autocite / \textcite / \smartcite / \fullcite, and any
# project-specific command whose name contains "cite" — plus natbib's optional
# [pre] and [post] arguments. Groups: (1) command name, (2) [pre], (3) [post],
# (4) comma-separated keys.
CITE_CMD_RE = re.compile(
    r'\\(\w*cite\w*)\s*'
    r'(\[[^\]]*\])?\s*'
    r'(\[[^\]]*\])?\s*'
    r'\{([^}]*)\}'
)

# `\nocite{*}` means "put every bib entry in the bibliography". It is a wildcard,
# not a key: nothing may treat it as a missing reference.
NOCITE_ALL = "*"


def cited_keys_in_tex(tex_content: str) -> set[str]:
    """Every citation key referenced by ``tex_content``.

    The single source of truth for "is this key cited?". Includes the
    ``\\nocite{*}`` wildcard verbatim when present, so callers can detect it
    (see :data:`NOCITE_ALL`) rather than mistaking it for a real key.
    """
    keys: set[str] = set()
    for m in CITE_CMD_RE.finditer(tex_content):
        for key in (m.group(4) or "").split(","):
            key = key.strip()
            if key:
                keys.add(key)
    return keys


def cited_keys_in_dir(tex_dir) -> set[str]:
    """Union of :func:`cited_keys_in_tex` over every ``.tex`` under ``tex_dir``."""
    keys: set[str] = set()
    for tex_file in Path(tex_dir).glob("**/*.tex"):
        try:
            keys |= cited_keys_in_tex(tex_file.read_text(errors="replace"))
        except Exception:
            continue
    return keys


# ═══════════════════════════════════════════════════════════
#  Research topic → academic-search queries
# ═══════════════════════════════════════════════════════════
#
# DBLP/CrossRef/arXiv/S2 are KEYWORD engines. Handing them a paragraph of
# LLM-oriented prose (the Deep Research prompt) makes CrossRef's
# `query.bibliographic` fuzzy-match incidental words and return topically
# random work: a real run on "spectral bias in two-layer ReLU MLPs" got
# peri-implant mucositis, tunnel-face failure in two-LAYER soils, retinal
# LAYER thickness in dogs, and three CrossRef *figure* records — 12/12 wrong,
# all from CrossRef (DBLP, being CS-only, returned nothing). The same topic as
# 2-4 short keyword queries returns the intended papers on the first page.

# Grammatical filler.
_STOPWORDS = frozenset("""
a an the of in on for to and or with without from as at by is are was were be been being am
this that these those it its their our we they you your he she his her them us
can may might will would should could must not no nor but if then than so such
into over under between among during after before while about across through via
where when which who whom whose what how why there here both each all any some more most
less least very much many few other others same own too also only just even still yet
""".split())

# Words that appear in every other academic title. Keeping them in a query is
# what pulls in "A Multi-Vertical Empirical STUDY" and "COMPARISON of TWO
# methods" from unrelated fields.
_GENERIC_TERMS = frozenset("""
study studies investigation investigating empirical analysis analyses analytical
approach approaches method methods methodology technique techniques framework frameworks
system systems model models modeling comparison comparing evaluation evaluating assessment
results result findings effect effects impact role toward towards case cases application
applications paper work works research review survey report letter note chapter section
figure supplement supplementary appendix table pilot experience overview introduction
novel new recent proposed present presents presented using used use based via
experiment experiments experimental observation observations quantifying characterization
properties property changes development different various multiple several general
sample samples population populations participants subjects cohort patients trial trials
""".split())

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
# `**bold**` / `*emph*` — where a writer marks the actual subject of the work.
_EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
# "Rahaman et al. (2019)" / "Basri et al. 2019" — named prior work is the most
# precise query material available: it names a paper that provably exists.
_AUTHOR_YEAR_RE = re.compile(r"\b([A-Z][a-z]{2,})\s+et\s+al\.?,?\s*\(?((?:19|20)\d{2})")
# Formulas, inline code, urls and markdown scaffolding: noise in a keyword query.
_NOISE_RE = re.compile(
    r"\$[^$]*\$|\\\w+(?:\{[^}]*\})?|`[^`]*`|https?://\S+|\|[^\n]*\||^[#>\-\*\d\.\s]+",
    re.MULTILINE)


def _clean_phrase(text: str) -> str:
    """Strip markup/math and glue the remaining words back together."""
    return " ".join(_WORD_RE.findall(_NOISE_RE.sub(" ", text)))


def topic_terms(text: str) -> set[str]:
    """Distinctive lowercase terms of a research topic.

    Drops grammar words and cross-field academic filler, so what remains is
    what actually identifies the subject ("spectral", "relu", "frequency",
    "kernel") rather than what identifies a paper as a paper ("empirical",
    "study", "comparison").
    """
    out: set[str] = set()
    for w in _WORD_RE.findall(_NOISE_RE.sub(" ", text.lower())):
        w = w.strip("-")
        if len(w) >= 4 and w not in _STOPWORDS and w not in _GENERIC_TERMS:
            out.add(w)
    return out


# CrossRef indexes a paper's FIGURES, TABLES and supplements as records of
# their own, with the caption as the "title". They match topical keywords
# perfectly ("Table 6: Extra layer in attention with ReLU and CNN with
# sigmoid.") and are never citable work.
# The `\b` after the keyword is what keeps real papers out: "Figures of Merit
# …" and "Tabular Data Learning …" have a word character right after
# figure/tab, so they never match.
_ARTIFACT_TITLE_RE = re.compile(
    r"^\s*(?:supplementary\s+|extended\s+data\s+|supporting\s+)?"
    r"(?:fig(?:ure)?|table|scheme|chart|plate|exhibit|appendix|"
    r"algorithm|listing|movie|video|dataset)\b"
    r"\s*(?:[\dIVXivx]{1,4}|[A-Za-z])?\s*"
    r"(?:[.:\)—–-]|figure\s+supplement|$)",
    re.IGNORECASE)


# Publishers also register the supplementary FILE beside the paper, keeping the
# paper's own title and appending the file tag — "…in Coordinate Networks_supp1-3".
# On topic, indistinguishable by keywords, still not the citable record.
_SUPPLEMENT_SUFFIX_RE = re.compile(
    r"[_\-\s](?:supp|suppl|supplement(?:ary|al)?|esm)"
    r"(?:[\s_\-]*(?:material|information|data|file|note|method|text)s?)?"
    r"[\s_\-]*[\dIVXivx]*(?:\s*[-–—]\s*[\dIVXivx]+)?"
    # …and the record is often the FILE, so the title carries its extension:
    # "…in Coordinate Networks_supp1-3667002.pdf". Missing this made the whole
    # pattern a no-op on the real data — the fixture it was written against had
    # been truncated by a log line, which cut the extension off.
    r"(?:\.(?:pdf|docx?|xlsx?|zip|csv|txt|tex|tif{1,2}|png|jpe?g))?\s*$",
    re.IGNORECASE)


def is_artifact_record(paper_title: str) -> bool:
    """True for a figure/table/supplement record masquerading as a paper."""
    title = paper_title or ""
    return bool(_ARTIFACT_TITLE_RE.match(title)
                or _SUPPLEMENT_SUFFIX_RE.search(title))


def looks_like_paper(title: str, authors=None, year=None) -> bool:
    """Is this search hit citable work, as opposed to an indexed fragment?

    Academic indexes carry more than papers. CrossRef registers figures, tables
    and supplements; Springer registers its reference works term by term, so a
    query containing "spectral" and "width" returns encyclopedia stubs titled
    "spectral width", "laser spectral width", "pulse spectral width" — perfect
    keyword matches, zero authors, no year, and nothing to cite. Real work has a
    title plus at least one of an author or a year.
    """
    title = (title or "").strip()
    if not title or is_artifact_record(title):
        return False
    return bool((authors or []) or year)


def title_on_topic(paper_title: str, terms: set[str], min_overlap: int = 2) -> bool:
    """Backstop: does a search hit share enough distinctive terms with the topic?

    Deliberately lenient (two terms). Precision comes from asking a good
    question — this only catches what a keyword query still drags in.
    """
    if not terms or not paper_title:
        return False
    if is_artifact_record(paper_title):
        return False
    return len(topic_terms(paper_title) & terms) >= min_overlap


def search_queries_from_topic(prose: str, title: str = "", limit: int = 4) -> list[str]:
    """Short keyword queries for academic search, from a prose research topic.

    Deterministic on purpose: this feeds the fallback that runs *because* the
    LLM-backed Deep Research just failed, so it must not need an LLM itself.

    Sources, most precise first:
      1. emphasised spans (``**spectral bias in two-layer ReLU MLPs**``);
      2. the paper title, minus generic academic words;
      3. named prior work (``Rahaman et al. (2019)`` → ``Rahaman 2019 …``);
      4. the topic's most distinctive remaining terms.

    Every query is capped at 8 words — long queries are what turn a keyword
    search back into fuzzy prose matching.
    """
    def _shorten(words: list[str], cap: int = 8) -> str:
        keep, seen = [], set()
        for w in words:
            low = w.lower()
            if low in _STOPWORDS or low in _GENERIC_TERMS or low in seen:
                continue
            seen.add(low)
            keep.append(w)
        return " ".join((keep or words)[:cap])

    queries: list[str] = []

    def _add(q: str):
        q = " ".join(q.split())
        if len(q.split()) < 2:
            return
        low = q.lower()
        if any(low == e.lower() for e in queries):
            return
        queries.append(q)

    for m in _EMPHASIS_RE.finditer(prose or ""):
        _add(_shorten(_clean_phrase(m.group(1) or m.group(2) or "").split()))

    if title:
        _add(_shorten(_clean_phrase(title).split()))

    # Rank by how often the topic actually repeats a term — a subject gets
    # restated, incidental vocabulary does not. (Ranking by word LENGTH picks
    # things like "frequency-dependent higher-dimensional": long, not central.)
    corpus = f"{title}\n{prose or ''}"
    wanted = topic_terms(corpus)
    counts: dict[str, int] = {}
    for w in _WORD_RE.findall(_NOISE_RE.sub(" ", corpus.lower())):
        w = w.strip("-")
        if w in wanted:
            counts[w] = counts.get(w, 0) + 1
    ranked = sorted(wanted, key=lambda w: (-counts.get(w, 0), w))

    # Anchor named prior work to the TITLE's own words, in title order — never
    # to corpus frequency. Frequency shifts with how the prose happens to be
    # worded on a given run, and two frequent-but-unrelated terms silently
    # compose into another field's term of art: a real run ranked "width" high
    # (the hypothesis is about width-invariance) and asked for "Rahaman 2019
    # spectral width", which is a laser/radar concept — back came SpringerReference
    # glossary entries for spectral line width and a paper on radar echo
    # turbulence. The title is curated and stable across runs.
    anchor_source = _clean_phrase(title) or _clean_phrase(
        next((m.group(1) or m.group(2) or "" for m in _EMPHASIS_RE.finditer(prose or "")), ""))
    anchor_terms: list[str] = []
    for w in anchor_source.split():
        low = w.lower().strip("-")
        if low in topic_terms(anchor_source) and low not in anchor_terms:
            anchor_terms.append(low)
    anchor = " ".join(anchor_terms[:2]) or " ".join(ranked[:1])

    for surname, year in _AUTHOR_YEAR_RE.findall(corpus):
        _add(f"{surname} {year} {anchor}".strip())

    if ranked:
        _add(" ".join(ranked[:5]))

    return queries[:limit]


# ═══════════════════════════════════════════════════════════
#  HTTP helper
# ═══════════════════════════════════════════════════════════

# Hosts that did not answer since the last reset_source_health(): an HTTP
# error, a timeout, or a page that is not the API's JSON. A search that got no
# answer says nothing about whether the paper exists. Measured from mcnode15 on
# 2026-11-05: DBLP served a bot-challenge page to every API call, arXiv timed
# out, Semantic Scholar and OpenAlex returned 429. Each looked like "no hits",
# and the citation loop deleted LoRA, T5 and EWC from a paper as unconfirmable.
_UNAVAILABLE: set[str] = set()


def reset_source_health() -> None:
    _UNAVAILABLE.clear()


def unavailable_sources() -> list[str]:
    return sorted(_UNAVAILABLE)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or url


def _http_get(url: str, headers: dict | None = None, timeout: int = _REQUEST_TIMEOUT) -> str | None:
    """Simple HTTP GET, returns response body or None on error."""
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", f"ARK-Research/0.1 (mailto:{_CROSSREF_MAILTO})")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # A status code is an answer: the host is up and said no. Only "come
        # back later" (429) and a server-side fault (5xx) mean we could not
        # ask. Treating a 404 as an outage let one hallucinated DOI or DBLP
        # key — `doi.org/{doi}` and `dblp.org/rec/{key}.bib` are both plain
        # GETs — put its host in `unavailable_sources()`, and the compiler
        # then keeps the WHOLE bibliography as "unconfirmed, nothing dropped".
        # A fabricated entry suppressed the step that exists to delete it.
        if e.code == 429 or e.code >= 500:
            _UNAVAILABLE.add(_host(url))
        return None
    except (urllib.error.URLError, OSError, TimeoutError):
        _UNAVAILABLE.add(_host(url))
        return None


def _http_get_json(url: str, timeout: int = _REQUEST_TIMEOUT) -> dict | None:
    body = _http_get(url, timeout=timeout)
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        _UNAVAILABLE.add(_host(url))
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
    """First author's surname, lowercased, from either name convention.

    The two sources disagree: the APIs return "Ashish Vaswani" (surname last)
    while BibTeX overwhelmingly writes "Vaswani, Ashish" (surname first). A
    single last-token rule reads the comma form as "ashish", so comparing a
    bib entry against an API hit would never match and every author check
    would quietly fail open. The comma is the discriminator — where it is
    present, everything before it is the surname.
    """
    if not authors:
        return ""
    name = authors[0] if isinstance(authors[0], str) else str(authors[0])
    name = re.sub(r"\s+et\s+al\.?$", "", name, flags=re.IGNORECASE)
    if "," in name:
        name = name.split(",", 1)[0]
    # Tokens that are not the surname: DBLP's homonym number ("Wei Wang 0001")
    # and generational suffixes. Taking the last token blindly compared "0001"
    # against "wang" and vetoed the right paper.
    parts = [t for t in name.split()
             if not t.isdigit() and t.strip(".").lower() not in _NAME_SUFFIXES]
    return _plain_name(parts[-1]) if parts else ""


_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})


def _plain_name(token: str) -> str:
    """A surname reduced to lowercase ASCII letters.

    BibTeX spells accents as commands inside braces (``M{\\"u}ller``,
    ``{Vaswani}``) where the APIs return Unicode ("Müller"). Both reduce to
    "muller" here, so the comparison is about the person, not the encoding.
    """
    t = re.sub(r"\\[a-zA-Z]+\s*|\\.", "", token)       # \ss, \", \', \^ ...
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if c.isalpha() and c.isascii()).lower()


def _same_author(want: str, got: str) -> bool:
    """Same first-author surname, allowing an LLM's one-letter misspelling.

    Exact after normalisation, or, for longer names, close enough to read as a
    typo ("vaswany" for "vaswani", ratio 0.86). Short surnames must match
    exactly: "li" / "liu", "chen" / "cheng" and "huang" / "hung" are different
    people who all score 0.8 or more.
    """
    if want == got:
        return True
    if min(len(want), len(got)) < 6:
        return False
    return SequenceMatcher(None, want, got).ratio() >= 0.8


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
            # "issued" is the one date CrossRef guarantees; print/online are
            # often absent (e.g. proceedings papers), which left year=0.
            date_parts = item.get(
                "published-print",
                item.get("published-online", item.get("issued", {})),
            ).get("date-parts", [[0]])
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

def _search_arxiv(query: str, max_results: int = 5, *, title_only: bool = False) -> list[Paper]:
    """Search arXiv API for metadata (title, authors, abstract, arxiv_id)."""
    term = f'ti:"{query}"' if title_only else f"all:{query}"
    encoded = urllib.parse.quote(term)
    url = f"https://export.arxiv.org/api/query?search_query={encoded}&max_results={max_results}"
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

            id_m = re.search(r"<id>https?://arxiv.org/abs/(.*?)</id>", entry)
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
    keep = _KEEP_FIELDS.get(entry_type, _KEEP_FIELDS_DEFAULT) | {"doi"}

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
            if d.dblp_key and _same_paper(paper.title, {"author": " and ".join(paper.authors),
                                                      "year": paper.year, "doi": paper.doi}, [d]):
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

def bib_entry_spans(content: str):
    """Yield complete brace-delimited BibTeX records, including single-line entries."""
    header = re.compile(r"@\w+\s*\{")
    pos = 0
    while match := header.search(content, pos):
        pos = match.end()
        if content[content.rfind("\n", 0, match.start()) + 1:match.start()].lstrip().startswith("%"):
            continue
        closing = find_matching_brace(content, match.end() - 1)
        if closing is not None:
            yield match.start(), closing + 1
            pos = closing + 1


def parse_bib(bib_path: str) -> list[dict]:
    """Parse a .bib file into a list of entry dicts.

    Each dict has: key, entry_type, fields (dict), raw (original text),
    preceding_comments (comment lines above the entry).
    """
    return parse_bib_string(Path(bib_path).read_text(errors="replace"))


def parse_bib_string(content: str) -> list[dict]:
    """Parse an in-memory bibliography with the same fields and comments as a file."""
    entries = []
    for start, end in bib_entry_spans(content):
        raw = content[start:end]
        header = re.match(r"@(\w+)\s*\{\s*([^,\s}]+)\s*,", raw)
        if not header:
            continue
        entry_type_raw = header[1].lower()
        entry_key = header[2]
        body = raw[header.end():-1]

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
            "raw": raw,
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

    # Collect all cited keys from .tex files. Uses the shared CITE_CMD_RE — a
    # narrower regex here deletes entries that ARE cited (see its docstring).
    cited_keys = cited_keys_in_dir(tex_path)

    # `\nocite{*}` pulls the whole bib into the bibliography — nothing is unused.
    if NOCITE_ALL in cited_keys:
        return []

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
            records = parse_bib_string(bib) if bib else []
            if len(records) == 1 and _same_paper(title, fields, [_bib_paper(records[0])]):
                found_in.append("crossref")
                best_bibtex = _strip_bibtex_fields(bib)
            time.sleep(_CROSSREF_DELAY)

        # Search DBLP by title
        dblp_results = _search_dblp(title, max_results=3)
        time.sleep(_DBLP_DELAY)
        dblp_match = None
        for d in dblp_results:
            if _same_paper(title, fields, [d]):
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
                if _same_paper(title, fields, [cr]):
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
            # Tag the entry for downstream tooling (summary PDF, citation
            # audits) by prepending a comment line. Do NOT add a `note`
            # field to the entry itself — most bib styles render `note` in
            # the references list, so a "[NEEDS-CHECK: citation not
            # verified]" note shows up next to the citation in the rendered
            # PDF (and was previously wrapped in \textcolor{red}{} which
            # made it ugly red text). The leading comment is invisible to
            # LaTeX but parseable by us.
            marker = "% [NEEDS-CHECK: citation not verified]\n"
            if marker + r.original_bibtex not in content and not r.original_bibtex.lstrip().startswith("% [NEEDS-CHECK"):
                content = content.replace(
                    r.original_bibtex, marker + r.original_bibtex,
                )

    bib_file.write_text(content)


def replace_unverified_entries(
    bib_path: str, results: list[VerificationResult],
) -> tuple[list[str], list[str]]:
    """Second-chance resolution for NEEDS-CHECK entries.

    verify_bib only consults DBLP + CrossRef; this widens the search to the
    full API set (arXiv + Semantic Scholar included) and, on a confident
    title match, REPLACES the unverified entry in the file with the real
    API-fetched BibTeX re-keyed to the original cite key — so a reference an
    LLM wrote from memory becomes a database-confirmed one. Entries with no
    title, or that no API can confirm, are left untouched and returned as
    unresolved for the caller to decide (drop or keep marked).

    Returns (replaced_keys, unresolved_keys).
    """
    bib_file = Path(bib_path)
    if not bib_file.exists():
        return [], []
    content = bib_file.read_text(errors="replace")
    replaced, unresolved = [], []
    for r in results:
        if r.status != "NEEDS-CHECK":
            continue
        try:
            parsed = parse_bib_string(r.original_bibtex)
            fields = parsed[0]["fields"] if parsed else {}
            title = (fields.get("title") or "").strip()
            if not title:
                unresolved.append(r.entry_key)
                continue
            match = _same_paper(title, fields, search_papers(title, max_results=10))
            bib = fetch_bibtex(match) if match else None
            if not bib:
                unresolved.append(r.entry_key)
                continue
            # Re-key to the ORIGINAL cite key so existing \cite commands resolve.
            bib = re.sub(r"(@\w+\s*\{)\s*[^,]+,", r"\1" + r.entry_key + ",", bib, count=1)
            tagged = f"% {_ARK_SOURCE_TAG}verified-replace]\n{bib.strip()}"
            # fix_bib has already prepended the NEEDS-CHECK marker — swap the
            # marked block if present, else the bare entry.
            marked = "% [NEEDS-CHECK: citation not verified]\n" + r.original_bibtex
            old = marked if marked in content else r.original_bibtex
            content = content.replace(old, tagged)
            replaced.append(r.entry_key)
        except Exception:
            unresolved.append(r.entry_key)
    if replaced:
        bib_file.write_text(content)
    return replaced, unresolved


def _as_year(value) -> int:
    """A 4-digit year from whatever the field happens to hold, else 0."""
    m = re.search(r"(1[89]|20)\d{2}", str(value or ""))
    return int(m.group()) if m else 0


def _doi_id(value) -> str:
    """Accept a standalone DOI or resolver URL, never a substring of a claim."""
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value or "").strip(), flags=re.I)
    return value.lower() if re.fullmatch(r"10\.\d{4,9}/[^\s<>]+", value, re.I) else ""


def _bib_paper(entry: dict) -> Paper:
    f = entry["fields"]
    return Paper(title=f.get("title", ""), authors=f.get("author", "").split(" and "),
                 year=_as_year(f.get("year")), doi=_doi_id(f.get("doi")) or None,
                 arxiv_id=f.get("eprint"),
                 venue=next((f[k] for k in ("journal", "booktitle", "publisher") if f.get(k)), ""),
                 bibtex=entry["raw"])


def _same_paper(title: str, fields: dict, candidates) -> "Paper | None":
    """Pick the candidate that is the SAME PAPER, not merely the same title.

    Title similarity alone is not identity, and treating it as identity is how
    a correct citation gets swapped for a wrong one. Measured on DBLP: a search
    for "Attention Is All You Need" returns several distinct papers that reuse
    the phrase verbatim, and the 2017 original does not appear until well past
    the tenth hit. First-above-threshold therefore replaced a Vaswani 2017
    reference with an unrelated recent paper — a silent corruption, because the
    result is a real, database-confirmed entry that simply is not the work the
    author cited.

    The entry we are replacing already carries a year and an author list. Use
    them: require agreement where the field exists. Author agreement survives
    encoding differences (BibTeX accents, DBLP homonym numbers) and a one-letter
    misspelling of the surname, but not a different person. When nothing
    agrees we return None and the caller leaves the entry unresolved — a marked
    or dropped citation is recoverable, a confidently wrong one is not.
    """
    want_doi = _doi_id(fields.get("doi"))
    want_year = _as_year(fields.get("year"))
    raw_authors = (fields.get("author") or "").split(" and ")
    want_author = _first_author_surname([a.strip() for a in raw_authors if a.strip()])
    # Veto on CONTRADICTION, never on absence. arXiv and Semantic Scholar both
    # return records with no year or no author list, and treating a missing
    # field as disagreement would reject the very sources this second pass was
    # widened to reach. A field only counts against a candidate when both sides
    # state it and they disagree.
    fallback = None
    for p in candidates:
        if want_doi and p.doi and _doi_id(p.doi) != want_doi:
            continue
        if title_similarity(title, p.title) < _SIMILARITY_THRESHOLD:
            continue
        # Preprint and proceedings years routinely differ by one. The APIs are
        # not disciplined about this field — it arrives as None, "2017", or
        # "2017-06" depending on the source — so coerce rather than trust it.
        got_year = _as_year(getattr(p, "year", None))
        got_author = _first_author_surname(getattr(p, "authors", None) or [])
        if want_year and got_year and abs(got_year - want_year) > 1:
            continue
        if want_author and got_author and not _same_author(want_author, got_author):
            continue
        # Positive agreement on both wins outright; anything merely
        # uncontradicted is held as a fallback behind it.
        if want_year and got_year and want_author and got_author:
            return p
        if fallback is None:
            fallback = p
    return fallback


def remove_entries(bib_path: str, keys: list[str]) -> list[str]:
    """Remove specific entries (and their preceding comment lines, e.g. the
    NEEDS-CHECK marker) from references.bib. Returns the keys actually removed.
    """
    bib_file = Path(bib_path)
    if not bib_file.exists() or not keys:
        return []
    want = set(keys)
    content = bib_file.read_text(errors="replace")
    removed = []
    for entry in parse_bib(bib_path):
        if entry["key"] not in want:
            continue
        to_remove = entry["raw"]
        if entry["preceding_comments"]:
            to_remove = entry["preceding_comments"] + "\n" + to_remove
        if to_remove in content:
            content = content.replace(to_remove, "")
        else:
            content = content.replace(entry["raw"], "")
        removed.append(entry["key"])
    if removed:
        content = re.sub(r"\n{3,}", "\n\n", content)
        bib_file.write_text(content)
    return removed


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

        # Build entry with whatever info we have. Placeholder values like
        # "Unknown" / "TBD" / "Anonymous" are worse than silence — the
        # reviewer treats them as an unfinished-manuscript signal and will
        # surface them as a submission-blocking issue. Skip them.
        if author.strip().lower() in {"", "unknown", "tbd", "anonymous", "anon", "n/a", "na"}:
            author = ""
        fields = [f"  title = {{{title}}}"]
        if author:
            fields.append(f"  author = {{{author} et al.}}")
        if year:
            fields.append(f"  year = {{{year}}}")
        # No `note` field with red markup — the comment line above the
        # entry is the marker; downstream tooling (summary PDF) reads it.

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
                paper = _search_by_title(title, {"author": ref.get("authors"),
                                                 "year": ref.get("year"), "doi": ref.get("doi")})
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
        # See above: no `note` field with red markup. The leading
        # comment line is the marker.

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

    refs = [r for r in data["references"] if isinstance(r, dict)]
    existing_titles = {r.get("title", "").lower() for r in refs}
    existing_keys = {r.get("bibtex_key") for r in refs}

    for idx, (paper, key) in enumerate(zip(papers, cite_keys)):
        if key in existing_keys or paper.title.lower() in existing_titles:
            continue
        ctx = (contexts[idx] if contexts and idx < len(contexts) else "") or ""
        entry = {
            "title": paper.title,
            "authors": ", ".join(paper.authors[:3]) + (" et al." if len(paper.authors) > 3 else ""),
            "year": paper.year,
            "venue": paper.venue,
            "bibtex_key": key,
            "source": (paper.source if paper.source == "existing bibliography"
                       else f"API ({', '.join(paper.confirmed_by)})"),
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


def _cited_work(title: str, fields: dict, candidates: list[Paper]) -> Paper | None:
    """Require affirmative metadata for identity fields supplied by the researcher."""
    candidates = [c for c in candidates if
                  (not fields.get("author") or _first_author_surname(c.authors))
                  and (not fields.get("year") or _as_year(c.year))
                  and (not fields.get("doi") or _doi_id(c.doi) == _doi_id(fields["doi"]))]
    if not any(fields.get(k) for k in ("author", "year", "doi")):
        candidates = [c for c in candidates if title_similarity(title, c.title) >= 0.97]
    return _same_paper(title, fields, candidates)


def _resolve_bootstrap_paper(title: str, fields: dict, query: str) -> Paper | None:
    if fields["doi"]:
        raw = _fetch_bibtex_from_doi(fields["doi"])
        records = parse_bib_string(raw) if raw else []
        if len(records) == 1:
            candidate = _bib_paper(records[0])
            if _cited_work(title, fields, [candidate]):
                candidate.source, candidate.confirmed_by = "crossref", ["crossref"]
                candidate.bibtex = _strip_bibtex_fields(raw)
                return candidate
    for term in dict.fromkeys((title, query, _extract_keywords_from_title(title))):
        if term:
            candidate = _search_by_title(title, fields, term)
            if candidate:
                if not candidate.abstract and candidate.source in ("dblp", "crossref"):
                    _supplement_abstract(candidate)
                return candidate
    return None


def _previous_bootstrap_reference(references, by_key, title, fields, paper):
    matches = []
    for ref in references:
        entry = by_key.get(ref.get("bibtex_key"))
        if not entry or not _is_ark_managed(entry):
            continue
        if ref.get("requested_title") == title and _same_paper(title, fields, [_bib_paper(entry)]):
            matches.append(ref)
            continue
        # Legacy rows did not store the requested identity. Only a single
        # labeled DOI in that row's claim can corroborate a separately
        # resolved publication. Never use prose as the lookup identifier.
        context = ref.get("context") or ""
        if (not ref.get("requested_title") and paper and paper.doi
                and title_similarity(title, ref.get("title", "")) >= _SIMILARITY_THRESHOLD):
            mentioned = re.findall(r"\b10\.\d{4,9}/[^\s)\],]+", context, re.I)
            if (len(mentioned) == 1 and _doi_id(mentioned[0]) == _doi_id(paper.doi)
                    and re.search(r"\bDOI:\s*" + re.escape(mentioned[0]), context, re.I)):
                matches.append(ref)
    return matches[0] if len(matches) == 1 else None


def bootstrap_citations(titles: list[str], bib_path: str, literature_path: str,
                        search_queries: list[str] = None, authors: list[str] = None,
                        years: list = None, contexts: list[str] = None,
                        dois: list[str] = None) -> BootstrapResult:
    """Resolve each extracted work independently, keeping its requested identity and claim together."""
    import yaml
    from datetime import date
    from ark.orchestrator.state import _atomic_write_text

    bib_file, lit_file = Path(bib_path), Path(literature_path)
    bib = bib_file.read_text() if bib_file.is_file() else "% ARK auto-managed references\n"
    literature = (yaml.safe_load(lit_file.read_text()) or {}) if lit_file.is_file() else {}
    references = literature.setdefault("references", [])
    unverified = literature.setdefault("needs_check", [])
    found, keys, missing = [], [], []

    for i, original_title in enumerate(titles):
        title = original_title.strip()
        if not title:
            continue

        def value(items, default=""):
            return items[i] if items and i < len(items) and items[i] is not None else default

        fields = {"author": value(authors), "year": _as_year(value(years)), "doi": _doi_id(value(dois))}
        context = value(contexts)
        entries = parse_bib_string(bib)
        by_key = {e["key"]: e for e in entries}
        local = None
        previous = None
        # A placeholder's key can already be cited by the manuscript.
        for row in unverified:
            if (_normalize_title(row.get("title", "")) == _normalize_title(title)
                    and _same_paper(title, fields, [Paper(title, [row.get("authors", "")], row.get("year"))])):
                previous = row
                break
        for entry in entries:
            paper = _bib_paper(entry)
            if (_normalize_title(title) == _normalize_title(paper.title)
                    and _first_author_surname([fields["author"]])
                    and _as_year(fields["year"]) and (paper.venue or paper.doi or paper.arxiv_id)
                    and _first_author_surname(paper.authors) and paper.year
                    and "[NEEDS-CHECK]" not in entry["preceding_comments"]
                    and (not fields["doi"] or fields["doi"] == paper.doi)
                    and _same_paper(title, fields, [paper])):
                paper.source = "existing bibliography"
                local = (entry["key"], paper)
                break

        paper = local[1] if local else _resolve_bootstrap_paper(title, fields, value(search_queries))
        # Verify what will actually be printed. A metadata match can be followed
        # by a BibTeX fetch returning an unrelated edition, author, or software.
        raw = (paper.bibtex or fetch_bibtex(paper)) if paper else None
        records = parse_bib_string(raw) if raw else []
        if len(records) != 1 or not _cited_work(title, fields, [_bib_paper(records[0])]):
            paper = None

        if not previous:
            previous = _previous_bootstrap_reference(references, by_key, title, fields, paper)

        key = (previous or {}).get("bibtex_key") or (local[0] if local else None)
        if not key and paper:
            key = records[0]["key"]
        if not key:
            key = "".join(re.findall(r"\w+", title.lower())[:3]) or "source"
        entry = by_key.get(key)
        if entry and not previous and not local and not _same_paper(title, fields, [_bib_paper(entry)]):
            base = key
            suffix = 2
            while key in by_key:
                key = f"{base}{suffix}"
                suffix += 1
            entry = None

        if paper:
            source = f"API ({', '.join(paper.confirmed_by)})" if not local else "existing bibliography"
            raw = re.sub(r"(@\w+\s*\{\s*)[^,]+", lambda m: m[1] + key, raw.strip(), count=1)
            paper.bibtex = raw
            record = {"title": paper.title, "authors": ", ".join(paper.authors), "year": paper.year,
                      "venue": paper.venue, "bibtex_key": key, "bibtex": raw, "source": source,
                      "requested_title": title}
            if paper.doi:
                record["doi"] = paper.doi
            if paper.arxiv_id:
                record["arxiv_id"] = paper.arxiv_id
            if paper.abstract:
                record["abstract"] = paper.abstract[:500]
            tag = f"% [ARK:source={paper.source or 'verified'}]"
            found.append(paper)
            keys.append(key)
        else:
            record = {"title": title, "authors": fields["author"], "year": fields["year"],
                      "bibtex_key": key, "status": "[NEEDS-CHECK]", "reason": "Source identity unconfirmed"}
            raw = (f"@misc{{{key},\n  title = {{{title}}},\n"
                   + (f"  author = {{{fields['author']} et al.}},\n" if fields["author"] else "")
                   + (f"  year = {{{fields['year']}}},\n" if fields["year"] else "") + "}")
            tag = "% [NEEDS-CHECK] Source identity unconfirmed"
            missing.append(title)
        record["added_date"] = (previous or {}).get("added_date") or date.today().isoformat()
        if context:
            record["context"] = context
            if any(word in context.lower() for word in ("must cite", "essential", "critical", "closest prior work")):
                record["importance"] = "critical"
        existing = next((r for r in references if r.get("bibtex_key") == key), None)
        if local and existing:
            # Preserve upstream provenance and any other curated fields on reuse.
            record = {**existing, **record, "source": existing.get("source") or record["source"],
                      "added_date": existing.get("added_date") or record["added_date"]}
        for group in (references, unverified):
            group[:] = [row for row in group if row.get("bibtex_key") != key]
        (references if paper else unverified).append(record)
        if not local or (previous and entry and entry["raw"] != raw):
            if entry:
                # Replace only the ARK marker directly attached to this record.
                pattern = r"(?:^% \[(?:ARK:source=|NEEDS-CHECK)[^\n]*\n)*" + re.escape(entry["raw"])
                bib = re.sub(pattern, lambda _: tag + "\n" + raw, bib, count=1, flags=re.M)
            else:
                bib += "\n" + tag + "\n" + raw + "\n"

    if titles:
        if not bib_file.is_file() or bib != bib_file.read_text():
            _atomic_write_text(bib_file, bib)
        _atomic_write_text(lit_file, yaml.safe_dump(literature, allow_unicode=True, sort_keys=False))
    return BootstrapResult(found=found, found_keys=keys, needs_check=missing)


def _search_by_title(title: str, fields: dict | None = None, query: str = "") -> Paper | None:
    """Use the query to retrieve candidates; verify against the requested work."""
    fields = fields or {}
    query = query or title
    for search, delay in ((_search_dblp, _DBLP_DELAY), (_search_crossref, _CROSSREF_DELAY),
                          (_search_semantic_scholar, 0)):
        candidates = search(query, max_results=6)
        if delay:
            time.sleep(delay)
        candidates.sort(key=lambda paper: (not _is_published(paper), -title_similarity(title, paper.title)))
        candidate = _cited_work(title, fields, candidates)
        if candidate:
            return candidate
    # arXiv has papers that Crossref does not index. The title prefix survives
    # punctuation and hyphen changes between preprint and proceedings titles.
    if query == title:
        prefix = " ".join(_normalize_title(title).split()[:4])
        return _cited_work(title, fields, _search_arxiv(prefix, max_results=8, title_only=True))
    return None


def _supplement_abstract(paper: Paper) -> None:
    """Try to get abstract from Semantic Scholar for a paper that lacks one."""
    s2_results = _search_semantic_scholar(paper.title, max_results=1)
    if s2_results and _same_paper(paper.title, {"author": " and ".join(paper.authors),
                                                 "year": paper.year, "doi": paper.doi}, [s2_results[0]]):
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

def active_tex_sources(tex_dir: Path):
    """Walk local TeX inputs reachable from the manuscript, ignoring comments and unused drafts."""
    root = Path(tex_dir).resolve()
    queue = [root / "main.tex"]
    seen = set()
    while queue:
        file = queue.pop()
        if file in seen or not file.is_relative_to(root) or not file.is_file():
            continue
        seen.add(file)
        source = re.sub(r"(?m)(?<!\\)%.*$", "", file.read_text(errors="replace"))
        yield file, source
        for name in re.findall(r"\\(?:input|include|subfile)\s*\{([^{}]+)\}", source):
            rel = Path(name)
            if not rel.suffix:
                rel = rel.with_suffix(".tex")
            candidate = root / rel
            if not candidate.is_file():
                candidate = file.parent / rel
            queue.append(candidate.resolve())


def unconfirmed_citations(tex_dir: Path) -> list[str]:
    """Identify actually cited bibliography entries explicitly marked unconfirmed."""
    bib = Path(tex_dir) / "references.bib"
    if not bib.is_file():
        return []
    cited = {key for _, source in active_tex_sources(tex_dir) for key in cited_keys_in_tex(source)}
    missing = []
    for entry in parse_bib(str(bib)):
        tags = re.findall(r"(?m)^%\s*\[(NEEDS-CHECK|ARK:source=[^\]]+)\]", entry.get("preceding_comments", ""))
        if entry["key"] in cited and tags and tags[-1] == "NEEDS-CHECK":
            missing.append(entry["key"])
    return sorted(missing)


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
