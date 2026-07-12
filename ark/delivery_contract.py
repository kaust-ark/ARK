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

MIN_PDF_BYTES = 50_000       # below this a "paper" is a stub/template skeleton
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
    rep.findings.append(Finding("pdf_exists", "pass", "hard"))

    text = _pdf_text(pdf)
    tex = tex_path.read_text(errors="replace") if tex_path.exists() else ""
    if text is None:
        rep.findings.append(Finding("pdf_readable", "violated", "hard",
                                    "PDF could not be parsed"))
        return rep

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
    n_cites = len(re.findall(r"\\cite", tex))
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

    # ── generated figures actually used ───────────────────────────────────
    figs_dir = paper_dir / "figures"
    figs = ([f for f in figs_dir.glob("*")
             if f.suffix in (".png", ".pdf") and f.stat().st_size > MIN_FIG_BYTES]
            if figs_dir.exists() else [])
    n_inc = len(re.findall(r"\\includegraphics", tex))
    if len(figs) >= 2 and n_inc == 0:
        rep.findings.append(Finding(
            "figures_referenced", "violated", "soft",
            f"{len(figs)} usable figures generated, none referenced in the paper"))
    else:
        rep.findings.append(Finding("figures_referenced",
                                    "pass" if figs else "skipped", "soft",
                                    f"{len(figs)} figures / {n_inc} includegraphics"))

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
                    r"\\includegraphics\[[^\]]*width\s*=\s*\\(columnwidth|textwidth|linewidth)[^\]]*\]\{([^}]+)\}",
                    tex):
                fname = Path(m2.group(2)).name
                fp = figs_dir / fname
                if fp.suffix == ".pdf" and fp.exists():
                    try:
                        d2 = fitz.open(str(fp))
                        nat_w = d2[0].rect.width / 72.0
                        d2.close()
                        if nat_w < 0.75 * col_in:
                            upscaled.append(f"{fname} ({nat_w:.1f}in → {col_in:.1f}in)")
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
