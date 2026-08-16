"""Delivery contract: acceptance checks over final paper artifacts.

Fixtures replay the real production violations of 2026-07 week 2:
missing disclosure (7 papers), empty references.bib with unresolved cites
(debe29f1), unreferenced figures (44459bb4), stub PDFs marked done,
stack credits in Acknowledgments (472e4874).
"""

import re
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from ark.delivery_contract import (
    evaluate, ensure_disclosure_in_tex, DISCLOSURE_MARKER)
from ark.template_preprocess import ARK_ACK_TEXT


def _make_pdf(path: Path, text: str, pages: int = 2):
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page()
        if i == 0:
            # word-boundary wrap: a single long insert_text line is clipped at
            # the page edge and lost from extraction
            words, lines, cur = text.split(" "), [], ""
            for w in words:
                if len(cur) + len(w) > 70:
                    lines.append(cur); cur = w
                else:
                    cur = (cur + " " + w).strip()
            lines.append(cur)
            for k, ln in enumerate(lines):
                pg.insert_text((72, 72 + 12 * k), ln)
        else:
            pg.insert_text((72, 72), f"page {i}")
    doc.save(str(path))
    doc.close()
    # pad past the stub threshold
    with open(path, "ab") as f:
        f.write(b"%" + b"x" * 60_000)


def _project(tmp_path: Path, tex: str, pdf_text: str, bib: str = "@article{a, title={t}}"):
    paper = tmp_path / "paper"
    (paper / "figures").mkdir(parents=True)
    (paper / "main.tex").write_text(tex)
    (paper / "references.bib").write_text(bib)
    _make_pdf(paper / "main.pdf", pdf_text)
    return tmp_path


GOOD_TEX = ("\\documentclass{article}\\begin{document}Body \\cite{a}.\n"
            "\\section*{Acknowledgments}\n" + ARK_ACK_TEXT +
            "\n\\bibliography{references}\\end{document}")
GOOD_PDF_TEXT = "Body [1]. Acknowledgments " + ARK_ACK_TEXT


def test_healthy_paper_passes(tmp_path):
    p = _project(tmp_path, GOOD_TEX, GOOD_PDF_TEXT)
    rep = evaluate(p, venue_pages=8)
    assert not rep.violations, [f"{f.check}: {f.detail}" for f in rep.violations]


def test_stub_pdf_is_hard_violation(tmp_path):
    paper = tmp_path / "paper"; paper.mkdir()
    (paper / "main.pdf").write_bytes(b"%PDF tiny")
    rep = evaluate(tmp_path)
    assert rep.hard_violations and rep.findings[0].check == "pdf_exists"


def test_missing_disclosure_flagged(tmp_path):
    p = _project(tmp_path, GOOD_TEX.replace(ARK_ACK_TEXT, "We thank nobody."),
                 GOOD_PDF_TEXT.replace(ARK_ACK_TEXT, "We thank nobody."))
    rep = evaluate(p)
    assert any(f.check == "disclosure_present" and f.status == "violated"
               for f in rep.hard_violations)


def test_unresolved_citations_flagged(tmp_path):
    p = _project(tmp_path, GOOD_TEX, GOOD_PDF_TEXT + " as shown in [?] and (?) ")
    rep = evaluate(p)
    assert any(f.check == "citations_resolved" and f.status == "violated"
               for f in rep.hard_violations)


def test_empty_bib_with_cites_flagged(tmp_path):
    p = _project(tmp_path, GOOD_TEX, GOOD_PDF_TEXT, bib="\n")
    rep = evaluate(p)
    assert any(f.check == "references_nonempty" and f.status == "violated"
               for f in rep.hard_violations)


def test_unreferenced_figures_soft_flagged(tmp_path):
    p = _project(tmp_path, GOOD_TEX, GOOD_PDF_TEXT)
    for name in ("f1.png", "f2.pdf"):
        (p / "paper" / "figures" / name).write_bytes(b"x" * 6000)
    rep = evaluate(p)
    v = [f for f in rep.violations if f.check == "figures_referenced"]
    assert v and v[0].severity == "soft"


def test_stack_leak_in_ack_flagged(tmp_path):
    leaky = GOOD_TEX.replace(
        ARK_ACK_TEXT,
        "This research was supported by the OpenHands AI research platform. " + ARK_ACK_TEXT)
    p = _project(tmp_path, leaky, GOOD_PDF_TEXT)
    rep = evaluate(p)
    assert any(f.check == "ack_no_stack_leak" and f.status == "violated"
               for f in rep.hard_violations)


def test_model_mention_in_methods_not_flagged(tmp_path):
    # 472e4874 lesson: model/provider names in the METHODS section are
    # legitimate reproducibility info — only the ack block is policed.
    tex = GOOD_TEX.replace("Body \\cite{a}.",
                           "Experiments use Claude Sonnet 4 via OpenRouter. \\cite{a}.")
    p = _project(tmp_path, tex, GOOD_PDF_TEXT)
    rep = evaluate(p)
    assert not any(f.check == "ack_no_stack_leak" and f.status == "violated"
                   for f in rep.findings)


def test_repair_inserts_into_existing_ack(tmp_path):
    tex_path = tmp_path / "main.tex"
    tex_path.write_text("\\documentclass{article}\\begin{document}Body.\n"
                        "\\section*{Acknowledgments}\nWe thank the reviewers.\n"
                        "\\bibliography{references}\\end{document}")
    assert ensure_disclosure_in_tex(tex_path) is True
    out = tex_path.read_text()
    assert DISCLOSURE_MARKER in out
    assert out.index("Acknowledgments") < out.index(DISCLOSURE_MARKER) < out.index("\\bibliography")
    # idempotent
    assert ensure_disclosure_in_tex(tex_path) is False


def test_repair_creates_section_before_bib(tmp_path):
    tex_path = tmp_path / "main.tex"
    tex_path.write_text("\\documentclass{article}\\begin{document}Body.\n"
                        "\\bibliographystyle{plain}\n\\bibliography{references}\\end{document}")
    assert ensure_disclosure_in_tex(tex_path) is True
    out = tex_path.read_text()
    assert re.search(r"\\section\*\{Acknowledgments\}[\s\S]*idea2paper\.org[\s\S]*\\bibliographystyle", out)


# ── Most figures left on the floor ───────────────────────────────────────────
# The rule used to fire only at ZERO references. Smoke run 36763067 generated
# four figures, a writing pass deleted three \includegraphics blocks, one
# remained — and the contract reported "all checks passed" on a paper missing
# two thirds of its evidence.

def _tex_with(*figures):
    inc = "".join(f"\\includegraphics[width=\\columnwidth]{{figures/{f}}}\n"
                  for f in figures)
    return ("\\documentclass{article}\\begin{document}Body \\cite{a}.\n" + inc +
            "\\section*{Acknowledgments}\n" + ARK_ACK_TEXT +
            "\n\\bibliography{references}\\end{document}")


def _figures(project, *stems):
    for stem in stems:
        (project / "paper" / "figures" / f"{stem}.pdf").write_bytes(b"x" * 6000)
        (project / "paper" / "figures" / f"{stem}.png").write_bytes(b"x" * 6000)


def test_shipping_one_of_four_generated_figures_is_flagged(tmp_path):
    p = _project(tmp_path, _tex_with("fig_main.pdf"), GOOD_PDF_TEXT)
    _figures(p, "fig_main", "fig_ablation", "fig_concept", "fig_convergence")
    rep = evaluate(p)
    v = [f for f in rep.violations if f.check == "figures_referenced"]
    assert v, "a paper using 1 of 4 generated figures must not pass silently"
    assert v[0].severity == "soft"
    assert "fig_ablation" in v[0].detail        # names what went missing


def test_the_png_pdf_pair_of_one_figure_counts_once(tmp_path):
    """Both formats of a single plot must not read as two unused figures."""
    p = _project(tmp_path, _tex_with("fig_main.pdf"), GOOD_PDF_TEXT)
    _figures(p, "fig_main")
    rep = evaluate(p)
    assert not [f for f in rep.violations if f.check == "figures_referenced"]


def test_a_spare_figure_is_tolerated(tmp_path):
    """Generating one extra is normal practice, not a defect."""
    p = _project(tmp_path, _tex_with("fig_a.pdf", "fig_b.pdf"), GOOD_PDF_TEXT)
    _figures(p, "fig_a", "fig_b", "fig_spare")
    rep = evaluate(p)
    assert not [f for f in rep.violations if f.check == "figures_referenced"]
