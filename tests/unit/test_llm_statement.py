"""LLM Usage Statement appears between Conclusion and the bibliography.

The statement is auto-inserted by the template scaffold (and the default CLI
template) so it ends up on the same page as References when LaTeX flows.
"""

from ark import template_preprocess, cli


def test_writer_scaffold_contains_llm_statement():
    scaffold = template_preprocess._WRITER_SCAFFOLD
    assert r"\section*{LLM Usage Statement}" in scaffold
    assert "ARK (idea2paper.org)" in scaffold
    assert "ultimate responsibility" in scaffold


def test_writer_scaffold_orders_conclusion_then_statement():
    scaffold = template_preprocess._WRITER_SCAFFOLD
    conclusion_idx = scaffold.index(r"\section{Conclusion}")
    statement_idx = scaffold.index(r"\section*{LLM Usage Statement}")
    assert conclusion_idx < statement_idx, "LLM Statement must come AFTER Conclusion"


def test_default_cli_template_has_statement_before_bibliography():
    # An unrecognized venue_format falls through to the default
    # \documentclass[11pt]{article} branch in _get_main_tex_content.
    tex = cli._get_main_tex_content(
        venue_format="generic-workshop",
        title="Some Title",
        venue_name="Workshop",
        authors=["Test"],
    )
    assert r"\section*{LLM Usage Statement}" in tex
    statement_idx = tex.index(r"\section*{LLM Usage Statement}")
    biblio_idx = tex.index(r"\bibliography{")
    assert statement_idx < biblio_idx, "LLM Statement must come BEFORE bibliography"
