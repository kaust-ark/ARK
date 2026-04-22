"""End-to-end: run the preprocessor against a real user-uploaded template.

Uses the actual NeurIPS 2026 template extracted into project
``d9b7fab8-b466-40ba-978c-2fe464dae9bc`` (the one whose title was getting
written as "Formatting Instructions For NeurIPS 2026" in the user's bug
report).  Verifies that after preprocessing the sanitised ``main.tex``:

* drops the template's hardcoded title, author, and abstract
* removes the 300+ lines of instructional boilerplate between
  ``\\end{abstract}`` and ``\\section*{References}``
* emits a manifest that the writer prompt renderer can consume
* still parses as LaTeX (pdflatex compile) — we don't assert a successful
  compile because the reference environment may lack fonts, but we do
  assert the file isn't structurally broken (balanced braces, matching
  ``\\begin{document}`` / ``\\end{document}``)
"""

import shutil
from pathlib import Path

import pytest
import yaml

from ark.template_preprocess import (
    preprocess_custom_template,
    render_custom_template_notes,
)


REAL_TEMPLATE_DIR = Path(
    "/home/xinj/ARK/.ark/data/projects/55338527-08e5-460b-9150-bd2f48d831af/"
    "d9b7fab8-b466-40ba-978c-2fe464dae9bc/paper"
)


def _has_real_template() -> bool:
    return (REAL_TEMPLATE_DIR / "main.tex").exists()


@pytest.fixture
def fake_uploaded_paper(tmp_path):
    """Copy the real NeurIPS template into a tmp paper_dir — mimics what the
    zip extractor would leave behind after ``_extract_and_validate_template``
    runs.
    """
    if not _has_real_template():
        pytest.skip("Real d9b7fab8 template not present on this machine")
    target = tmp_path / "paper"
    target.mkdir()
    # Copy the interesting files (main.tex, checklist.tex, style)
    for name in ("main.tex", "checklist.tex", "neurips_2026.sty"):
        src = REAL_TEMPLATE_DIR / name
        if src.exists():
            shutil.copy(src, target / name)
    # Create an empty references.bib so the extract validation is happy.
    (target / "references.bib").write_text("")
    return target


class TestRealTemplateE2E:
    def test_preprocess_strips_placeholder_title(self, fake_uploaded_paper):
        manifest = preprocess_custom_template(
            fake_uploaded_paper, venue_hint="neurips_2026"
        )
        out = (fake_uploaded_paper / "main.tex").read_text()
        # The exact bug from the user's report: this title must not survive
        assert "Formatting Instructions For NeurIPS 2026" not in out
        assert (
            manifest["detected_placeholders"]["title_placeholder"]
            == "Formatting Instructions For NeurIPS 2026"
        )

    def test_preprocess_strips_hippocampus_author(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper)
        out = (fake_uploaded_paper / "main.tex").read_text()
        assert "Hippocampus" not in out
        assert "hippo@cs.cranberry-lemon.edu" not in out

    def test_preprocess_empties_placeholder_abstract(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper)
        out = (fake_uploaded_paper / "main.tex").read_text()
        # Template's placeholder abstract text must be gone
        assert "abstract paragraph should be indented" not in out
        # But the environment itself must still exist for the writer to fill
        assert "\\begin{abstract}" in out
        assert "\\end{abstract}" in out

    def test_preprocess_removes_submission_instructions(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper)
        out = (fake_uploaded_paper / "main.tex").read_text()
        # These are venue instructions from the template body (lines 117+ of
        # the original main.tex), not paper content.  They must not survive.
        assert "Submission of papers to NeurIPS 2026" not in out
        assert "Please read the instructions below carefully" not in out
        assert "General formatting instructions" not in out
        assert "Retrieval of style files" not in out
        # The writer's empty section skeleton should take their place
        assert "\\section{Introduction}" in out
        assert "TO BE WRITTEN" in out

    def test_preprocess_preserves_structural_scaffolding(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper)
        out = (fake_uploaded_paper / "main.tex").read_text()
        # The overall LaTeX scaffold the writer relies on must survive
        for required in (
            "\\documentclass{article}",
            "\\usepackage{neurips_2026}",
            "\\begin{document}",
            "\\maketitle",
            "\\begin{abstract}",
            "\\end{abstract}",
            "\\section*{References}",
            "\\appendix",
            "\\input{checklist.tex}",
            "\\end{document}",
        ):
            assert required in out, f"{required!r} missing from sanitized main.tex"

    def test_preprocess_strips_appendix_instruction_prose(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper)
        out = (fake_uploaded_paper / "main.tex").read_text()
        # The NeurIPS template's appendix "Technical appendices" section is
        # example prose, not paper content — must not survive.
        assert "Technical appendices with additional results" not in out
        assert "optional reading" not in out
        assert "\\section{Technical appendices and supplementary material}" not in out
        # But the \appendix command and \input{checklist.tex} must both
        # remain so the NeurIPS checklist section still renders.
        assert "\\appendix" in out
        assert "\\input{checklist.tex}" in out

    def test_preprocess_balanced_braces(self, fake_uploaded_paper):
        """After preprocessing, the tex file should have balanced braces.
        A structurally broken file would crash pdflatex downstream.
        """
        preprocess_custom_template(fake_uploaded_paper)
        out = (fake_uploaded_paper / "main.tex").read_text()
        # Count unescaped braces
        depth = 0
        i = 0
        while i < len(out):
            ch = out[i]
            if ch == '\\' and i + 1 < len(out):
                i += 2
                continue
            if ch == '%':
                # Skip to end of line
                nl = out.find('\n', i)
                i = len(out) if nl < 0 else nl
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth < 0:
                    pytest.fail(f"unbalanced braces: extra '}}' at char {i}")
            i += 1
        assert depth == 0, f"unbalanced braces: {depth} unclosed '{{'"

    def test_manifest_captures_must_preserve_files(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper, venue_hint="neurips_2026")
        manifest = yaml.safe_load(
            (fake_uploaded_paper / "template_manifest.yaml").read_text()
        )
        must_preserve = manifest["constraints"].get("must_preserve_files", [])
        assert "neurips_2026.sty" in must_preserve
        # \input{checklist.tex} is referenced from main.tex so preprocessor
        # should flag it.
        assert any("checklist" in f for f in must_preserve)

    def test_manifest_detects_nine_page_limit(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper)
        manifest = yaml.safe_load(
            (fake_uploaded_paper / "template_manifest.yaml").read_text()
        )
        assert manifest["constraints"].get("page_limit") == 9

    def test_manifest_records_removed_headers(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper)
        manifest = yaml.safe_load(
            (fake_uploaded_paper / "template_manifest.yaml").read_text()
        )
        removed = (manifest["boilerplate_removed"] or {}).get(
            "removed_section_headers", []
        )
        # Several of the template's instruction-only sections should show up
        assert any("Submission" in h for h in removed)

    def test_writer_notes_block_rendered(self, fake_uploaded_paper):
        preprocess_custom_template(fake_uploaded_paper)
        notes = render_custom_template_notes(fake_uploaded_paper)
        assert "Custom Template Notice" in notes
        assert "neurips_2026.sty" in notes
        assert "9 pages" in notes
        # Warns the writer away from re-introducing template instructions
        assert "Submission" in notes


# ---------------------------------------------------------------------------
#  Webapp upload path + pipeline lifecycle integration
# ---------------------------------------------------------------------------

def _build_fake_upload_zip() -> bytes:
    """Re-create the user's upload by zipping the real template files."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("main.tex", "checklist.tex", "neurips_2026.sty"):
            src = REAL_TEMPLATE_DIR / name
            if src.exists():
                zf.write(src, arcname=name)
    return buf.getvalue()


class TestWebappUploadIntegration:
    """The webapp's ``_extract_and_validate_template`` must call the
    preprocessor, produce a compilable main.tex, and emit a manifest."""

    def test_full_upload_path(self, tmp_path):
        if not _has_real_template():
            pytest.skip("Real template not present")
        from website.dashboard.routes import _extract_and_validate_template

        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        err = _extract_and_validate_template(_build_fake_upload_zip(), paper_dir)
        assert err is None, f"extract/preprocess/validate failed: {err}"

        # Preprocess actually ran
        tex = (paper_dir / "main.tex").read_text()
        assert "Formatting Instructions For NeurIPS 2026" not in tex
        assert "Hippocampus" not in tex
        assert "\\section{Introduction}" in tex

        # Manifest emitted
        manifest_path = paper_dir / "template_manifest.yaml"
        assert manifest_path.exists()
        manifest = yaml.safe_load(manifest_path.read_text())
        assert manifest["constraints"].get("page_limit") == 9


class TestFullLifecycleTitlePropagation:
    """After upload + preprocess, when the pipeline's
    ``_update_title_from_idea`` runs, the LLM-generated title must flow
    into main.tex AND the writer/reviewer prompts must carry both the
    new title and the custom-template notes block."""

    def test_lifecycle_upload_then_title_generation(self, tmp_path):
        if not _has_real_template():
            pytest.skip("Real template not present")
        from unittest.mock import MagicMock, patch
        from website.dashboard.routes import _extract_and_validate_template
        from ark.pipeline import PipelineMixin

        # --- Arrange: layout a project directory like the webapp would ---
        code_dir = tmp_path / "project"
        paper = code_dir / "paper"
        agents = code_dir / "agents"
        state = code_dir / "auto_research" / "state"
        for d in (paper, agents, state):
            d.mkdir(parents=True)
        err = _extract_and_validate_template(_build_fake_upload_zip(), paper)
        assert err is None

        (state / "idea.md").write_text(
            "Position paper on compute-aware deployment of world models.\n"
        )
        config_path = code_dir / "config.yaml"
        config_path.write_text(yaml.dump({
            "title": "",
            "venue_format": "custom",
            "venue": "Customized",
            "venue_pages": 9,
            "latex_dir": "paper",
            "figures_dir": "paper/figures",
        }))

        # --- Act: bind the real pipeline methods onto a stub ---
        stub = MagicMock(spec=PipelineMixin)
        stub.code_dir = code_dir
        stub.latex_dir = paper
        stub.agents_dir = agents
        stub.state_dir = state
        stub.project_name = "d9b7fab8"
        stub._project_id = "d9b7fab8-b466-40ba-978c-2fe464dae9bc"
        stub.config = yaml.safe_load(config_path.read_text())
        stub._sync_db = MagicMock()
        stub.log = MagicMock()
        stub._update_title_from_idea = PipelineMixin._update_title_from_idea.__get__(stub)
        stub._sync_paper_metadata = PipelineMixin._sync_paper_metadata.__get__(stub)

        generated = "Compute-Aware Deployment of World Models"
        with patch("ark.pipeline._generate_title_via_llm", return_value=generated):
            stub._update_title_from_idea()

        # --- Assert: main.tex has the real title ---
        tex = (paper / "main.tex").read_text()
        assert generated in tex
        assert "Formatting Instructions" not in tex
        # The sentinel placeholder put in by sanitize_tex_metadata must be
        # replaced by the real title.
        assert "ARK Pending Title" not in tex

        # --- Assert: writer prompt has title + custom template notes ---
        writer = (agents / "writer.prompt").read_text()
        assert generated in writer
        assert "Custom Template Notice" in writer
        assert "9 pages" in writer
        assert "neurips_2026.sty" in writer
        # No unresolved placeholders leaked through
        assert "{CUSTOM_TEMPLATE_NOTES}" not in writer
        assert "{PAPER_TITLE}" not in writer

        # --- Assert: reviewer prompt also carries the custom notes ---
        reviewer = (agents / "reviewer.prompt").read_text()
        assert "Custom Template Notice" in reviewer


class TestNonCustomTemplatePlaceholderIsEmpty:
    """When there's no template_manifest.yaml, the {CUSTOM_TEMPLATE_NOTES}
    placeholder must render as empty string — no literal placeholder text
    should leak into the prompt."""

    def test_empty_when_no_manifest(self, tmp_path):
        from unittest.mock import MagicMock
        from ark.pipeline import PipelineMixin

        code_dir = tmp_path / "project"
        paper = code_dir / "paper"
        agents = code_dir / "agents"
        for d in (paper, agents):
            d.mkdir(parents=True)
        # Minimal main.tex so _sync_paper_metadata's title-rewrite branch
        # has something to look at
        (paper / "main.tex").write_text(
            "\\documentclass{article}\n\\title{Old}\n\\begin{document}\n"
        )

        stub = MagicMock(spec=PipelineMixin)
        stub.code_dir = code_dir
        stub.latex_dir = paper
        stub.agents_dir = agents
        stub.project_name = "plainproj"
        stub._project_id = "plainproj"
        stub.config = {"venue_format": "neurips_2026", "venue_pages": 9}
        stub.log = MagicMock()
        stub._sync_paper_metadata = PipelineMixin._sync_paper_metadata.__get__(stub)

        stub._sync_paper_metadata("Plain Title")

        writer = (agents / "writer.prompt").read_text()
        assert "Plain Title" in writer
        # Without a manifest, there's no Custom Template Notice block
        assert "Custom Template Notice" not in writer
        # And no literal placeholder leaked
        assert "{CUSTOM_TEMPLATE_NOTES}" not in writer
