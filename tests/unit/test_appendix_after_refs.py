"""Regression tests for _ensure_clearpage_before_bibliography appendix reorder.

ACM sample-sigplan.tex puts \\appendix AFTER \\bibliography (line 745-844
of acmart's official sample). Writer agents have been observed to emit
the older body→appendix→bibliography order; ARK auto-reorders so the
rendered PDF matches venue convention.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def stub(tmp_path):
    from ark.execution import ExecutionMixin

    latex_dir = tmp_path / "paper"
    latex_dir.mkdir(parents=True)

    s = MagicMock(spec=ExecutionMixin)
    s.latex_dir = latex_dir
    s.log = MagicMock()
    s._ARK_BODY_END_MARKER = ExecutionMixin._ARK_BODY_END_MARKER
    s._first_pos = ExecutionMixin._first_pos
    s._ensure_clearpage_before_bibliography = (
        ExecutionMixin._ensure_clearpage_before_bibliography.__get__(s)
    )
    return s


def _write_main(stub, body):
    (stub.latex_dir / "main.tex").write_text(body)


def _read_main(stub):
    return (stub.latex_dir / "main.tex").read_text()


# Body → appendix → bib  (the broken ordering writer often emits)
_BROKEN = r"""\documentclass[sigplan]{acmart}
\begin{document}
\section{Intro}
Body text.
\section{Conclusion}
Closing words.

\appendix
\section{Mitigation Sweep}
Appendix table here.

\clearpage
\bibliographystyle{ACM-Reference-Format}
\bibliography{references}

\end{document}
"""


def test_reorders_appendix_to_after_bibliography(stub):
    _write_main(stub, _BROKEN)
    stub._ensure_clearpage_before_bibliography()
    out = _read_main(stub)

    # Order check: \bibliography appears BEFORE \appendix
    bib_pos = out.find(r"\bibliography{")
    appendix_pos = out.find(r"\appendix")
    assert bib_pos != -1 and appendix_pos != -1
    assert bib_pos < appendix_pos, (
        "Bibliography must precede Appendix in canonical layout"
    )


def test_marker_present_at_body_end(stub):
    _write_main(stub, _BROKEN)
    stub._ensure_clearpage_before_bibliography()
    out = _read_main(stub)

    marker_pos = out.find("arkBodyEndPage")
    bib_pos = out.find(r"\bibliography{")
    appendix_pos = out.find(r"\appendix")
    # Body-end marker must precede the bibliography (it marks the
    # end of body, before the page break to refs).
    assert marker_pos != -1
    assert marker_pos < bib_pos < appendix_pos


def test_idempotent_on_already_canonical(stub):
    canonical = r"""\documentclass[sigplan]{acmart}
\begin{document}
\section{Intro}
Body.
\section{Conclusion}
Words.

\clearpage
\bibliographystyle{ACM-Reference-Format}
\bibliography{references}

\clearpage
\appendix
\section{Extra}
Stuff.

\end{document}
"""
    _write_main(stub, canonical)
    stub._ensure_clearpage_before_bibliography()
    pass1 = _read_main(stub)

    stub._ensure_clearpage_before_bibliography()
    pass2 = _read_main(stub)

    assert pass1 == pass2, "Second run must be byte-identical"
    # And canonical order stays
    assert pass2.find(r"\bibliography{") < pass2.find(r"\appendix")


def test_no_appendix_just_inserts_marker_and_clearpage(stub):
    src = r"""\documentclass[sigplan]{acmart}
\begin{document}
\section{Intro}
Body.

\bibliographystyle{ACM-Reference-Format}
\bibliography{references}

\end{document}
"""
    _write_main(stub, src)
    stub._ensure_clearpage_before_bibliography()
    out = _read_main(stub)

    assert "arkBodyEndPage" in out
    assert r"\appendix" not in out  # nothing fabricated
    # \clearpage must precede \bibliography
    bib_pos = out.find(r"\bibliography{")
    clear_pos = out.rfind(r"\clearpage", 0, bib_pos)
    assert clear_pos != -1
    assert clear_pos < bib_pos


def test_no_bibliography_is_no_op(stub):
    """A draft without \\bibliography{} just gets left alone — we don't
    fabricate a bibliography block."""
    src = r"""\documentclass[sigplan]{acmart}
\begin{document}
\section{Intro}
Body, no refs yet.
\end{document}
"""
    _write_main(stub, src)
    stub._ensure_clearpage_before_bibliography()
    out = _read_main(stub)
    assert out == src  # untouched


# IEEEtran's \appendices after the bibliography (project 527e). The old
# find(r'\appendix') missed it, took the no-appendix branch and kept nothing
# past \bibliography: the whole appendix and every label in it were deleted.
_IEEE_APPENDICES = r"""\documentclass[conference]{IEEEtran}
\begin{document}
\section{Intro}
See Appendix~\ref{app:sweep}.
\section{Conclusion}
Words.

\bibliographystyle{IEEEtran}
\bibliography{references}

\clearpage
\appendices
\section{Sweep}\label{app:sweep}
\begin{table}[t]\caption{Full map}\label{tab:map}\end{table}
\section{Proofs}\label{app:proofs}
Proof text.

\end{document}
"""


def test_ieeetran_appendices_survive(stub):
    _write_main(stub, _IEEE_APPENDICES)
    stub._ensure_clearpage_before_bibliography()
    out = _read_main(stub)
    for needle in (r"\appendices", r"\label{app:sweep}", r"\label{tab:map}",
                   r"\label{app:proofs}", "Proof text."):
        assert needle in out
    assert out.find(r"\bibliography{") < out.find(r"\appendices")


def test_ieeetran_appendices_before_bib_are_moved_after(stub):
    src = _IEEE_APPENDICES.replace(
        "\\bibliographystyle{IEEEtran}\n\\bibliography{references}\n\n", ""
    ).replace("Proof text.\n", "Proof text.\n\n\\bibliographystyle{IEEEtran}\n"
              "\\bibliography{references}\n")
    assert src.find(r"\appendices") < src.find(r"\bibliography{")
    _write_main(stub, src)
    stub._ensure_clearpage_before_bibliography()
    out = _read_main(stub)
    assert "Proof text." in out and r"\label{tab:map}" in out
    assert out.find(r"\bibliography{") < out.find(r"\appendices")


def test_appendices_environment_survives(stub):
    src = _IEEE_APPENDICES.replace(
        "\\appendices\n", "\\begin{appendices}\n").replace(
        "Proof text.\n", "Proof text.\n\\end{appendices}\n")
    _write_main(stub, src)
    stub._ensure_clearpage_before_bibliography()
    out = _read_main(stub)
    assert r"\begin{appendices}" in out and r"\end{appendices}" in out
    assert "Proof text." in out


def test_non_appendix_content_after_bib_survives(stub):
    src = _IEEE_APPENDICES.replace("\\appendices\n", "\\onecolumn\n")
    _write_main(stub, src)
    stub._ensure_clearpage_before_bibliography()
    out = _read_main(stub)
    assert r"\onecolumn" in out and r"\label{app:proofs}" in out


def test_appendices_rewrite_is_idempotent(stub):
    _write_main(stub, _IEEE_APPENDICES)
    stub._ensure_clearpage_before_bibliography()
    pass1 = _read_main(stub)
    stub._ensure_clearpage_before_bibliography()
    assert _read_main(stub) == pass1


def test_structure_guard_reports_losses():
    from ark.execution import _structure_lost
    before = (r"\section{A}\label{s:a}\includegraphics{f}"
              r"\begin{table}\end{table}\section*{Acknowledgments}")
    assert _structure_lost(before, before) == []
    assert _structure_lost(before, before + r"\section*{X}") == []
    lost = _structure_lost(before, r"\section*{Acknowledgments}")
    assert any("s:a" in x for x in lost)
    assert "1 figures" in lost and "1 tables" in lost and "1 sections" in lost


def test_the_disclosure_survives_a_skipped_reorder(stub, monkeypatch):
    """The structure guard must not cost the paper its required disclosure.

    `_ensure_clearpage_before_bibliography` is the only place a pipeline run
    inserts the Idea2Paper disclosure — `ensure_disclosure_in_tex` otherwise
    runs only from `ark audit --repair`. The guard added here returns before
    `main_tex.write_text(...)`, so on any paper where the reorder would drop
    structure the run shipped with no disclosure at all: a `hard` contract
    violation traded for the `soft` one the guard was avoiding.
    """
    from ark.template_preprocess import ARK_ACK_TEXT

    # A paper with endmatter to move but no disclosure yet.
    _write_main(stub, _IEEE_APPENDICES)
    assert "idea2paper.org" not in _read_main(stub)

    # Force the guard: pretend the rewrite would drop something.
    import ark.execution as ex
    monkeypatch.setattr(ex, "_structure_lost", lambda a, b: ["1 sections"])

    stub._ensure_clearpage_before_bibliography()

    out = _read_main(stub)
    assert "idea2paper.org" in out, "the reorder was skipped AND the disclosure lost"
    assert ARK_ACK_TEXT in out
    said = " ".join(str(c) for c in stub.log.call_args_list)
    assert "Skipped end-matter reorder" in said
