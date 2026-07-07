"""Acknowledgments section appears between Conclusion and the bibliography.

The Acknowledgments block is auto-inserted by the template scaffold (and the
default CLI template) so it ends up on the same page as References when LaTeX
flows.
"""

from ark import template_preprocess, cli


def test_writer_scaffold_contains_acknowledgments():
    scaffold = template_preprocess._WRITER_SCAFFOLD
    assert r"\section*{Acknowledgments}" in scaffold
    assert "Idea2Paper (idea2paper.org)" in scaffold
    assert "full responsibility" in scaffold


def test_writer_scaffold_orders_conclusion_then_acknowledgments():
    scaffold = template_preprocess._WRITER_SCAFFOLD
    conclusion_idx = scaffold.index(r"\section{Conclusion}")
    ack_idx = scaffold.index(r"\section*{Acknowledgments}")
    assert conclusion_idx < ack_idx, "Acknowledgments must come AFTER Conclusion"


def test_default_cli_template_has_acknowledgments_before_bibliography():
    # An unrecognized venue_format falls through to the default
    # \documentclass[11pt]{article} branch in _get_main_tex_content.
    tex = cli._get_main_tex_content(
        venue_format="generic-workshop",
        title="Some Title",
        venue_name="Workshop",
        authors=["Test"],
    )
    assert r"\section*{Acknowledgments}" in tex
    ack_idx = tex.index(r"\section*{Acknowledgments}")
    biblio_idx = tex.index(r"\bibliography{")
    assert ack_idx < biblio_idx, "Acknowledgments must come BEFORE bibliography"
