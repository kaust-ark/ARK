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

    def _host(self, tmp_path, config=None):
        from ark.pipeline import PipelineMixin
        h = SimpleNamespace()
        h.state_dir = tmp_path
        # The survey builds its search queries from the project's own title and
        # idea (its Related Work names real prior papers), so a host without a
        # config is not a realistic stub.
        h.config = config if config is not None else {}
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
        assert "1 on-topic papers kept" in marker

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

    def test_prose_topic_is_asked_as_short_keyword_queries(self, tmp_path):
        """The DR prompt is prose written for an LLM. Sent verbatim to keyword
        search it fuzzy-matches incidental words: a real run on spectral bias in
        ReLU MLPs got dentistry, civil engineering and CrossRef figure records.
        """
        host = self._host(tmp_path, config={
            "title": "Empirical Investigation of Spectral Bias in Small ReLU MLPs",
            "research_idea": "Two-layer ReLU MLPs show spectral bias: low-frequency "
                             "modes converge before high-frequency ones under "
                             "gradient descent. Rahaman et al. (2019) named it.",
        })
        seen = []

        def _capture(q, max_results=15):
            seen.append(q)
            return [SimpleNamespace(
                title="On the Spectral Bias of Neural Networks",
                authors=["N Rahaman"], venue="ICML", year=2019, abstract="")]

        with patch("ark.citation.search_papers", _capture):
            assert host._fallback_literature_survey(
                "## Research Topic Summary\n\nThis project provides an empirical "
                "study of **spectral bias in two-layer ReLU MLPs** trained on "
                "univariate multi-frequency targets. y(x) = $\\sin(2\\pi k x)$\n") is True

        assert seen, "no search was issued"
        for q in seen:
            assert len(q.split()) <= 8, f"prose leaked into the query: {q!r}"
            for junk in ("##", "**", "$", "\\", "\n"):
                assert junk not in q, f"markup leaked into the query: {q!r}"
        assert any("spectral" in q.lower() for q in seen), seen
        # The report records what was actually asked, for auditing.
        assert "Search queries used:" in (tmp_path / "deep_research.md").read_text()

    def test_off_topic_hits_are_discarded(self, tmp_path):
        """CrossRef returns real papers from unrelated fields; they must not
        become the paper's literature base."""
        host = self._host(tmp_path, config={
            "title": "Empirical Investigation of Spectral Bias in Small ReLU MLPs",
            "research_idea": "spectral bias, two-layer ReLU MLP, gradient descent, "
                             "sinusoidal modes, frequency convergence, neural tangent kernel",
        })
        hits = [
            SimpleNamespace(title="On the Spectral Bias of Neural Networks",
                            authors=[], venue="ICML", year=2019, abstract=""),
            SimpleNamespace(title="Study on Tunnel Face Failure Mechanism in Two-Layer Soils",
                            authors=[], venue="", year=2021, abstract=""),
            SimpleNamespace(title="Figure 6. Pathway bias and neuromodulatory control",
                            authors=[], venue="", year=2020, abstract=""),
        ]
        with patch("ark.citation.search_papers", return_value=hits):
            assert host._fallback_literature_survey("spectral bias in ReLU MLPs") is True

        report = (tmp_path / "deep_research.md").read_text()
        assert "On the Spectral Bias of Neural Networks" in report
        assert "Tunnel Face" not in report
        assert "Figure 6" not in report
        assert "off-topic hits discarded" in (tmp_path / "research_degraded.txt").read_text()

    def test_thin_topic_disables_the_relevance_filter(self, tmp_path):
        """With a one-word topic there is no vocabulary to judge against. The
        filter must stand down rather than reject everything and leave the paper
        with no literature at all."""
        host = self._host(tmp_path, config={})
        papers = [SimpleNamespace(title="Attention Is All You Need", authors=[],
                                  venue="NeurIPS", year=2017, abstract="")]
        with patch("ark.citation.search_papers", return_value=papers):
            assert host._fallback_literature_survey("transformers") is True
        assert "Attention Is All You Need" in (tmp_path / "deep_research.md").read_text()


@pytest.mark.parametrize(("settings", "retry"), [
    ({"skip_deep_research": False, "deep_research_backend": "gemini"}, True),
    ({"skip_deep_research": False, "deep_research_backend": "openrouter"}, True),
    ({"skip_deep_research": False, "layout_mode": "strict"}, True),
    ({"skip_deep_research": False}, False),
    ({"skip_deep_research": True, "deep_research_backend": "gemini"}, False),
])
def test_continue_retries_degraded_survey_only_when_required(tmp_path, settings, retry):
    from ark.pipeline import PipelineMixin

    p = PipelineMixin()
    p.state_dir = tmp_path
    p.config = settings
    for name in ("idea.md", "project_context.md"):
        (tmp_path / name).write_text("Present")
    report = tmp_path / "deep_research.md"
    marker = tmp_path / "research_degraded.txt"
    report.write_text("# Literature Survey (fallback)\nSearch findings")
    marker.write_text("Partial survey")
    assert p._should_run_research_phase() is retry
    if not settings["skip_deep_research"]:
        # Existing completed releases had no receipt; a stale fallback marker
        # cannot require their owners to pay for the survey again.
        report.write_text("# Deep Research Report\n**Generated**: yesterday\n**Duration**: 3m\n---\nStudy.")
        assert not p._should_run_research_phase()
        assert not marker.exists()


def test_retry_uses_normal_research_phase_and_preserves_previous_fallback_on_failure(tmp_path, monkeypatch):
    from ark.pipeline import PipelineMixin

    p = PipelineMixin()
    p.state_dir = tmp_path
    p.config = {"deep_research_backend": "gemini", "skip_deep_research": False, "gemini_api_key": "dummy"}
    p.telegram = SimpleNamespace(is_configured=False)
    for name in ("_sync_db", "log", "log_section", "log_step", "log_step_header",
                 "_ensure_project_env", "_update_title_from_idea", "_send_deep_research_telegram",
                 "_sync_agent_prompt_bases", "_install_builtin_skills",
                 "_bootstrap_citations_from_deep_research", "_fallback_literature_survey"):
        monkeypatch.setattr(p, name, MagicMock(), raising=False)
    for name in ("idea.md", "project_context.md"):
        (tmp_path / name).write_text("original")
    report = tmp_path / "deep_research.md"
    marker = tmp_path / "research_degraded.txt"
    marker.write_text("Partial survey")
    report.write_text("# Literature Survey (fallback)\nExisting references.")
    def paid_survey(**kwargs):
        report.write_text("# Deep Research Report\n**Generated**: today\n**Duration**: 1m\n---\nNew research.")
        return str(report)
    with patch("ark.deep_research.run_deep_research", return_value="") as failed:
        assert p._should_run_research_phase()
        p._run_research_phase()
        failed.assert_called_once()
        assert marker.is_file() and "Existing references." in report.read_text()
        p._fallback_literature_survey.assert_not_called()
    with patch("ark.deep_research.run_deep_research", side_effect=paid_survey) as completed:
        assert p._should_run_research_phase()
        p._run_research_phase()
        completed.assert_called_once()
        assert not marker.exists()
        assert not p._should_run_research_phase()


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
