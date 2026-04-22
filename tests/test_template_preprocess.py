"""Unit tests for ark.template_preprocess.

Covers:
- sanitize_tex_metadata: empties title/author/abstract placeholders
- detect_boilerplate_span: finds instructional region between
  \\end{abstract} / \\maketitle and the bibliography / appendix
- preprocess_custom_template: orchestrates the full pipeline and writes a
  manifest
- render_custom_template_notes: builds the agent-prompt block from a
  manifest, returning "" when no manifest exists
"""

from pathlib import Path

import pytest
import yaml

from ark.template_preprocess import (
    _TITLE_SENTINEL,
    _ABSTRACT_SENTINEL,
    build_manifest,
    detect_appendix_boilerplate_span,
    detect_boilerplate_span,
    preprocess_custom_template,
    render_custom_template_notes,
    sanitize_tex_metadata,
    stub_out_appendix_boilerplate,
    stub_out_boilerplate,
    write_template_manifest,
)


# ---------------------------------------------------------------------------
#  sanitize_tex_metadata
# ---------------------------------------------------------------------------

class TestSanitizeTexMetadata:
    def test_empties_simple_title(self):
        src = r"\title{Formatting Instructions}"
        out, info = sanitize_tex_metadata(src)
        assert _TITLE_SENTINEL in out
        assert "Formatting Instructions" not in out
        assert info["title_placeholder"] == "Formatting Instructions"

    def test_empties_multiline_author_block(self):
        src = (
            "\\author{\n"
            "  David S.~Hippocampus\\\\\n"
            "  Cranberry-Lemon University\n"
            "}\n"
        )
        out, info = sanitize_tex_metadata(src)
        # After sanitisation, \author{} has empty body
        assert "\\author{}" in out
        assert "Hippocampus" not in out
        assert "David S." in info["author_placeholder"]

    def test_empties_abstract_env(self):
        src = (
            "\\begin{abstract}\n"
            "The abstract paragraph should be indented ~1/2 inch.\n"
            "\\end{abstract}\n"
        )
        out, info = sanitize_tex_metadata(src)
        assert _ABSTRACT_SENTINEL in out
        assert "should be indented" not in out
        assert "paragraph should be indented" in info["abstract_placeholder"]

    def test_preserves_non_metadata_content(self):
        src = (
            "\\documentclass{article}\n"
            "\\usepackage{amsmath}\n"
            "\\title{Old}\n"
            "\\author{}\n"
            "\\begin{document}\n"
            "\\maketitle\n"
            "Body content that must survive.\n"
            "\\end{document}\n"
        )
        out, _ = sanitize_tex_metadata(src)
        for expected in (
            "\\documentclass{article}",
            "\\usepackage{amsmath}",
            "\\begin{document}",
            "\\maketitle",
            "Body content that must survive.",
            "\\end{document}",
        ):
            assert expected in out

    def test_idempotent(self):
        src = r"\title{Old}" "\n" r"\author{}" "\n"
        once, _ = sanitize_tex_metadata(src)
        twice, info = sanitize_tex_metadata(once)
        # Running twice yields the same output (title sentinel untouched)
        assert once == twice
        # On second run, \title body is the sentinel — that's what gets
        # "detected" — but it's harmless; no placeholder was real to strip.
        assert info["title_placeholder"] == _TITLE_SENTINEL

    def test_returns_empty_info_when_no_metadata(self):
        src = "Plain text with no LaTeX metadata commands.\n"
        out, info = sanitize_tex_metadata(src)
        assert out == src
        assert info == {}


# ---------------------------------------------------------------------------
#  detect_boilerplate_span
# ---------------------------------------------------------------------------

class TestDetectBoilerplateSpan:
    def test_finds_span_between_maketitle_and_references(self):
        src = (
            "\\maketitle\n"
            "This is instructional boilerplate.\n"
            "\\section{Submission of papers}\n"
            "More filler content.\n"
            "\\section*{References}\n"
            "References go here.\n"
        )
        span = detect_boilerplate_span(src)
        assert span is not None
        start, end = span
        # Span should cover the instructional prose
        assert "This is instructional boilerplate." in src[start:end]
        assert "Submission of papers" in src[start:end]
        # And NOT cover the references section
        assert "References go here." not in src[start:end]

    def test_prefers_end_of_abstract_over_maketitle(self):
        src = (
            "\\maketitle\n"
            "\\begin{abstract}\n"
            "This abstract content should not be in the span.\n"
            "\\end{abstract}\n"
            "Boilerplate starts here.\n"
            "\\bibliography{refs}\n"
        )
        span = detect_boilerplate_span(src)
        assert span is not None
        start, _ = span
        assert "abstract content" not in src[start:]

    def test_stops_at_appendix(self):
        src = (
            "\\maketitle\n"
            "Body content.\n"
            "\\appendix\n"
            "Appendix content.\n"
        )
        span = detect_boilerplate_span(src)
        assert span is not None
        _, end = span
        assert "Appendix content" not in src[:end]

    def test_returns_none_when_no_end_marker(self):
        # No bibliography / references / appendix / \begin{ack} — span
        # is unbounded, safer to return None than blow away unknown content.
        src = (
            "\\maketitle\n"
            "Some body content with no end marker.\n"
            "\\end{document}\n"
        )
        assert detect_boilerplate_span(src) is None

    def test_returns_none_when_no_start_marker(self):
        src = (
            "\\section*{References}\n"
            "Only a references section, no maketitle.\n"
        )
        assert detect_boilerplate_span(src) is None


# ---------------------------------------------------------------------------
#  stub_out_boilerplate
# ---------------------------------------------------------------------------

class TestStubOutBoilerplate:
    def test_replaces_span_with_skeleton(self):
        src = (
            "\\maketitle\nBOILERPLATE\\section*{References}\n"
        )
        span = detect_boilerplate_span(src)
        assert span is not None
        out = stub_out_boilerplate(src, span)
        assert "BOILERPLATE" not in out
        # Skeleton should contain empty section stubs
        assert "\\section{Introduction}" in out
        assert "TO BE WRITTEN" in out
        # References section boundary preserved
        assert "\\section*{References}" in out


# ---------------------------------------------------------------------------
#  Appendix boilerplate (second-pass)
# ---------------------------------------------------------------------------

class TestDetectAppendixBoilerplateSpan:
    def test_finds_span_between_appendix_and_checklist_input(self):
        src = (
            "\\appendix\n"
            "\\section{Technical appendices}\n"
            "Template appendix instructions go here.\n"
            "\\input{checklist.tex}\n"
            "\\end{document}\n"
        )
        span = detect_appendix_boilerplate_span(src)
        assert span is not None
        start, end = span
        removed = src[start:end]
        assert "Technical appendices" in removed
        assert "Template appendix instructions" in removed
        # Must not swallow the \input{checklist.tex} line
        assert "\\input{checklist.tex}" not in removed

    def test_returns_none_when_no_appendix(self):
        src = "\\section{Body}\nNo appendix marker.\n\\end{document}\n"
        assert detect_appendix_boilerplate_span(src) is None

    def test_falls_back_to_end_of_document_when_no_input(self):
        src = (
            "\\appendix\n"
            "Template instruction text.\n"
            "\\end{document}\n"
        )
        span = detect_appendix_boilerplate_span(src)
        assert span is not None
        start, end = span
        # \end{document} should NOT be inside the removed region
        assert "\\end{document}" not in src[start:end]
        assert "Template instruction text" in src[start:end]

    def test_handles_include_too(self):
        src = (
            "\\appendix\n"
            "Appendix filler.\n"
            "\\include{checklist}\n"
        )
        span = detect_appendix_boilerplate_span(src)
        assert span is not None
        start, end = span
        assert "\\include{checklist}" not in src[start:end]


class TestStubOutAppendixBoilerplate:
    def test_replaces_span_preserves_checklist_input(self):
        src = (
            "\\appendix\n"
            "\\section{Technical appendices}\n"
            "Template appendix boilerplate text.\n"
            "\\input{checklist.tex}\n"
            "\\end{document}\n"
        )
        span = detect_appendix_boilerplate_span(src)
        assert span is not None
        out = stub_out_appendix_boilerplate(src, span)
        # Appendix boilerplate removed
        assert "Template appendix boilerplate text" not in out
        assert "\\section{Technical appendices}" not in out
        # Structural scaffolding preserved
        assert "\\appendix" in out
        assert "\\input{checklist.tex}" in out
        assert "\\end{document}" in out
        # Writer guidance inserted
        assert "template appendix instructions removed" in out.lower()


# ---------------------------------------------------------------------------
#  build_manifest + write_template_manifest
# ---------------------------------------------------------------------------

class TestBuildManifest:
    def test_manifest_records_placeholders_and_span(self, tmp_path):
        # Minimal paper_dir with a style file so constraints pick up
        # must_preserve_files.
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        (paper_dir / "neurips_2026.sty").write_text("%% empty style file\n")

        original = (
            "\\title{Placeholder}\n"
            "\\maketitle\nsome boilerplate\\section*{References}\n"
        )
        sanitized, placeholders = sanitize_tex_metadata(original)
        span = detect_boilerplate_span(sanitized)

        manifest = build_manifest(
            original_src=original,
            sanitized_src=sanitized,
            detected_placeholders=placeholders,
            boilerplate_span=span,
            paper_dir=paper_dir,
            venue_hint="neurips_2026",
        )
        assert manifest["source"] == "user_uploaded_zip"
        assert manifest["venue_hint"] == "neurips_2026"
        assert manifest["detected_placeholders"]["title_placeholder"] == "Placeholder"
        assert manifest["constraints"]["must_preserve_files"] == ["neurips_2026.sty"]
        assert manifest["boilerplate_removed"]["chars"] > 0

    def test_manifest_detects_page_limit_word(self):
        paper_dir = Path("/tmp/does-not-exist-for-test")
        src = "Papers may only be up to {\\bf nine} pages long.\n"
        # _detect_constraints reads paper_dir for .sty files; pass a tmp_path
        # with no files instead.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            manifest = build_manifest(
                original_src=src,
                sanitized_src=src,
                detected_placeholders={},
                boilerplate_span=None,
                paper_dir=Path(td),
                venue_hint="",
            )
        assert manifest["constraints"].get("page_limit") == 9

    def test_manifest_records_missing_markers(self, tmp_path):
        manifest = build_manifest(
            original_src="no markers here",
            sanitized_src="no markers here",
            detected_placeholders={},
            boilerplate_span=None,
            paper_dir=tmp_path,
            venue_hint="",
        )
        assert manifest["boilerplate_removed"] is None
        assert "warning" in manifest["writer_instructions"]

    def test_write_roundtrip(self, tmp_path):
        m = {"source": "user_uploaded_zip", "venue_hint": "neurips"}
        p = write_template_manifest(tmp_path, m)
        assert p.exists()
        loaded = yaml.safe_load(p.read_text())
        assert loaded == m


# ---------------------------------------------------------------------------
#  preprocess_custom_template (full pipeline)
# ---------------------------------------------------------------------------

class TestPreprocessCustomTemplate:
    def test_full_pipeline_on_minimal_neurips_like(self, tmp_path):
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        (paper_dir / "neurips_2026.sty").write_text("% style\n")
        main_tex = paper_dir / "main.tex"
        main_tex.write_text(
            "\\documentclass{article}\n"
            "\\usepackage{neurips_2026}\n"
            "\\title{Formatting Instructions For NeurIPS 2026}\n"
            "\\author{David S.~Hippocampus}\n"
            "\\begin{document}\n"
            "\\maketitle\n"
            "\\begin{abstract}\n"
            "Placeholder abstract text.\n"
            "\\end{abstract}\n"
            "\\section{Submission of papers to NeurIPS 2026}\n"
            "Please read the instructions...\n"
            "\\section*{References}\n"
            "[1] Foo (2020)\n"
            "\\end{document}\n"
        )
        manifest = preprocess_custom_template(paper_dir, venue_hint="neurips_2026")

        out = main_tex.read_text()
        # Title placeholder was emptied (replaced with sentinel for later sync)
        assert "Formatting Instructions For NeurIPS 2026" not in out
        assert _TITLE_SENTINEL in out
        # Author was emptied
        assert "Hippocampus" not in out
        # Abstract was emptied
        assert "Placeholder abstract text" not in out
        # Boilerplate was removed and stubs put in place
        assert "Submission of papers to NeurIPS 2026" not in out
        assert "\\section{Introduction}" in out
        assert "TO BE WRITTEN" in out
        # References section header preserved
        assert "\\section*{References}" in out

        # Manifest contains detected info
        assert manifest["detected_placeholders"]["title_placeholder"] == (
            "Formatting Instructions For NeurIPS 2026"
        )
        assert manifest["constraints"]["must_preserve_files"] == ["neurips_2026.sty"]
        assert manifest["boilerplate_removed"]["chars"] > 0
        # Manifest written to disk
        assert (paper_dir / "template_manifest.yaml").exists()

    def test_missing_main_tex_raises(self, tmp_path):
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            preprocess_custom_template(paper_dir)


# ---------------------------------------------------------------------------
#  render_custom_template_notes
# ---------------------------------------------------------------------------

class TestRenderCustomTemplateNotes:
    def test_returns_empty_string_when_no_manifest(self, tmp_path):
        assert render_custom_template_notes(tmp_path) == ""

    def test_returns_notes_when_manifest_present(self, tmp_path):
        manifest = {
            "source": "user_uploaded_zip",
            "venue_hint": "neurips_2026",
            "detected_placeholders": {},
            "constraints": {
                "must_preserve_files": ["neurips_2026.sty", "checklist.tex"],
                "page_limit": 9,
            },
            "writer_instructions": {
                "sections_to_fill": ["Introduction", "Method"],
            },
            "boilerplate_removed": {
                "chars": 5000,
                "removed_section_headers": ["Submission of papers", "Style"],
            },
        }
        write_template_manifest(tmp_path, manifest)
        out = render_custom_template_notes(tmp_path)
        assert "Custom Template Notice" in out
        assert "neurips_2026.sty" in out
        assert "checklist.tex" in out
        assert "9 pages" in out
        assert "Introduction" in out
        assert "Submission of papers" in out

    def test_notes_warn_about_residue(self, tmp_path):
        """The prompt must nudge the writer to treat surviving template
        prose (example references, unanswered checklist questions) as
        placeholder content — not as already-written paper content."""
        write_template_manifest(tmp_path, {"detected_placeholders": {}})
        out = render_custom_template_notes(tmp_path)
        assert "residue" in out.lower()
        assert "TO BE WRITTEN" in out

    def test_warning_flag_surfaces_when_markers_missing(self, tmp_path):
        manifest = {
            "boilerplate_removed": None,
            "writer_instructions": {
                "warning": "Structural markers not found",
            },
        }
        write_template_manifest(tmp_path, manifest)
        out = render_custom_template_notes(tmp_path)
        assert "warning" in out.lower()
        assert "Structural markers not found" in out

    def test_corrupt_manifest_returns_empty(self, tmp_path):
        (tmp_path / "template_manifest.yaml").write_text("!@#$%^&*(\ninvalid: [")
        assert render_custom_template_notes(tmp_path) == ""
