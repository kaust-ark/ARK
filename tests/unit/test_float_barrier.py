"""Regression tests for _ensure_float_barrier.

Bug repro: a paper using `\\documentclass{acmart}` (or any class that loads its
own packages) has *zero* `\\usepackage{...}` lines in main.tex. The previous
code anchored the `\\usepackage{placeins}` insertion on the last existing
`\\usepackage`; with none present, the package was silently skipped while
`\\FloatBarrier` was still injected — the resulting paper failed to compile
with `! Undefined control sequence.`
"""

from unittest.mock import MagicMock
from pathlib import Path

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


# Two body sections so the "len(sections) >= 2" guard fires and we actually
# inject a barrier.
_BODY = (
    "\\section{Intro}\nIntroduction body.\n\n"
    "\\section{Method}\nMethod body.\n\n"
    "\\section{Results}\nResults body.\n"
)


def test_acmart_no_usepackage_inserts_placeins(exec_stub):
    """acmart-style preamble has no \\usepackage lines; placeins must still
    end up in the file before \\FloatBarrier is injected."""
    main_tex = exec_stub.latex_dir / "main.tex"
    main_tex.write_text(
        "\\documentclass[sigplan,10pt]{acmart}\n"
        "\\graphicspath{{figures/}}\n"
        "\\begin{document}\n"
        + _BODY +
        "\\bibliography{refs}\n"
        "\\end{document}\n"
    )

    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    assert "\\usepackage{placeins}" in out, "placeins must be inserted"
    assert "\\FloatBarrier" in out, "FloatBarrier must still be injected"
    # placeins must precede \begin{document} so it is in the preamble.
    assert out.index("\\usepackage{placeins}") < out.index("\\begin{document}")


def test_existing_usepackage_anchors_correctly(exec_stub):
    """With existing \\usepackage lines, placeins is inserted after the last
    one (preserves the established preamble order)."""
    main_tex = exec_stub.latex_dir / "main.tex"
    main_tex.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{amsmath}\n"
        "\\usepackage{graphicx}\n"
        "\\begin{document}\n"
        + _BODY +
        "\\end{document}\n"
    )

    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    assert out.count("\\usepackage{placeins}") == 1
    # Inserted after \usepackage{graphicx}, before \begin{document}.
    assert out.index("\\usepackage{graphicx}") < out.index("\\usepackage{placeins}")
    assert out.index("\\usepackage{placeins}") < out.index("\\begin{document}")


def test_idempotent_when_placeins_already_loaded(exec_stub):
    """Running twice (or on a file that already has placeins) must not
    duplicate the package."""
    main_tex = exec_stub.latex_dir / "main.tex"
    main_tex.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{placeins}\n"
        "\\begin{document}\n"
        + _BODY +
        "\\end{document}\n"
    )

    exec_stub._ensure_float_barrier()
    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    assert out.count("\\usepackage{placeins}") == 1
    assert out.count("\\FloatBarrier") == 1


def test_no_begin_document_bails_out_safely(exec_stub):
    """A pathological main.tex with neither \\usepackage nor
    \\begin{document} should not corrupt the file by injecting a stray
    \\FloatBarrier without its package."""
    main_tex = exec_stub.latex_dir / "main.tex"
    original = (
        "\\documentclass{article}\n"
        + _BODY  # body sections only, no document environment
    )
    main_tex.write_text(original)

    exec_stub._ensure_float_barrier()

    out = main_tex.read_text()
    assert "\\FloatBarrier" not in out, (
        "Must not inject FloatBarrier without a way to load placeins"
    )
    # File contents should otherwise be unchanged.
    assert out == original
