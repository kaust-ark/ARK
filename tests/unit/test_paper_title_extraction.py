"""_extract_tex_title / _read_paper_title: robust \\title{} extraction.

ff5a2e5b regression: a custom elsarticle template's COMMENTED example
``%% \\title{Title\\tnoteref{label1}}`` sat above the real \\title, the naive
first-``}`` regex captured ``Title\\tnoteref{label1``, and the project-list
sync overwrote the DB title with it. These tests pin the robust behavior:
comments skipped, braces balanced, annotation markers stripped, placeholders
rejected in favor of config.yaml.
"""

import textwrap

import pytest

from website.dashboard.routes import _extract_tex_title, _read_paper_title


ELSARTICLE_LIKE = textwrap.dedent(r"""
    %% use the tnoteref command within \title for footnotes;
    %% \title{Title\tnoteref{label1}}
    %% \tnotetext[label1]{}
    \documentclass[preprint,12pt]{elsarticle}
    \begin{document}
    \title{IC-Custom+FG: Frequency-Guided Identity Preservation}
    \end{document}
""")


def test_commented_template_example_is_skipped():
    assert _extract_tex_title(ELSARTICLE_LIKE) == \
        "IC-Custom+FG: Frequency-Guided Identity Preservation"


def test_tnoteref_marker_stripped_from_real_title():
    tex = r"\title{Real Title\tnoteref{t1,t2}}"
    assert _extract_tex_title(tex) == "Real Title"


def test_nested_braces_survive():
    tex = r"\title{Learning \emph{fast} adaptation}"
    assert _extract_tex_title(tex) == r"Learning \emph{fast} adaptation"


def test_multiline_title_collapses_whitespace():
    tex = "\\title{A Very Long\n  Wrapped \\\\ Title}"
    assert _extract_tex_title(tex) == "A Very Long Wrapped Title"


def test_unbalanced_braces_refused():
    assert _extract_tex_title(r"\title{Broken \tnoteref{label1") == ""


def test_placeholder_title_falls_back_to_config(tmp_path):
    paper = tmp_path / "paper"
    paper.mkdir()
    # active title IS the bare template placeholder
    (paper / "main.tex").write_text(r"\title{Title\tnoteref{label1,label2}}")
    (tmp_path / "config.yaml").write_text("title: Config Title\n")
    assert _read_paper_title(tmp_path) == "Config Title"


def test_real_title_read_from_tex(tmp_path):
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text(ELSARTICLE_LIKE)
    assert _read_paper_title(tmp_path) == \
        "IC-Custom+FG: Frequency-Guided Identity Preservation"
