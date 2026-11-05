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


# Enough prose that the fixture is a paper rather than a template skeleton,
# which the contract now rejects outright. Deliberately bland: no "?", no
# vendor names, nothing any check keys on.
_BODY = ("We measure how standardizing input features changes the number of "
         "iterations a quasi-Newton solver needs to converge, holding the "
         "dataset, the split, the seed and every hyperparameter fixed. ") * 12


def _project(tmp_path: Path, tex: str, pdf_text: str, bib: str = "@article{a, title={t}}",
             body: str = _BODY):
    paper = tmp_path / "paper"
    (paper / "figures").mkdir(parents=True)
    (paper / "main.tex").write_text(tex)
    (paper / "references.bib").write_text(bib)
    _make_pdf(paper / "main.pdf", pdf_text + " " + body)
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


def _reviewed(p, tex):
    state = p / "auto_research" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "reviewed_main.tex").write_text(tex)


def _finding(rep, check):
    return next(f for f in rep.findings if f.check == check)


def test_content_lost_after_the_last_review_is_reported(tmp_path):
    """527e025d: reviewed with its appendix, delivered without it."""
    reviewed = GOOD_TEX.replace("\\end{document}", "\\appendix\\section{Extra}\\label{app:x}"
                                "\\begin{table}\\end{table}\\end{document}")
    shipped = GOOD_TEX.replace("Body \\cite{a}.", "Body \\cite{a}, see Appendix~\\ref{app:x}.")
    p = _project(tmp_path, shipped, GOOD_PDF_TEXT)
    _reviewed(p, reviewed)
    f = _finding(evaluate(p), "no_loss_since_review")
    assert f.status == "violated"
    assert "app:x" in f.detail and "1 tables" in f.detail and "undefined labels" in f.detail


def test_edits_after_review_that_keep_everything_pass(tmp_path):
    p = _project(tmp_path, GOOD_TEX.replace("Body", "Body, reworded"), GOOD_PDF_TEXT)
    _reviewed(p, GOOD_TEX)
    f = _finding(evaluate(p), "no_loss_since_review")
    assert f.status == "pass" and "edited after the last review" in f.detail


def test_no_review_snapshot_means_no_regression_check(tmp_path):
    rep = evaluate(_project(tmp_path, GOOD_TEX, GOOD_PDF_TEXT))
    assert not any(f.check == "no_loss_since_review" for f in rep.findings)


# ── a deletion the review asked for is not an unreviewed loss ───────────────

def _review(p, text):
    state = p / "auto_research" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "latest_review.md").write_text(text)


FIG_TEX = GOOD_TEX.replace(
    "Body \\cite{a}.",
    "Body \\cite{a}, see Fig.~\\ref{fig:ablation}.\n"
    "\\begin{figure}\\includegraphics{figures/fig_ablation.pdf}"
    "\\caption{c}\\label{fig:ablation}\\end{figure}")
NO_FIG_TEX = GOOD_TEX


def test_a_deletion_nobody_reviewed_is_still_reported(tmp_path):
    p = _project(tmp_path, NO_FIG_TEX, GOOD_PDF_TEXT)
    _reviewed(p, FIG_TEX)
    _review(p, "### M1. The introduction is too long\nTrim the second paragraph.")

    f = _finding(evaluate(p), "no_loss_since_review")
    assert f.status == "violated"
    assert "fig:ablation" in f.detail


def test_a_deletion_the_review_asked_for_by_label_passes(tmp_path):
    p = _project(tmp_path, NO_FIG_TEX, GOOD_PDF_TEXT)
    _reviewed(p, FIG_TEX)
    _review(p, "### M1. Ad-hoc ablation figure violates the protocol\n"
               "**Target**: `fig:ablation`\n**Action**: Remove Figure 2.")

    f = _finding(evaluate(p), "no_loss_since_review")
    assert f.status == "pass"
    assert "fig:ablation" in f.detail          # says what it accounted for


def test_a_deletion_the_review_named_by_graphics_file_passes(tmp_path):
    """The real M1 named the file, not the label."""
    p = _project(tmp_path, NO_FIG_TEX, GOOD_PDF_TEXT)
    _reviewed(p, FIG_TEX)
    _review(p, "**Target**: `paper/main.tex` (lines 87-94), "
               "`paper/figures/fig_ablation.pdf`\n**Action**: Remove Figure 2.")

    assert _finding(evaluate(p), "no_loss_since_review").status == "pass"


def test_only_what_the_review_named_is_excused(tmp_path):
    """Obeying M1 does not license dropping something else in the same edit."""
    two = FIG_TEX.replace(
        "\\end{document}",
        "\\begin{table}\\caption{t}\\label{tab:main}\\end{table}\\end{document}")
    p = _project(tmp_path, NO_FIG_TEX, GOOD_PDF_TEXT)
    _reviewed(p, two)
    _review(p, "**Target**: `paper/figures/fig_ablation.pdf`\n**Action**: Remove Figure 2.")

    f = _finding(evaluate(p), "no_loss_since_review")
    assert f.status == "violated"
    assert "tab:main" in f.detail
    assert "fig:ablation" not in f.detail
    assert "1 tables" in f.detail
    assert "figures" not in f.detail           # the excused one is subtracted


def test_without_a_review_file_nothing_is_excused(tmp_path):
    p = _project(tmp_path, NO_FIG_TEX, GOOD_PDF_TEXT)
    _reviewed(p, FIG_TEX)                      # snapshot, but no review text

    assert _finding(evaluate(p), "no_loss_since_review").status == "violated"


def test_the_review_merely_discussing_a_figure_still_excuses_only_removals(tmp_path):
    """Nothing removed, so nothing to excuse — and no false pass detail."""
    p = _project(tmp_path, FIG_TEX, GOOD_PDF_TEXT)
    _reviewed(p, FIG_TEX)
    _review(p, "**Target**: `paper/figures/fig_ablation.pdf`\n**Action**: Remove Figure 2.")

    f = _finding(evaluate(p), "no_loss_since_review")
    assert f.status == "pass"
    assert "asked to remove" not in f.detail


# ── a skeleton is a stub however much it weighs ────────────────────────────

def test_a_pdf_with_only_headings_is_a_stub(tmp_path):
    """acmart embeds its fonts, so an empty paper still clears the byte floor.

    Measured: a run a rate limit cut short mid-draft shipped section headings
    and nothing else — 473 characters of text in 221 KB, and every content
    check downstream read it as a healthy paper.
    """
    p = _project(tmp_path, GOOD_TEX,
                 "Introduction Related Work Method Experiments Conclusion",
                 body="")                    # headings, no prose — as shipped

    f = _finding(evaluate(p, venue_pages=2), "pdf_exists")
    assert f.status == "violated"
    assert "skeleton" in f.detail


def test_a_real_paper_is_not_mistaken_for_a_skeleton(tmp_path):
    p = _project(tmp_path, GOOD_TEX, GOOD_PDF_TEXT)

    assert _finding(evaluate(p, venue_pages=2), "pdf_exists").status == "pass"
