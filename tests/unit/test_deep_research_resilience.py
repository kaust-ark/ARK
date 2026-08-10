"""Deep Research must fail loudly and never leave a paper ungrounded.

2026-08-06: OpenRouter answered HTTP 200 with a body containing nothing but
keep-alive padding (974 lines, ~5.4 KB) because the upstream research model
died mid-generation. `resp.json()` reported "Expecting value: line 975
column 1", the pipeline logged a WARN, and then wrote an entire paper with
zero literature grounding — silently.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ark.deep_research import _parse_openrouter_body


class TestBodyParsing:
    def test_keepalive_padding_only_is_named_not_a_json_traceback(self):
        data, reason = _parse_openrouter_body("   \n" * 974, "3m41s")
        assert data is None
        assert "empty body" in reason and "3m41s" in reason
        assert "Expecting value" not in reason  # no raw parser noise

    def test_sse_comment_lines_are_skipped_not_parsed(self):
        body = ": OPENROUTER PROCESSING\n" * 20 + '{"choices": [{"x": 1}]}'
        data, _ = _parse_openrouter_body(body, "2m")
        assert data == {"choices": [{"x": 1}]}

    def test_comments_with_no_payload_are_reported(self):
        data, reason = _parse_openrouter_body(": OPENROUTER PROCESSING\n" * 20, "2m")
        assert data is None and "keep-alive" in reason

    def test_streamed_body_takes_the_last_data_event(self):
        body = 'data: {"choices": [{"a": 1}]}\ndata: {"choices": [{"a": 2}]}\ndata: [DONE]'
        data, _ = _parse_openrouter_body(body, "1m")
        assert data == {"choices": [{"a": 2}]}

    def test_plain_json_still_works(self):
        data, reason = _parse_openrouter_body('{"choices": []}', "1m")
        assert data == {"choices": []} and reason == ""


class TestFallbackSurvey:
    """The pipeline mixin method, exercised on a bare host object."""

    def _host(self, tmp_path):
        from ark.pipeline import PipelineMixin
        h = SimpleNamespace()
        h.state_dir = tmp_path
        h.log = lambda *a, **k: None
        h.log_step = lambda *a, **k: None
        h._fallback_literature_survey = PipelineMixin._fallback_literature_survey.__get__(h)
        return h

    def test_writes_a_real_survey_and_marks_it_degraded(self, tmp_path):
        host = self._host(tmp_path)
        papers = [SimpleNamespace(title="Attention Is All You Need",
                                  authors=["A Vaswani"], venue="NeurIPS",
                                  year=2017, abstract="The dominant models...")]
        with patch("ark.citation.search_papers", return_value=papers):
            assert host._fallback_literature_survey("transformers") is True

        report = (tmp_path / "deep_research.md").read_text()
        assert "Attention Is All You Need" in report
        # The report must admit what it is, so downstream agents aren't misled.
        assert "THINNER" in report
        marker = (tmp_path / "research_degraded.txt").read_text()
        assert "1 papers" in marker

    def test_no_results_still_records_the_gap(self, tmp_path):
        host = self._host(tmp_path)
        with patch("ark.citation.search_papers", return_value=[]):
            assert host._fallback_literature_survey("x") is False
        assert not (tmp_path / "deep_research.md").exists()
        assert "no literature grounding" in (tmp_path / "research_degraded.txt").read_text()

    def test_search_failure_is_survived(self, tmp_path):
        host = self._host(tmp_path)
        with patch("ark.citation.search_papers", side_effect=RuntimeError("net down")):
            assert host._fallback_literature_survey("x") is False
        assert (tmp_path / "research_degraded.txt").exists()


def test_delivery_contract_flags_degraded_research(tmp_path):
    """A thinner foundation must be visible in the report, not silent."""
    from ark import delivery_contract as dc
    state = tmp_path / "auto_research" / "state"
    state.mkdir(parents=True)
    (state / "research_degraded.txt").write_text("Deep Research failed; 5 papers.")
    # `evaluate` bails out early on a missing/stub/unparseable PDF, so reuse
    # the contract suite's own fixture builder rather than a fake byte blob.
    from tests.unit.test_delivery_contract import _project, GOOD_TEX, GOOD_PDF_TEXT
    _project(tmp_path, GOOD_TEX, GOOD_PDF_TEXT)

    rep = dc.evaluate(tmp_path, venue_pages=8)
    grounded = [f for f in rep.findings if f.check == "research_grounded"]
    assert grounded and grounded[0].status == "violated"
    assert grounded[0].severity == "soft"  # degraded ships, but visibly
