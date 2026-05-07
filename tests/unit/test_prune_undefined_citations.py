"""Unit tests for _prune_undefined_citations: keys defined in any
sibling .bib (e.g. ACL's custom.bib) must NOT be pruned.

Regression: paper 1 in this session lost 25 \\cite{} calls during
Continue iter 3 because the pruner only inspected references.bib
while the keys lived in custom.bib.
"""
import re
import textwrap
from pathlib import Path

import pytest

from ark.latex.compiler import CompilerMixin


class _MockOrch(CompilerMixin):
    def __init__(self, latex_dir: Path):
        self.latex_dir = latex_dir
        self.code_dir = latex_dir.parent
        self.state_dir = latex_dir.parent / "auto_research" / "state"
        self.config = {}
        self.hooks = {}
        self.log_calls: list[tuple[str, str]] = []

    def log(self, msg: str, level: str = "INFO"):
        self.log_calls.append((level, msg))


def _seed(latex_dir: Path, files: dict[str, str]):
    latex_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (latex_dir / name).write_text(textwrap.dedent(content).lstrip())


def test_does_not_prune_keys_defined_in_custom_bib(tmp_path):
    """Real paper-1 regression: \\cite{mlebench2024} from main.tex,
    key in custom.bib but not in references.bib.

    Old behaviour: pruner saw references.bib only, stripped the cite.
    New behaviour: pruner unions references.bib + custom.bib keys
    and leaves the cite alone.
    """
    _seed(tmp_path, {
        "main.tex": r"""
            \documentclass{article}
            \begin{document}
            Benchmarks like MLE-Bench~\cite{mlebench2024} and
            MLR-Bench~\cite{mlrbench2025} measure pass-rate.
            \bibliography{custom}
            \end{document}
            """,
        "references.bib": "% ARK auto-managed, no entries\n",
        "custom.bib": """
            @misc{mlebench2024, title={MLE-Bench}, year=2024}
            @inproceedings{mlrbench2025, title={MLR-Bench}, year=2025}
            """,
    })
    orch = _MockOrch(tmp_path)
    pruned = orch._prune_undefined_citations()
    assert pruned == 0, "should NOT prune keys defined in custom.bib"
    text = (tmp_path / "main.tex").read_text()
    assert r"\cite{mlebench2024}" in text
    assert r"\cite{mlrbench2025}" in text


def test_prunes_keys_truly_missing_from_all_bibs(tmp_path):
    _seed(tmp_path, {
        "main.tex": r"""
            \documentclass{article}
            \begin{document}
            See~\cite{realKey} and \cite{ghostKey} for details.
            \bibliography{custom}
            \end{document}
            """,
        "references.bib": "@misc{realKey, title={Real}}",
        "custom.bib": "% empty\n",
    })
    orch = _MockOrch(tmp_path)
    pruned = orch._prune_undefined_citations()
    assert pruned == 1
    text = (tmp_path / "main.tex").read_text()
    assert r"\cite{realKey}" in text
    assert "ghostKey" not in text


def test_unions_keys_across_three_bibs(tmp_path):
    _seed(tmp_path, {
        "main.tex": r"""
            \documentclass{article}
            \begin{document}
            \cite{a}, \cite{b}, \cite{c}, \cite{d}.
            \bibliography{anthology,custom}
            \end{document}
            """,
        "references.bib": "@misc{a, title={A}}\n",
        "custom.bib":      "@misc{b, title={B}}\n",
        "anthology.bib":   "@misc{c, title={C}}\n",
    })
    orch = _MockOrch(tmp_path)
    pruned = orch._prune_undefined_citations()
    assert pruned == 1, "only `d` should be pruned"
    text = (tmp_path / "main.tex").read_text()
    for key in ("a", "b", "c"):
        assert f"\\cite{{{key}}}" in text
    assert "\\cite{d}" not in text


def test_log_lists_all_scanned_bibs(tmp_path):
    _seed(tmp_path, {
        "main.tex": r"""
            \begin{document}\cite{ghost}\bibliography{custom}\end{document}
            """,
        "references.bib": "@misc{ref_only, title={X}}\n",
        "custom.bib":     "@misc{cus_only, title={Y}}\n",
    })
    orch = _MockOrch(tmp_path)
    orch._prune_undefined_citations()
    log_text = " ".join(m for _, m in orch.log_calls)
    assert "references.bib" in log_text
    assert "custom.bib" in log_text


def test_no_op_when_no_main_tex(tmp_path):
    _seed(tmp_path, {"custom.bib": "@misc{x}\n"})
    orch = _MockOrch(tmp_path)
    assert orch._prune_undefined_citations() == 0


def test_no_op_when_no_bib_files(tmp_path):
    _seed(tmp_path, {
        "main.tex": r"\begin{document}\cite{x}\end{document}",
    })
    orch = _MockOrch(tmp_path)
    assert orch._prune_undefined_citations() == 0


def test_idempotent_double_run_does_not_re_prune(tmp_path):
    _seed(tmp_path, {
        "main.tex": r"""
            \begin{document}\cite{good}, \cite{bad}\end{document}
            """,
        "references.bib": "@misc{good, title={G}}",
    })
    orch = _MockOrch(tmp_path)
    n1 = orch._prune_undefined_citations()
    n2 = orch._prune_undefined_citations()
    assert n1 == 1
    assert n2 == 0
