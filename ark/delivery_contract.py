"""Delivery contract: read-only acceptance checks over a project's final paper.

One source of truth for "what a deliverable paper must look like", consumed by
three callers with zero logic duplication:

  1. the pipeline's pre-delivery step and its completion decision;
  2. ``ark audit`` (batch verification over historical projects, with
     ``--repair`` for the mechanical fixes);
  3. dashboards/ops reading the persisted ``delivery_report.yaml``.

Design rules:
  - every check is PURE and read-only over the project dir (PDF + tex + bib);
  - checks never raise: an unreadable artifact is itself a finding;
  - severity ``hard`` = must-hold invariant (disclosure, resolved citations);
    ``soft`` = quality signal (page budget, figure usage).

Born 2026-07-12 after a week where each of these invariants was violated in
production and discovered only by manual audit (7 papers without the required
disclosure, a paper shipped with 17 unresolved citations and an empty
references.bib, figures generated but never referenced, stub runs marked done).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import yaml

# The one required tooling acknowledgment (see template_preprocess.ARK_ACK_TEXT).
# Matching is on the stable marker, tolerant of legacy wording ("used ARK").
DISCLOSURE_MARKER = "idea2paper.org"

# Words that must not appear inside the Acknowledgments block — internal stack
# names leak implementation details and read as pseudo-grant credits
# (project 472e4874 shipped "supported by the OpenHands AI research platform").
_STACK_LEAK_RE = re.compile(
    r"OpenHands|OpenRouter|LiteLLM|Idea2Paper platform credits|Claude[ ~]|GPT-|DeepSeek",
    re.IGNORECASE)

# natbib/plain render an unresolved \cite as a bare "?" — English prose never
# puts one between spaces/brackets (same pattern as compiler.ensure_resolved_citations).
_UNRESOLVED_CITE_RE = re.compile(r"[\s(\[]\?[\s,.;)\]]")

_LABEL_RE = re.compile(r"\\label\{([^}]*)\}")
_FLOAT_RE = re.compile(r"\\begin\{(figure|table)\*?\}(.*?)\\end\{\1\*?\}", re.S)
_GRAPHIC_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}")


def _removals_the_review_asked_for(before: str, after: str, review: str) -> dict:
    """Floats this edit removed that the review itself named, by label.

    The point of ``no_loss_since_review`` is to catch deletions nobody
    reviewed. A reviewer that says "Remove Figure 2" and names
    ``paper/figures/fig_ablation.pdf`` has reviewed that one, so reporting the
    writer for obeying is noise — and noise is how a real loss gets waved
    through. Measured on a cheap-test run: one violation, and it was the
    writer doing exactly what issue M1 asked.

    The match is on an exact identifier: the label, or a graphics file inside
    the same float. Never on the review's prose, which would make this a guess.
    Returns ``{label: {"figures": n, "tables": n}}`` for the removed floats the
    review named, so the caller can subtract what it explains from the counts.
    """
    if not review:
        return {}
    gone = set(_LABEL_RE.findall(before)) - set(_LABEL_RE.findall(after))
    if not gone:
        return {}
    explained = {}
    for kind, body in ((m.group(1), m.group(2)) for m in _FLOAT_RE.finditer(before)):
        labels = [lab for lab in _LABEL_RE.findall(body) if lab in gone]
        if not labels:
            continue
        graphics = _GRAPHIC_RE.findall(body)
        names = set(labels) | {Path(g).name for g in graphics} | {Path(g).stem for g in graphics}
        # On a whole word, not a substring. `n in review` let a figure named
        # `figures/overview.pdf` be excused by any review sentence containing
        # "overview", and a short stem by any longer word containing it —
        # while the docstring promises a match on an exact identifier and
        # "never on the review's prose".
        if not any(n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", review)
                   for n in names):
            continue
        for lab in labels:
            explained[lab] = {"figures": len(graphics) if kind == "figure" else 0,
                              "tables": 1 if kind == "table" else 0}
    return explained

MIN_PDF_BYTES = 50_000       # below this a "paper" is a stub/template skeleton
# Bytes alone cannot see a skeleton: acmart embeds its fonts, so a document
# with nothing but section headings still weighs 221 KB and sailed past the
# byte floor. Measured on three cheap-test runs — two finished papers at 6553
# and 6942 extractable characters, one skeleton a rate limit cut short at 473.
MIN_PDF_TEXT_CHARS = 1_500
MIN_FIG_BYTES = 5_000        # ignore icons/empty plot files


@dataclass
class Finding:
    check: str
    status: str              # "pass" | "violated" | "skipped"
    severity: str            # "hard" | "soft"
    detail: str = ""


@dataclass
class DeliveryReport:
    findings: list = field(default_factory=list)

    @property
    def violations(self) -> list:
        return [f for f in self.findings if f.status == "violated"]

    @property
    def hard_violations(self) -> list:
        return [f for f in self.violations if f.severity == "hard"]

    def delivery_blockers(self, config: dict) -> list[Finding]:
        """Failures confirmed on the artifact; heuristic warnings remain visible in the report."""
        required = {"pdf_exists", "pdf_readable", "author_metadata", "disclosure_present",
                    "citations_resolved", "cross_references_resolved"}
        if config.get("layout_mode") == "strict":
            required.update({"page_budget", "cited_sources_verified", "column_overflow"})
        if config.get("figure_generation") == "nano_banana":
            required.add("concept_figure_embedded")
        if config.get("skip_deep_research") is False and (
                config.get("layout_mode") == "strict"
                or config.get("deep_research_backend") in {"gemini", "openrouter"}):
            required.add("research_grounded")
        blockers = [f for f in self.violations if f.check in required]
        if config.get("figure_generation") == "nano_banana":
            blockers.extend(f for f in self.findings if f.check == "concept_figure_science" and f.status != "pass")
        return blockers

    def to_dict(self) -> dict:
        return {"schema_version": 2, "findings": [asdict(f) for f in self.findings],
                "violations": len(self.violations),
                "hard_violations": len(self.hard_violations)}


def _pdf_text(pdf: Path) -> Optional[str]:
    try:
        import fitz
        doc = fitz.open(str(pdf))
        text = "".join(pg.get_text() for pg in doc)
        doc.close()
        return text
    except Exception:
        return None


def _repeated_body_passage(text: str) -> str:
    """Find a long duplicated stretch of rendered prose for editorial review."""
    body = re.split(
        r"(?im)^\s*(?:references|bibliography|appendix|supplementary materials?|参考文献)\s*$",
        text, maxsplit=1,
    )[0]
    # PDF line wrapping can insert discretionary hyphens. Preserve the actual words and numbers.
    body = re.sub(r"(?<=\w)-\s*\n\s*(?=[a-z])", "", body)
    words = re.findall(r"[^\W_]+", body.casefold())
    seen = {}
    width = 80
    for index in range(len(words) - width + 1):
        anchor = tuple(words[index:index + 8])
        old = seen.setdefault(anchor, index)
        if index - old < width or words[old:old + width] != words[index:index + width]:
            continue
        if len({word for word in words[index:index + width] if len(word) >= 3 and word.isalpha()}) >= 24:
            return " ".join(words[index:index + 12])
    return ""


def _page_budget(pdf: Path, tex: str, limit: int, *, strict: bool = False) -> Finding:
    from ark.latex.utils import body_page_boundary
    try:
        import fitz
        with fitz.open(pdf) as doc:
            total = len(doc)
            if not limit:
                return Finding("page_budget", "skipped", "soft", f"No body-page limit set ({total} total pages)")
            boundary = body_page_boundary(doc)
            if boundary is None:
                # Localized sections and included TeX files may lack a visible
                # English heading. Total pages give a safe upper bound only.
                declares_endmatter = re.search(
                    r"\\(?:bibliography\b|printbibliography\b|appendi(?:x|ces)\b"
                    r"|begin\{(?:thebibliography|appendices)\})", tex)
                if declares_endmatter or not tex.strip():
                    status = "pass" if total <= limit else "skipped"
                    return Finding("page_budget", status, "soft",
                                   f"{total} total pages, body limit {limit}. "
                                   "The body/endmatter boundary could not be verified.")
                body = total
            else:
                page_index, body_bottoms = boundary
                body = page_index + int(any(body_bottoms))
        detail = f"{body} body pages / limit {limit} ({total} total pages)"
        if strict and body < limit:
            return Finding("page_budget", "violated", "soft",
                           f"{detail}, short of the requested strict body length.")
        if body > limit:
            over = body - limit
            return Finding("page_budget", "violated", "soft",
                           f"{detail}, {over} page{'s' if over != 1 else ''} over limit.")
        return Finding("page_budget", "pass", "soft", detail)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        return Finding("page_budget", "skipped", "soft", f"Could not measure body pages: {error}")


def _ack_block(tex: str) -> str:
    """The Acknowledgments block from tex source (up to the next section/bib)."""
    m = re.search(
        r"\\section\*?\{Acknowledg[^}]*\}([\s\S]*?)(?=\\section|\\bibliography|\\begin\{thebibliography\}|\\end\{document\}|$)",
        tex)
    return m.group(1) if m else ""


def cross_reference_warning(final_log: str) -> str:
    """Describe undefined references and ambiguous labels after the final TeX pass."""
    problems = []
    warnings = re.findall(
        r"^LaTeX Warning: Reference [`']([^'\n]+)'\s+on page (\S+)\s+undefined"
        r"(?:\s+on\s+input\s+line\s+(\d+))?", final_log, flags=re.M)
    if warnings:
        locations = list(dict.fromkeys(
            f"{label} (page {page}" + (f", input line {line})" if line else ")")
            for label, page, line in warnings
        ))
        problems.append("Unresolved LaTeX cross-reference: " + ", ".join(locations[:8]) +
                        ". Check the reference and the label on the intended table, figure or section, then recompile.")
    elif "LaTeX Warning: There were undefined references." in final_log:
        problems.append("LaTeX reports unresolved references in the final pass. Inspect main.log, fix the references and recompile.")
    repeated = dict.fromkeys(re.findall(r"^LaTeX Warning: Label [`']([^'\n]+)' multiply defined\.", final_log, re.M))
    if repeated:
        problems.append("Multiply defined LaTeX labels: " + ", ".join(list(repeated)[:8]) +
                        ". Give each referenced object a unique label and recompile.")
    elif "LaTeX Warning: There were multiply-defined labels." in final_log:
        problems.append("LaTeX reports multiply-defined labels in the final pass. Correct the label collisions and recompile.")
    return " ".join(problems)


def _dangling_refs(tex: str) -> set:
    labels = set(re.findall(r"\\label\{([^}]*)\}", tex))
    refs = {k.strip() for m in re.findall(r"\\(?:ref|eqref|autoref|cref|Cref|pageref)\{([^}]*)\}", tex)
            for k in m.split(",")}
    return refs - labels


def _concept_figure_in_pdf(pdf: Path, figures_dir: Path, *, semantic_state: Path | None = None) -> Finding:
    """Match the registered concept art to pixels actually painted in the paper."""
    import fitz

    from ark.figure_manifest import AI_SOURCES, load_manifest

    from ark.latex.utils import body_page_boundary

    check = "concept_figure_science" if semantic_state is not None else "concept_figure_embedded"
    figures = load_manifest(figures_dir, infer_existing=False).get("figures", {})
    sources = [figures_dir / name for name, info in figures.items()
               if isinstance(info, dict) and info.get("source") in AI_SOURCES
               and Path(name).name == name]
    if not sources:
        return Finding(check, "skipped" if semantic_state is not None else "violated", "soft",
                       "No concept artwork registered for the selected figure mode")
    try:
        with fitz.open(pdf) as doc:
            boundary = body_page_boundary(doc)
            pages = {}
            for page in doc:
                if boundary and page.number > boundary[0]:
                    break
                for image in page.get_image_info(hashes=True):
                    if boundary and page.number == boundary[0]:
                        col = int(image["bbox"][0] >= page.rect.width / 2)
                        if image["bbox"][3] > boundary[1][col]:
                            continue
                    pages.setdefault(image["digest"], page.number + 1)
    except (OSError, RuntimeError, ValueError) as error:
        return Finding(check, "skipped", "soft", f"Could not inspect PDF images: {error}")
    mismatched = []
    inspected, pending = [], []
    specs = {}
    if semantic_state is not None:
        from ark.nano_banana import concept_design, SEMANTIC_REVIEW_VERSION, generated_result_conflict, semantic_verdict
        try:
            specs = {row["name"]: row for row in json.loads((figures_dir / "concept_figures.json").read_text())
                     if isinstance(row, dict) and isinstance(row.get("name"), str)}
        except (ValueError, OSError, TypeError):
            pass
    for source in sources:
        try:
            digest = figures[source.name].get("artifact_sha256")
            if digest and hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                mismatched.append(source.name)
                continue
            pixels = fitz.Pixmap(str(source))
            if pixels.alpha:
                pixels = fitz.Pixmap(pixels, 0)
        except (OSError, RuntimeError, ValueError):
            continue
        page_number = pages.get(pixels.digest)
        if page_number:
            if semantic_state is None:
                return Finding(check, "pass", "soft", f"{source.name} matches PDF pixels on page {page_number}")
            review = figures[source.name].get("semantic_review") or {}
            spec = specs.get(source.stem)
            if (review.get("artifact_sha256") == hashlib.sha256(source.read_bytes()).hexdigest()
                    and (conflict := generated_result_conflict(review.get("checks")))):
                return Finding(check, "violated", "soft", f"{source.name} on page {page_number}: {conflict} "
                               "Use the worker measurements for a separate results plot.")
            bound = (spec and review.get("version") == SEMANTIC_REVIEW_VERSION
                     and review.get("artifact_sha256") == hashlib.sha256(source.read_bytes()).hexdigest()
                     and review.get("design_sha256") == hashlib.sha256(concept_design(spec, semantic_state).encode()).hexdigest())
            if bound and figures[source.name].get("representation") == "subject_with_comparison_panel":
                raw = Path(figures[source.name].get("illustration_file") or "")
                root = (figures_dir / ".concept_sources").resolve()
                location = (figures_dir / raw).resolve()
                bound = (location.is_relative_to(root) and location.is_file() and
                         hashlib.sha256(location.read_bytes()).hexdigest() == figures[source.name].get("payload_sha256"))
            if not bound:
                pending.append(f"{source.name}: final review missing or no longer matches image/design")
            elif review.get("status") == "contradiction":
                conflicts = (review.get("label_verification") or {}).get("conflicts") or []
                if conflicts:
                    item = conflicts[0]
                    return Finding(check, "violated", "soft", f"{source.name}: printed label {item.get('read_label')!r} "
                                   f"disagrees with the reviewed transcription {item.get('model_label')!r}")
                return Finding(check, "violated", "soft", f"{source.name}: the final image contradicts the study design")
            elif review.get("status") == "passed" and semantic_verdict(review.get("checks")) != "passed":
                pending.append(f"{source.name}: final visual check records incomplete")
            elif review.get("status") != "passed":
                pending.append(f"{source.name}: final visual check {review.get('status', 'unavailable')}")
            else:
                coverage = (review.get("label_verification") or {}).get("coverage", "not recorded")
                inspected.append(f"{source.name} (independent label check: {coverage})")
    if semantic_state is not None:
        if pending or not inspected:
            return Finding(check, "skipped", "soft", "; ".join(pending) or "No verified final artwork to assess")
        return Finding(check, "pass", "soft", "Final image and study design reviewed: " + ", ".join(inspected))
    detail = "Could not match registered concept artwork to an image in the body of the PDF"
    if mismatched:
        detail += ". Files changed since generation: " + ", ".join(mismatched)
    return Finding(check, "violated", "soft", detail)


def evaluate(project_dir: Path, venue_pages: int = 0, *, config: Optional[dict] = None) -> DeliveryReport:
    """Run every acceptance check over the project's final artifacts."""
    project_dir = Path(project_dir)
    if config is None:
        try:
            config = yaml.safe_load((project_dir / "config.yaml").read_text()) or {}
        except (OSError, yaml.YAMLError):
            config = {}
        if not isinstance(config, dict):
            config = {}
    paper_dir = project_dir / (config.get("latex_dir") or "paper")
    pdf = paper_dir / "main.pdf"
    tex_path = paper_dir / "main.tex"
    rep = DeliveryReport()

    # ── artifact existence ────────────────────────────────────────────────
    if not pdf.exists() or pdf.stat().st_size < MIN_PDF_BYTES:
        rep.findings.append(Finding(
            "pdf_exists", "violated", "hard",
            f"main.pdf missing or stub ({pdf.stat().st_size if pdf.exists() else 0} bytes)"))
        return rep  # nothing else is checkable
    text = _pdf_text(pdf)
    tex = tex_path.read_text(errors="replace") if tex_path.is_file() else ""
    if text is None:
        rep.findings.append(Finding("pdf_exists", "pass", "hard"))
        rep.findings.append(Finding("pdf_readable", "violated", "hard",
                                    "PDF could not be parsed"))
        return rep
    if len(text.strip()) < MIN_PDF_TEXT_CHARS:
        rep.findings.append(Finding(
            "pdf_exists", "violated", "hard",
            f"main.pdf is a template skeleton: {len(text.strip())} characters of "
            f"text in {pdf.stat().st_size} bytes"))
        return rep
    rep.findings.append(Finding("pdf_exists", "pass", "hard"))
    if not tex.strip():
        rep.findings.append(Finding("source_exists", "violated", "hard",
                                    "paper/main.tex is missing or empty"))

    # ── required disclosure ───────────────────────────────────────────────
    # Whitespace-normalized haystack: PDF extraction inserts line breaks that
    # can split the marker (e.g. "idea2pap\ner.org").
    text_nows = re.sub(r"\s+", "", text)
    if DISCLOSURE_MARKER in text_nows:
        rep.findings.append(Finding("disclosure_present", "pass", "hard"))
    else:
        rep.findings.append(Finding(
            "disclosure_present", "violated", "hard",
            "required Idea2Paper disclosure absent from the rendered PDF"))

    # ICML emits these diagnostics as ordinary footnote text; TeX succeeds.
    if "AUTHORERR:" in text_nows:
        rep.findings.append(Finding(
            "author_metadata", "violated", "hard",
            "ICML printed AUTHORERR in the PDF. Check the author, affiliation "
            "and corresponding-author declarations in main.tex."))
    else:
        rep.findings.append(Finding("author_metadata", "pass", "hard"))

    log = paper_dir / "main.log"
    if log.is_file():
        log_text = log.read_text(errors="replace")
        cross_ref = cross_reference_warning(log_text)
        rep.findings.append(Finding("cross_references_resolved", "violated" if cross_ref else "pass", "hard", cross_ref))
        sizes = [float(pt) for pt in re.findall(r"Overfull \\hbox \(([0-9.]+)pt too wide\)", log_text)]
        over = [pt for pt in sizes if pt > 3]
        rep.findings.append(Finding("column_overflow", "violated" if over else "pass", "soft",
            f"{len(over)} boxes extend more than 3 pt beyond a column; largest {max(over):.2f} pt."
            if over else "No significant column overflow in the final build log."))

    if config.get("layout_mode") == "strict":
        repeated = _repeated_body_passage(text)
        if repeated:
            rep.findings.append(Finding(
                "body_prose_repeated", "violated", "soft",
                f'Two body passages share at least 80 consecutive words starting with "{repeated}". '
                "Check whether both occurrences serve a distinct purpose and revise redundant text."
            ))

        emphasis = re.search(r"\*\*([^*\n]{2,90})\*\*", text)
        if emphasis and re.search(r"[^\W\d_]+\s+[^\W\d_]+", emphasis[1]):
            rep.findings.append(Finding(
                "markdown_emphasis", "violated", "soft",
                f'The rendered PDF contains literal "{emphasis[0]}". Use LaTeX emphasis if this is running prose.'
            ))

    # ── citations resolved ────────────────────────────────────────────────
    n_unresolved = len(_UNRESOLVED_CITE_RE.findall(text))
    if n_unresolved:
        rep.findings.append(Finding(
            "citations_resolved", "violated", "hard",
            f"{n_unresolved} unresolved '?' citation markers in the PDF"))
    else:
        rep.findings.append(Finding("citations_resolved", "pass", "hard"))

    from ark.citation import unconfirmed_citations
    unconfirmed = unconfirmed_citations(paper_dir)
    rep.findings.append(Finding(
        "cited_sources_verified", "violated" if unconfirmed else "pass", "soft",
        "Cited records still marked unconfirmed: " + ", ".join(unconfirmed)
        if unconfirmed else "No explicitly unconfirmed records cited."))

    # ── references exist when the paper cites ─────────────────────────────
    # Shared extractor: a literal r"\\cite" scan misses biblatex's \parencite /
    # \autocite / \textcite, so an all-biblatex paper counted 0 cites and this
    # hard gate downgraded itself to "skipped" on the papers that needed it.
    from ark.citation import cited_keys_in_tex
    n_cites = len(cited_keys_in_tex(tex))
    bib = paper_dir / "references.bib"
    n_bib = len(re.findall(r"^@", bib.read_text(errors="replace"), re.M)) if bib.exists() else 0
    inline_bib = bool(re.search(r"\\begin\{thebibliography\}", tex))
    if n_cites and not n_bib and not inline_bib:
        rep.findings.append(Finding(
            "references_nonempty", "violated", "hard",
            f"{n_cites} \\cite commands but references.bib has 0 entries"))
    elif n_cites:
        rep.findings.append(Finding("references_nonempty", "pass", "hard",
                                    f"{n_cites} cites / {n_bib} bib entries"))
    else:
        rep.findings.append(Finding("references_nonempty", "skipped", "hard",
                                    "paper has no citations"))

    # ── literature grounding ──────────────────────────────────────────────
    # Deep Research can fail (flaky provider); the pipeline then falls back to
    # a direct academic search and drops a marker. Surface that here so a
    # thinner foundation is visible in the report and to `ark audit` — it used
    # to pass silently, and a paper written on no literature at all looked
    # exactly like one written on a full survey.
    state = project_dir / "auto_research" / "state"
    from ark.deep_research import research_report_status

    research_status = research_report_status(state)
    if research_status == "degraded":
        marker = state / "research_degraded.txt"
        detail = (marker.read_text(errors="replace").strip()[:200] if marker.is_file()
                  else "Literature assembled from search APIs after Deep Research failed")
        rep.findings.append(Finding("research_grounded", "violated", "soft", detail))
    elif config.get("skip_deep_research"):
        rep.findings.append(Finding("research_grounded", "skipped", "soft",
                                    "Deep Research disabled in project settings"))
    elif research_status in ("verified", "invalid_receipt"):
        matched = research_status == "verified"
        rep.findings.append(Finding(
            "research_grounded", "pass" if matched else "violated", "soft",
            "Google research receipt matches the saved report" if matched
            else "Google research completion receipt or its report digest could not be verified"))
    elif research_status in ("available", "legacy"):
        rep.findings.append(Finding("research_grounded", "pass", "soft",
                                    "Literature report present in project artifacts"))
    else:
        status = "violated" if config.get("skip_deep_research") is False else "skipped"
        rep.findings.append(Finding("research_grounded", status, "soft",
                                    "No literature report available in project artifacts"))

    # ── generated figures actually used ───────────────────────────────────
    figs_dir = project_dir / config["figures_dir"] if config.get("figures_dir") else paper_dir / "figures"
    figs = ([f for f in figs_dir.glob("*")
             if f.suffix in (".png", ".pdf") and f.stat().st_size > MIN_FIG_BYTES]
            if figs_dir.exists() else [])
    # Count DISTINCT figures, not files: the generator emits a .png and a .pdf
    # of the same plot, so a file count doubles every figure and makes any
    # ratio meaningless.
    made = {f.stem for f in figs}
    used = {Path(m).stem for m in
            re.findall(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", tex)}
    n_inc = len(re.findall(r"\\includegraphics", tex))
    unused = sorted(made - used)
    if made and not (made & used):
        rep.findings.append(Finding(
            "figures_referenced", "violated", "soft",
            f"{len(made)} usable figures generated, none referenced in the paper"))
    elif len(made) >= 3 and len(made & used) * 2 < len(made):
        # The old rule only fired at ZERO references, so a paper that generated
        # four figures and shipped one passed cleanly. That is the case that
        # actually occurs: a writing pass deleted three \includegraphics blocks
        # and the contract reported "all checks passed" on a paper missing two
        # thirds of its evidence. Producing a spare figure is normal; leaving
        # most of them on the floor is worth a look.
        rep.findings.append(Finding(
            "figures_referenced", "violated", "soft",
            f"only {len(made & used)} of {len(made)} generated figures are "
            f"referenced; unused: {', '.join(unused)}"))
    else:
        rep.findings.append(Finding("figures_referenced",
                                    "pass" if figs else "skipped", "soft",
                                    f"{len(made)} figures / {n_inc} includegraphics"))

    if config.get("figure_generation") == "nano_banana":
        rep.findings.append(_concept_figure_in_pdf(pdf, figs_dir))
        rep.findings.append(_concept_figure_in_pdf(pdf, figs_dir, semantic_state=state))

    # ── page budget ───────────────────────────────────────────────────────
    rep.findings.append(_page_budget(pdf, tex, venue_pages, strict=config.get("layout_mode") == "strict"))

    # ── vector figures not upscaled at insertion ──────────────────────────
    # matplotlib PDFs are saved at print size; inserting a small one at
    # width=\columnwidth in a single-column template blows it and its fonts
    # up 2x (e4503b8d shipped 6 such figures in TMLR). Compare each inserted
    # PDF's natural width against the template's column width.
    upscaled = []
    try:
        import json as _json
        cfg_p = figs_dir / "figure_config.json"
        col_in = float(((_json.loads(cfg_p.read_text()) or {}).get("geometry") or {})
                       .get("columnwidth_in") or 0) if cfg_p.exists() else 0
        if col_in:
            import fitz
            for m2 in re.finditer(
                    r"\\includegraphics\[[^\]]*width\s*=\s*([0-9.]*)\\(columnwidth|textwidth|linewidth)[^\]]*\]\{([^}]+)\}",
                    tex):
                coef = float(m2.group(1) or 1.0)
                target_w = coef * col_in
                fname = Path(m2.group(3)).name
                fp = figs_dir / fname
                if fp.suffix == ".pdf" and fp.exists():
                    try:
                        d2 = fitz.open(str(fp))
                        nat_w = d2[0].rect.width / 72.0
                        d2.close()
                        if nat_w < 0.75 * target_w:
                            upscaled.append(f"{fname} ({nat_w:.1f}in → {target_w:.1f}in)")
                    except Exception:
                        pass
    except Exception:
        pass
    if upscaled:
        rep.findings.append(Finding(
            "figures_not_upscaled", "violated", "soft",
            f"vector figures enlarged at insertion: {', '.join(upscaled[:4])}"))
    else:
        rep.findings.append(Finding("figures_not_upscaled", "pass", "soft"))

    # ── dense bitmaps not crushed into a column ───────────────────────────
    # A 12.5in multi-panel concept diagram shrunk to a 3.3in column (3.8x)
    # is unreadable (30c70039). Bitmaps are "scalable", but legibility isn't.
    crushed = []
    try:
        if col_in:
            import fitz
            for m3 in re.finditer(
                    r"\\includegraphics\[[^\]]*width\s*=\s*([0-9.]*)\\columnwidth[^\]]*\]\{([^}]+)\}",
                    tex):
                coef3 = float(m3.group(1) or 1.0)
                target_w3 = coef3 * col_in
                fname = Path(m3.group(2)).name
                fp = figs_dir / fname
                if fp.suffix == ".png" and fp.exists():
                    try:
                        d3 = fitz.open(str(fp))
                        nat_w = d3[0].rect.width / 72.0
                        d3.close()
                        if target_w3 and nat_w > 2.5 * target_w3:
                            crushed.append(f"{fname} ({nat_w:.1f}in → {target_w3:.1f}in)")
                    except Exception:
                        pass
    except Exception:
        pass
    if crushed:
        rep.findings.append(Finding(
            "figures_not_crushed", "violated", "soft",
            f"dense bitmaps shrunk beyond legibility: {', '.join(crushed[:4])} — "
            f"use figure* or regenerate simpler"))
    else:
        rep.findings.append(Finding("figures_not_crushed", "pass", "soft"))

    # ── a figure's own labels do not overprint ────────────────────────────
    # Size and placement were already checked; legibility was not. A run
    # delivered two charts reading "RaStandardized" and
    # "PlannAdtualActual max_iter max_iter", and every other figure check
    # passed on them. This runs at pre-delivery, which is the only moment
    # after the last review that still sees the figures the reader will get:
    # the writer's reply to a review comment regenerated both of those 19
    # minutes after the last page image anyone looked at.
    illegible, examined, failed = [], 0, []
    try:
        from ark.figure_legibility import describe
    except Exception as e:                      # the scan never ran at all
        describe, failed = None, [f"import: {e}"]
    if describe is not None:
        for name in sorted({Path(g).name for g in _GRAPHIC_RE.findall(tex)}):
            fp = figs_dir / name
            if fp.suffix != ".pdf":
                # matplotlib writes the same figure as .pdf and .png, and the
                # paper may include either. A raster has no text to measure,
                # so fall back to its vector twin — otherwise including the
                # .png hides the defect completely, which is how a figure
                # whose labels overprint passed this check.
                twin = figs_dir / (fp.stem + ".pdf")
                fp = twin if twin.exists() else fp
            if fp.suffix == ".pdf" and fp.exists():
                # Per figure, not per scan: one unreadable PDF used to abort
                # the loop and leave `illegible` empty, which this check then
                # reported as "pass". That is the same hole 8b380e0 closed in
                # the overlap gate — a check that could not run claiming a
                # clean result.
                try:
                    what = describe(fp)
                except Exception as e:
                    failed.append(f"{name}: {e}")
                    continue
                examined += 1
                if what:
                    illegible.append(f"{name} ({what})")
    if illegible:
        rep.findings.append(Finding(
            "figures_legible", "violated", "soft",
            f"{'; '.join(illegible[:3])} — give the labels room "
            f"(larger figsize, shorter text, or rotate them)"))
    elif failed:
        rep.findings.append(Finding(
            "figures_legible", "violated", "soft",
            f"could not be checked, so this is NOT a clean result: "
            f"{'; '.join(failed[:3])}"
            + (f" ({examined} other figure(s) examined and clean)" if examined else "")))
    else:
        # Say what was measured. "figures_legible: pass" with an empty detail
        # reads as "the figures are legible"; what this earns is narrower, and
        # dsh9 shipped a panel label struck through by a legend handle that
        # every part of it passed.
        rep.findings.append(Finding(
            "figures_legible", "pass" if examined else "skipped", "soft",
            f"{examined} figure(s): no overlapping or clipped labels "
            f"(text-vs-text only — a label struck through by a legend handle, "
            f"data line or marker is not detected)" if examined
            else "no vector figure included to check"))

    # ── nothing lost after the last review ────────────────────────────────
    # The last iteration's edits are never reviewed (8 of 10 production runs
    # shipped such edits). They deleted 527e025d's appendix and left dangling
    # references and missing packages elsewhere.
    reviewed = project_dir / "auto_research" / "state" / "reviewed_main.tex"
    if reviewed.exists() and tex:
        from ark.execution import _structure_lost
        before = reviewed.read_text(errors="replace")
        review_path = project_dir / "auto_research" / "state" / "latest_review.md"
        review = review_path.read_text(errors="replace") if review_path.exists() else ""
        asked = _removals_the_review_asked_for(before, tex, review)
        lost = _structure_lost(before, tex, ignore_labels=set(asked),
                               ignore_counts={
                                   "figures": sum(v["figures"] for v in asked.values()),
                                   "tables": sum(v["tables"] for v in asked.values())})
        grew = len(_dangling_refs(tex)) - len(_dangling_refs(before))
        if grew > 0:
            lost.append(f"{grew} more references to undefined labels")
        if lost:
            rep.findings.append(Finding("no_loss_since_review", "violated", "soft",
                                        "since the last review: " + "; ".join(lost)))
        else:
            detail = ("unchanged since review" if before == tex else
                      "edited after the last review, nothing lost")
            if asked:
                detail += f" (the review asked to remove {', '.join(sorted(asked))})"
            rep.findings.append(Finding("no_loss_since_review", "pass", "soft", detail))

    # ── result numbers come from result files ─────────────────────────────
    # At delivery the evidence is everything results/ holds by then, which in a
    # dense project lets invented numbers match by chance. The check run when
    # the last review started is the stronger one, so both are reported.
    try:
        from ark.provenance import audit, verdict
        _audit = audit(tex, project_dir)
        ok, detail = verdict(_audit)
        # "skipped", not "pass", when the evidence index was cut short: the
        # check did not measure what its name claims, so it must not count
        # toward "all checks passed". It is still not a violation — the clamp
        # may excuse a number, never accuse one.
        rep.findings.append(Finding(
            "numbers_traced",
            "skipped" if _audit.get("unverified") else ("pass" if ok else "violated"),
            "hard", detail))
    except Exception as e:
        rep.findings.append(Finding("numbers_traced", "skipped", "hard", f"audit failed: {e}"))
    at_review = project_dir / "auto_research" / "state" / "provenance_at_review.json"
    if at_review.exists():
        try:
            import json as _json
            rec = _json.loads(at_review.read_text())
            rep.findings.append(Finding("numbers_traced_at_review", "pass" if rec.get("ok") else "violated",
                                        "hard", f"iteration {rec.get('iteration')}: {rec.get('detail', '')}"))
        except Exception:
            pass

    # ── no internal-stack credits in the acknowledgments ─────────────────
    ack = _ack_block(tex)
    m = _STACK_LEAK_RE.search(ack) if ack else None
    if m:
        rep.findings.append(Finding(
            "ack_no_stack_leak", "violated", "hard",
            f"internal tooling credited in Acknowledgments ('{m.group(0)}')"))
    else:
        rep.findings.append(Finding("ack_no_stack_leak", "pass", "hard"))

    return rep


def write_report(project_dir: Path, report: DeliveryReport) -> Optional[Path]:
    """Persist the report for dashboards/ops. Fail-soft."""
    try:
        from ark.orchestrator.state import _atomic_write_yaml
        out = Path(project_dir) / "auto_research" / "state" / "delivery_report.yaml"
        _atomic_write_yaml(out, report.to_dict(), sort_keys=False)
        return out
    except Exception:
        return None


# ── mechanical repair (audit --repair; needs no agent) ──────────────────────

def ensure_disclosure_in_tex(tex_path: Path) -> bool:
    """Insert the required disclosure into main.tex if absent. Returns True if
    the file changed. Mirrors execution._ensure_clearpage_before_bibliography's
    rules: append into an existing Acknowledgments block, else insert a new
    section right before the bibliography."""
    from ark.template_preprocess import ARK_ACK_TEXT
    src = tex_path.read_text(errors="replace")
    if DISCLOSURE_MARKER in src:
        return False
    ack_m = re.search(r"(\\section\*?\{Acknowledg[^}]*\}\s*\n)", src)
    if ack_m:
        insert_at = ack_m.end()
        src = src[:insert_at] + "\n" + ARK_ACK_TEXT + "\n" + src[insert_at:]
    else:
        bib_m = re.search(r"(\\bibliographystyle\{|\\bibliography\{|\\begin\{thebibliography\})", src)
        block = "\\section*{Acknowledgments}\n" + ARK_ACK_TEXT + "\n\n"
        if bib_m:
            src = src[:bib_m.start()] + block + src[bib_m.start():]
        else:
            end_m = re.search(r"\\end\{document\}", src)
            if not end_m:
                return False
            src = src[:end_m.start()] + block + src[end_m.start():]
    tex_path.write_text(src)
    return True
