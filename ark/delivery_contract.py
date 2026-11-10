"""Delivery contract: read-only acceptance checks over a project's final paper.

One source of truth for "what a deliverable paper must look like", consumed by
three callers with zero logic duplication:

  1. the pipeline's pre-delivery step (observe-only in v1: report + log, never
     block — enforcement decisions come after a week of fleet reports);
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

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

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
PAGE_SLACK = 3               # refs/appendix allowance past the venue body budget


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

    def to_dict(self) -> dict:
        return {"findings": [asdict(f) for f in self.findings],
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


def _pdf_pages(pdf: Path) -> int:
    try:
        import fitz
        doc = fitz.open(str(pdf))
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0


def _ack_block(tex: str) -> str:
    """The Acknowledgments block from tex source (up to the next section/bib)."""
    m = re.search(
        r"\\section\*?\{Acknowledg[^}]*\}([\s\S]*?)(?=\\section|\\bibliography|\\begin\{thebibliography\}|\\end\{document\}|$)",
        tex)
    return m.group(1) if m else ""


def _dangling_refs(tex: str) -> set:
    labels = set(re.findall(r"\\label\{([^}]*)\}", tex))
    refs = {k.strip() for m in re.findall(r"\\(?:ref|eqref|autoref|cref|Cref|pageref)\{([^}]*)\}", tex)
            for k in m.split(",")}
    return refs - labels


def evaluate(project_dir: Path, venue_pages: int = 0) -> DeliveryReport:
    """Run every acceptance check over the project's final artifacts."""
    project_dir = Path(project_dir)
    paper_dir = project_dir / "paper"
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
    tex = tex_path.read_text(errors="replace") if tex_path.exists() else ""
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

    # ── citations resolved ────────────────────────────────────────────────
    n_unresolved = len(_UNRESOLVED_CITE_RE.findall(text))
    if n_unresolved:
        rep.findings.append(Finding(
            "citations_resolved", "violated", "hard",
            f"{n_unresolved} unresolved '?' citation markers in the PDF"))
    else:
        rep.findings.append(Finding("citations_resolved", "pass", "hard"))

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
    degraded = project_dir / "auto_research" / "state" / "research_degraded.txt"
    if degraded.exists():
        rep.findings.append(Finding(
            "research_grounded", "violated", "soft",
            degraded.read_text(errors="replace").strip()[:200]))
    else:
        rep.findings.append(Finding("research_grounded", "pass", "soft",
                                    "full research report available"))

    # ── generated figures actually used ───────────────────────────────────
    figs_dir = paper_dir / "figures"
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

    # ── page budget ───────────────────────────────────────────────────────
    npages = _pdf_pages(pdf)
    if venue_pages and npages > venue_pages + PAGE_SLACK:
        rep.findings.append(Finding(
            "page_budget", "violated", "soft",
            f"{npages} pages vs venue budget {venue_pages} (+{PAGE_SLACK} slack)"))
    else:
        rep.findings.append(Finding("page_budget", "pass", "soft",
                                    f"{npages} pages / budget {venue_pages or 'n/a'}"))

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
        ok, detail = verdict(audit(tex, project_dir))
        rep.findings.append(Finding("numbers_traced", "pass" if ok else "violated", "hard", detail))
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
        import yaml
        out = Path(project_dir) / "auto_research" / "state" / "delivery_report.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(report.to_dict(), sort_keys=False))
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
