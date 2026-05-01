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
from pathlib import Path
from types import SimpleNamespace

from ark.deep_research import _apply_url_annotations, _assemble_report


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
