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
