"""Regression tests for Deep Research output assembly.

Earlier code took only ``outputs[0].text`` and dropped the rest of the
report. On a real DR interaction we observed 6 outputs — 4 TextContent
(body + Sources list) and 2 ImageContent — totalling ~46k chars + two
PNG figures. The old code kept the first ~6.7k chars and discarded the
References section, which downstream broke the citation-bootstrap step
and produced PDFs with no References.

These tests exercise ``_assemble_report`` and ``_apply_url_annotations``
against synthetic objects shaped like the real google-genai
TextContent/ImageContent pydantic models (only the attrs we touch).
"""

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ark.deep_research import _apply_url_annotations, _assemble_report, run_deep_research


def _text(text: str, annotations=None) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text, annotations=annotations)


def _image(data: bytes, mime: str = "image/png") -> SimpleNamespace:
    return SimpleNamespace(
        type="image",
        data=base64.b64encode(data).decode(),
        mime_type=mime,
    )


def _ann(start: int, end: int, url: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="url_citation", start_index=start, end_index=end, url=url, title=None
    )


def test_completed_google_report_identifies_the_actual_interaction(tmp_path, monkeypatch):
    from google import genai

    api = Mock()
    api.create.return_value = SimpleNamespace(id="fixture-interaction")
    api.get.return_value = SimpleNamespace(
        status="completed",
        usage={"grounding_tool_count": [{"type": "google_search", "count": 3}]},
        steps=[SimpleNamespace(type="model_output", content=[_text("Study evidence.")])],
    )
    monkeypatch.setattr(genai, "Client", lambda **kwargs: SimpleNamespace(interactions=api))
    report_path = run_deep_research({"title": "Study"}, tmp_path, custom_query="Query", api_key="dummy")
    api.get.assert_called_once_with("fixture-interaction")
    report = Path(report_path).read_text()
    header, body = report.split("\n---\n", 1)
    assert "**Provider**: Google Gemini" in header
    assert f"**Agent**: {api.create.call_args.kwargs['agent']}" in header
    assert "**Interaction ID**: fixture-interaction" in header
    assert body.strip() == "Study evidence."
    receipt = json.loads((tmp_path / "deep_research_receipt.json").read_text())
    assert receipt == {
        "provider": "google", "agent": api.create.call_args.kwargs["agent"],
        "interaction_id": "fixture-interaction", "status": "completed",
        "report_sha256": hashlib.sha256(Path(report_path).read_bytes()).hexdigest(),
        "usage": api.get.return_value.usage,
    }
    query = api.create.call_args.kwargs["input"]
    assert query.startswith("Query") and "original publication" in query
    assert "## Primary studies" in query and "conditions closest to this project" in query


def test_existing_interaction_restores_raw_steps_and_cross_block_grounding(tmp_path, monkeypatch):
    from google import genai

    api = Mock()
    prefix = "Préface. "
    body = "Study [cite: 1, 2] continues."
    start = len((prefix + "Study ").encode())
    end = start + len("[cite: 1, 2]")
    annotations = [
        {"type": "url_citation", "start_index": start, "end_index": end, "url": url}
        for url in ("https://one.example/", "https://two.example/")
    ]
    api.get.return_value = SimpleNamespace(status="completed", steps=[
        {"type": "user_input", "content": [{"type": "text", "text": "Private input"}]},
        {"type": "thought", "content": [{"type": "text", "text": "Private thought"}]},
        {"type": "model_output", "content": [{"type": "text", "text": prefix, "annotations": annotations}]},
        {"type": "model_output", "content": [{"type": "image", "data": "YWJj", "mime_type": "image/png"},
                                               {"type": "text", "text": body}]},
    ], usage={"search_count": 2})
    monkeypatch.setattr(genai, "Client", lambda **_: SimpleNamespace(interactions=api))
    (tmp_path / "research_degraded.txt").touch()
    (tmp_path / "deep_research.md").write_text("# Literature Survey (fallback)")

    path = run_deep_research({}, tmp_path, api_key="dummy", interaction_id="old-id")
    text = Path(path).read_text()
    assert prefix in text and "Private" not in text
    assert "[cite: 1, 2](https://one.example/) [source](https://two.example/) continues." in text
    assert text.index(prefix) < text.index("![Figure 1]") < text.index("Study [cite")
    assert (tmp_path / "deep_research_assets/figure_01.png").read_bytes() == b"abc"
    assert not (tmp_path / "research_degraded.txt").exists()
    api.create.assert_not_called()
    api.get.assert_called_once_with("old-id")


def test_interrupted_google_request_is_polled_once_on_continue(tmp_path, monkeypatch):
    from google import genai

    api = Mock()
    api.create.return_value = SimpleNamespace(id="paid-id")
    api.get.side_effect = [TimeoutError("unavailable"), SimpleNamespace(
        status="completed", outputs=[_text("Completed survey.")])]
    monkeypatch.setattr(genai, "Client", lambda **_: SimpleNamespace(interactions=api))
    (tmp_path / "idea.md").write_text("Original idea")
    # Initializer can emit a custom query, whereas Continue builds it from idea.md.
    assert not run_deep_research({}, tmp_path, "initializer's prompt", "dummy")
    pending = tmp_path / "deep_research_interaction.json"
    assert json.loads(pending.read_text())["interaction_id"] == "paid-id"
    (tmp_path / "deep_research.md").write_text("# Literature Survey (fallback)")
    (tmp_path / "research_degraded.txt").touch()
    path = run_deep_research({}, tmp_path, "reconstructed prompt", "dummy")
    assert "Completed survey." in Path(path).read_text()
    assert not pending.exists()
    # Already complete, including if process stopped just before marker cleanup.
    (tmp_path / "research_degraded.txt").touch()
    assert run_deep_research({}, tmp_path, "reconstructed prompt", "dummy") == path
    assert not (tmp_path / "research_degraded.txt").exists()
    api.create.assert_called_once()
    assert [call.args for call in api.get.call_args_list] == [("paid-id",), ("paid-id",)]


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "not_found"])
def test_terminal_interaction_allows_a_new_submission_on_next_attempt(tmp_path, monkeypatch, terminal):
    from google import genai

    class NotFound(Exception):
        status_code = 404
    api = Mock()
    api.create.side_effect = [SimpleNamespace(id="expired"), SimpleNamespace(id="replacement")]
    api.get.side_effect = [
        NotFound() if terminal == "not_found" else SimpleNamespace(status=terminal),
        SimpleNamespace(status="completed", outputs=[_text("New report.")]),
    ]
    monkeypatch.setattr(genai, "Client", lambda **_: SimpleNamespace(interactions=api))
    assert not run_deep_research({}, tmp_path, api_key="dummy")
    assert not (tmp_path / "deep_research_interaction.json").exists()
    assert run_deep_research({}, tmp_path, api_key="dummy")
    assert [call.args for call in api.get.call_args_list] == [("expired",), ("replacement",)]


def test_concurrent_workers_share_one_google_interaction(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from google import genai

    entered, release = Event(), Event()
    api = Mock()
    api.create.return_value = SimpleNamespace(id="shared")
    def respond(interaction_id):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(status="completed", outputs=[_text("Shared survey.")])
    api.get.side_effect = respond
    monkeypatch.setattr(genai, "Client", lambda **_: SimpleNamespace(interactions=api))
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run_deep_research, {}, tmp_path, api_key="dummy")
        assert entered.wait(5)
        second = pool.submit(run_deep_research, {}, tmp_path, api_key="dummy")
        release.set()
        assert first.result() == second.result() == str(tmp_path / "deep_research.md")
    api.create.assert_called_once()
    api.get.assert_called_once_with("shared")


# ─────────────────────────────────────────────────────────────────────
# _apply_url_annotations
# ─────────────────────────────────────────────────────────────────────


def test_annotations_link_cite_marker():
    text = "Background context [cite: 1] continues here."
    # `[cite: 1]` lives at offsets 19..28 (9 chars).
    assert text[19:28] == "[cite: 1]"
    out = _apply_url_annotations(text, [_ann(19, 28, "https://example.com/x")])
    assert out == (
        "Background context [cite: 1](https://example.com/x) continues here."
    )


def test_annotations_apply_in_reverse_order():
    """When two cites share a sentence, both must rewrite correctly —
    naive forward iteration would shift later indices."""
    text = "A [cite: 1] B [cite: 2] C"
    s1, e1 = text.find("[cite: 1]"), text.find("[cite: 1]") + len("[cite: 1]")
    s2, e2 = text.find("[cite: 2]"), text.find("[cite: 2]") + len("[cite: 2]")
    out = _apply_url_annotations(text, [
        _ann(s1, e1, "https://a/"),
        _ann(s2, e2, "https://b/"),
    ])
    assert out == "A [cite: 1](https://a/) B [cite: 2](https://b/) C"


def test_annotations_use_byte_offsets_not_char_offsets():
    """The API returns UTF-8 *byte* offsets, not character indices.

    With an em-dash (3 bytes / 1 char) before the marker, the byte offset
    of ``]`` runs ahead of the character offset. The old char-index code
    would slice past the marker and jam ``(url)`` into the next word
    (the real symptom: ``[cite: 13, 15]. Th(https://…)is``). The fix must
    insert ``(url)`` immediately after ``]``.
    """
    text = "Result—per cite [cite: 1] This continues."
    marker = "[cite: 1]"
    char_start = text.index(marker)
    char_end = char_start + len(marker)
    data = text.encode("utf-8")
    byte_start = len(text[:char_start].encode("utf-8"))
    byte_end = len(text[:char_end].encode("utf-8"))
    # The em-dash makes byte offsets larger than char offsets.
    assert byte_start == char_start + 2
    assert byte_end == char_end + 2

    out = _apply_url_annotations(text, [_ann(byte_start, byte_end, "http://x")])
    # `(url)` lands right after `]`, not inside the following word.
    assert "[cite: 1](http://x) This continues." in out
    assert out == "Result—per cite [cite: 1](http://x) This continues."
    # And crucially NOT jammed into the next word.
    assert "Th(http://x)" not in out


def test_annotations_skip_non_url_citation():
    """Future API versions may add other annotation kinds — ignore them."""
    text = "Hello [cite: 1] world"
    fake = SimpleNamespace(
        type="something_else", start_index=6, end_index=15, url="https://x/"
    )
    out = _apply_url_annotations(text, [fake])
    assert out == text


def test_annotations_skip_invalid_offsets():
    text = "short"
    out = _apply_url_annotations(text, [_ann(100, 200, "https://x/")])
    assert out == text


def test_annotations_empty_or_none():
    assert _apply_url_annotations("hello", None) == "hello"
    assert _apply_url_annotations("hello", []) == "hello"


# ─────────────────────────────────────────────────────────────────────
# _assemble_report
# ─────────────────────────────────────────────────────────────────────


def test_concatenates_all_text_outputs(tmp_path):
    """The bug we're fixing: only outputs[0] survived. Now all should."""
    outputs = [
        _text("# Title\n\nFirst section body."),
        _text("## Second section\n\nMore body."),
        _text("**Sources:** 1. Foo 2. Bar"),
    ]
    md = _assemble_report(outputs, tmp_path / "assets")
    assert "First section body." in md
    assert "Second section" in md
    assert "Sources:" in md
    # Order is preserved.
    assert md.find("First section") < md.find("Second section") < md.find("Sources")


def test_skips_empty_text_outputs(tmp_path):
    outputs = [_text("real content"), _text(""), _text("   "), _text("more")]
    md = _assemble_report(outputs, tmp_path / "assets")
    assert "real content" in md
    assert "more" in md
    # No double blank-line artefacts from empty entries.
    assert "\n\n\n\n" not in md


def test_saves_images_as_png(tmp_path):
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    outputs = [
        _text("Before figure."),
        _image(png_bytes, "image/png"),
        _text("After figure."),
    ]
    assets = tmp_path / "deep_research_assets"
    md = _assemble_report(outputs, assets)

    # File written with sequential numbering
    saved = assets / "figure_01.png"
    assert saved.exists()
    assert saved.read_bytes() == png_bytes
    # Markdown references via the assets/ subfolder name (relative)
    assert "deep_research_assets/figure_01.png" in md
    # And the image reference appears between the two text blocks
    assert md.find("Before figure.") < md.find("figure_01.png") < md.find("After figure.")


def test_multiple_images_get_sequential_names(tmp_path):
    outputs = [
        _image(b"\x89PNG\r\n\x1a\n" + b"\x01" * 16),
        _text("middle"),
        _image(b"\x89PNG\r\n\x1a\n" + b"\x02" * 16),
    ]
    assets = tmp_path / "assets"
    md = _assemble_report(outputs, assets)
    assert (assets / "figure_01.png").exists()
    assert (assets / "figure_02.png").exists()
    assert "figure_01.png" in md
    assert "figure_02.png" in md


def test_image_decode_failure_does_not_abort(tmp_path):
    """A single corrupt image must not lose the rest of the report."""
    bad = SimpleNamespace(type="image", data="not-valid-base64!!!", mime_type="image/png")
    outputs = [_text("body 1"), bad, _text("body 2")]
    md = _assemble_report(outputs, tmp_path / "assets")
    assert "body 1" in md
    assert "body 2" in md
    assert "failed to decode image" in md


def test_unknown_output_types_are_ignored(tmp_path):
    """Future API additions (tool calls, etc.) shouldn't crash us."""
    weird = SimpleNamespace(type="tool_call", name="search", args={})
    outputs = [_text("body"), weird, _text("more body")]
    md = _assemble_report(outputs, tmp_path / "assets")
    assert "body" in md
    assert "more body" in md


def test_annotations_resolved_in_concatenated_output(tmp_path):
    """End-to-end: cite markers in different segments all get resolved
    (each segment uses its own per-segment indices)."""
    seg1 = "Para 1 [cite: 1] continues."
    s, e = seg1.find("[cite: 1]"), seg1.find("[cite: 1]") + len("[cite: 1]")
    seg2 = "Para 2 [cite: 2] continues."
    s2, e2 = seg2.find("[cite: 2]"), seg2.find("[cite: 2]") + len("[cite: 2]")
    outputs = [
        _text(seg1, [_ann(s, e, "https://one/")]),
        _text(seg2, [_ann(s2, e2, "https://two/")]),
    ]
    md = _assemble_report(outputs, tmp_path / "assets")
    assert "[cite: 1](https://one/)" in md
    assert "[cite: 2](https://two/)" in md


def test_empty_outputs_returns_empty_string(tmp_path):
    assert _assemble_report([], tmp_path / "assets") == ""
    assert _assemble_report(None, tmp_path / "assets") == ""


# ── May-2026 `steps` schema (google-genai >= 2.0) ────────────────────

from ark.deep_research import _model_output_contents


def test_steps_schema_flattens_model_output_content():
    """v2 interactions carry content in steps[].content; thought/tool steps
    carry none. Items must come back flat, in order, ready for
    _assemble_report."""
    it = SimpleNamespace(steps=[
        SimpleNamespace(type="thought", content=[]),
        SimpleNamespace(type="model_output", content=[_text("part one")]),
        SimpleNamespace(type="tool_call", content=[_text("IGNORED")]),
        SimpleNamespace(type="model_output",
                        content=[_text("part two"), _image(b"png")]),
    ])
    items = _model_output_contents(it)
    assert [getattr(i, "type") for i in items] == ["text", "text", "image"]
    assert items[0].text == "part one" and items[1].text == "part two"


def test_legacy_outputs_schema_still_supported():
    """Old SDKs / recorded fixtures expose .outputs — fallback must work."""
    it = SimpleNamespace(outputs=[_text("legacy body")])
    assert [i.text for i in _model_output_contents(it)] == ["legacy body"]


def test_steps_schema_assembles_into_report(tmp_path):
    it = SimpleNamespace(steps=[
        SimpleNamespace(type="model_output", content=[_text("Body."), _text("Sources.")]),
    ])
    out = _assemble_report(_model_output_contents(it), tmp_path / "assets")
    assert "Body." in out and "Sources." in out
