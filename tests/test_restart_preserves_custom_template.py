"""Restart must not delete a user-uploaded custom template.

Before this fix, ``_clean_project_state`` in ``website.dashboard.routes``
deleted every file in ``paper/`` whose suffix wasn't in
``{.cls, .sty, .bst}``. ``api_restart_project`` only re-copied a venue
template when ``venue_format != "custom"``, so restarting a custom-template
project left ``paper/`` with only style files — no ``main.tex``, no
``references.bib``, no ``template_manifest.yaml`` — and the next pipeline
run had nothing to build from.
"""

from pathlib import Path

import yaml


def _setup_custom_template_project(pdir: Path):
    (pdir / "paper").mkdir(parents=True, exist_ok=True)
    (pdir / "paper" / "main.tex").write_text(
        "\\documentclass{article}\n\\title{X}\n\\begin{document}\n\\end{document}\n"
    )
    (pdir / "paper" / "references.bib").write_text("@article{foo, title={x}}\n")
    (pdir / "paper" / "neurips_2026.sty").write_text("% style file\n")
    (pdir / "paper" / "checklist.tex").write_text("% user checklist\n")
    (pdir / "paper" / "template_manifest.yaml").write_text(yaml.dump({
        "source": "user_uploaded_zip",
        "detected_venue_format": "neurips",
    }))
    # Some generated artifacts that SHOULD be wiped.
    (pdir / "auto_research" / "state").mkdir(parents=True)
    (pdir / "auto_research" / "state" / "findings.yaml").write_text("findings: []\n")
    (pdir / "auto_research" / "logs").mkdir(parents=True)
    (pdir / "auto_research" / "logs" / "run.log").write_text("log\n")
    (pdir / "agents").mkdir()
    (pdir / "agents" / "writer.prompt").write_text("stale prompt\n")


def _setup_non_custom_project(pdir: Path):
    # Same shape, but no template_manifest.yaml — the signal that this is a
    # user-uploaded custom template.
    (pdir / "paper").mkdir(parents=True, exist_ok=True)
    (pdir / "paper" / "main.tex").write_text(
        "\\documentclass{article}\n\\title{X}\n\\begin{document}\n\\end{document}\n"
    )
    (pdir / "paper" / "references.bib").write_text("@article{foo, title={x}}\n")
    (pdir / "paper" / "neurips_2026.sty").write_text("% style file\n")


class TestCleanProjectStateCustomTemplate:
    def test_preserves_uploaded_skeleton(self, tmp_path):
        from website.dashboard.routes import _clean_project_state

        _setup_custom_template_project(tmp_path)
        _clean_project_state(tmp_path)

        # Uploaded skeleton survives.
        assert (tmp_path / "paper" / "main.tex").exists(), \
            "main.tex must survive restart for custom templates"
        assert (tmp_path / "paper" / "references.bib").exists()
        assert (tmp_path / "paper" / "template_manifest.yaml").exists()
        assert (tmp_path / "paper" / "checklist.tex").exists(), \
            "user-supplied supporting .tex files must survive"
        assert (tmp_path / "paper" / "neurips_2026.sty").exists()

        # Regenerable state is wiped as before.
        assert not (tmp_path / "auto_research" / "state" / "findings.yaml").exists()
        assert not (tmp_path / "agents" / "writer.prompt").exists()

    def test_noncustom_behavior_unchanged(self, tmp_path):
        """Without a template_manifest.yaml, .tex/.bib are still cleaned —
        we rely on api_restart_project calling copy_venue_template to
        restore them, so historical behaviour is preserved.
        """
        from website.dashboard.routes import _clean_project_state

        _setup_non_custom_project(tmp_path)
        _clean_project_state(tmp_path)

        # Style file kept; generated .tex/.bib removed (restore comes from
        # copy_venue_template afterwards).
        assert (tmp_path / "paper" / "neurips_2026.sty").exists()
        assert not (tmp_path / "paper" / "main.tex").exists()
        assert not (tmp_path / "paper" / "references.bib").exists()

    def test_figures_dir_reset(self, tmp_path):
        from website.dashboard.routes import _clean_project_state

        _setup_custom_template_project(tmp_path)
        figs = tmp_path / "paper" / "figures"
        figs.mkdir()
        (figs / "stale.pdf").write_bytes(b"%PDF stale")
        _clean_project_state(tmp_path)
        assert figs.exists()
        assert not (figs / "stale.pdf").exists()
