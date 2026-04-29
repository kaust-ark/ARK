"""Regression tests for _ensure_float_barrier.

Behavior contract:
- Body-region `\\FloatBarrier` (writer-added or otherwise) gets stripped.
- Nothing is *added* — no `\\FloatBarrier`, no `\\usepackage{placeins}`.
  An earlier version of this routine injected one canonical `\\FloatBarrier`
  before the last body `\\section`; that injection collapsed the entire
  pending-float queue at end-of-body and pushed the Conclusion onto a 9th
  page in figure-dense papers (~6 figures + a table in 8 pages), silently
  breaking venue page limits. The fix removed the injection.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def exec_stub(tmp_path):
    """Stub exposing the attributes _ensure_float_barrier needs."""
    from ark.execution import ExecutionMixin

    latex_dir = tmp_path / "paper"
    latex_dir.mkdir(parents=True)

    stub = MagicMock(spec=ExecutionMixin)
    stub.latex_dir = latex_dir
    stub.log = MagicMock()
    stub._ensure_float_barrier = ExecutionMixin._ensure_float_barrier.__get__(stub)
    return stub


_BODY = (
    "\\section{Intro}\nIntroduction body.\n\n"
    "\\section{Method}\nMethod body.\n\n"
    "\\section{Results}\nResults body.\n"
)


def test_strips_writer_added_floatbarrier(exec_stub):
    main_tex = exec_stub.latex_dir / "main.tex"
    main_tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Intro}\nFoo.\n\n"
        "\\FloatBarrier\n"
        "\\section{Method}\nBar.\n"
        "\\end{document}\n"
    )

    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    assert "\\FloatBarrier" not in out, (
        "writer-added FloatBarrier in body must be stripped"
    )


def test_strips_multiple_floatbarriers(exec_stub):
    main_tex = exec_stub.latex_dir / "main.tex"
    main_tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{A}\n\\FloatBarrier\nFoo.\n"
        "\\section{B}\n\\FloatBarrier\nBar.\n"
        "\\section{C}\n\\FloatBarrier\nBaz.\n"
        "\\end{document}\n"
    )

    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    assert "\\FloatBarrier" not in out


def test_does_not_inject_when_none_present(exec_stub):
    """If the body has no \\FloatBarrier, the routine must not add one and
    must leave the file byte-identical (cheaper, also avoids creating churn
    in the project's git history)."""
    main_tex = exec_stub.latex_dir / "main.tex"
    original = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        + _BODY +
        "\\end{document}\n"
    )
    main_tex.write_text(original)

    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    assert out == original, (
        "Must not inject \\FloatBarrier when none was present"
    )
    assert "\\FloatBarrier" not in out


def test_does_not_add_placeins_package(exec_stub):
    """We never inject \\FloatBarrier ourselves anymore, so the placeins
    package isn't needed. Adding it would dirty the preamble for nothing."""
    main_tex = exec_stub.latex_dir / "main.tex"
    main_tex.write_text(
        "\\documentclass[sigplan]{acmart}\n"
        "\\begin{document}\n"
        + _BODY +
        "\\end{document}\n"
    )

    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    assert "\\usepackage{placeins}" not in out


def test_floatbarrier_after_bibliography_left_alone(exec_stub):
    """The strip only applies inside the body region (everything before
    \\appendix / \\clearpage / \\bibliography). A `\\FloatBarrier` in or after
    a back-matter context is unusual but not the bug we're guarding against,
    so we leave it."""
    main_tex = exec_stub.latex_dir / "main.tex"
    main_tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        + _BODY +
        "\\bibliography{refs}\n"
        "\\FloatBarrier\n"  # in tail
        "\\end{document}\n"
    )

    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    # tail-region FloatBarrier preserved
    assert out.count("\\FloatBarrier") == 1
    # And it's still after \bibliography
    assert out.index("\\bibliography") < out.index("\\FloatBarrier")
