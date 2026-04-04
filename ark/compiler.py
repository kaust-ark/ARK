"""CompilerMixin: LaTeX compilation, figure generation, PDF-to-image conversion."""

import json
import re
import subprocess
import yaml
from pathlib import Path


class CompilerMixin:
    """Mixin providing LaTeX compilation and figure management.

    Expects self to have: latex_dir, figures_dir, code_dir, config, hooks,
    log, log_step, run_agent, state_dir.
    """

    def compile_latex_with_errors(self) -> tuple:
        """Compile LaTeX and return (success, error_string).

        On success: returns (True, "").
        On failure: returns (False, structured_errors) where structured_errors
        contains up to 5 error blocks parsed from main.log, with stderr fallback.
        """
        success = self.compile_latex()
        if success:
            return True, ""

        log_path = self.latex_dir / "main.log"
        errors = self._extract_latex_errors(log_path)

        # If log parsing found nothing, use captured stderr as fallback
        stderr = getattr(self, '_last_compile_stderr', '')
        if "no specific errors found" in errors and stderr:
            errors += f"\n\nStderr from last pdflatex run:\n{stderr[:1000]}"

        return False, errors

    def _extract_latex_errors(self, log_path: Path) -> str:
        """Parse LaTeX log for structured error messages.

        Looks for common LaTeX error patterns. Returns up to 5
        error blocks with surrounding context.
        """
        if not log_path.exists():
            return "No main.log found — compilation may not have run."

        try:
            log_text = log_path.read_text(errors="replace")
        except Exception as e:
            return f"Could not read main.log: {e}"

        lines = log_text.splitlines()
        error_markers = []
        for i, line in enumerate(lines):
            if (line.startswith("!")
                    or "Fatal error" in line
                    or "Emergency stop" in line
                    or "Undefined control sequence" in line
                    or "LaTeX Error:" in line
                    or "Package Error:" in line
                    or "Missing $ inserted" in line
                    or "Extra alignment tab" in line
                    or "Misplaced alignment tab" in line
                    or "Missing \\begin{document}" in line
                    or "File not found" in line
                    or "No file" in line and ".sty" in line
                    or "Too many }'s" in line
                    or "Runaway argument" in line
                    or "Paragraph ended before" in line):
                error_markers.append(i)

        if not error_markers:
            return "Compilation failed but no specific errors found in main.log."

        # Deduplicate nearby markers (within 3 lines)
        deduped = [error_markers[0]]
        for idx in error_markers[1:]:
            if idx - deduped[-1] > 3:
                deduped.append(idx)
        error_markers = deduped[:5]  # Cap at 5 errors

        blocks = []
        for idx in error_markers:
            start = max(0, idx - 1)
            end = min(len(lines), idx + 4)
            block = "\n".join(lines[start:end])
            blocks.append(block)

        return f"Found {len(blocks)} error(s) in main.log:\n\n" + "\n---\n".join(blocks)

    def compile_latex(self) -> bool:
        """Compile the LaTeX paper.

        On success, stores the PDF path in self._latest_pdf.
        Returns True/False for backward compat; callers that need the
        path should read self._latest_pdf.
        """
        self.log("Compiling LaTeX...")
        self._last_compile_stderr = ""
        try:
            for cmd in [
                ["pdflatex", "-interaction=nonstopmode", "main.tex"],
                ["bibtex", "main"],
                ["pdflatex", "-interaction=nonstopmode", "main.tex"],
                ["pdflatex", "-interaction=nonstopmode", "main.tex"],
            ]:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=self.latex_dir,
                )
                if result.returncode != 0 and "main.tex" in cmd:
                    self._last_compile_stderr = result.stderr[:1000] or result.stdout[-1000:]
                    self.log(f"LaTeX compilation warning: {result.stderr[:500]}")

            pdf_path = self.latex_dir / "main.pdf"
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                self._latest_pdf = pdf_path
                self._pdf_page_count = self._count_pdf_pages(pdf_path)
                self._body_page_count = self._count_body_pages(pdf_path)
                self._overfull_warnings = self._parse_overfull_warnings()
                page_info = f", {self._body_page_count:.1f} body pages ({self._pdf_page_count} total)" if self._body_page_count else ""
                overfull_info = f", {len(self._overfull_warnings)} overfull warnings" if self._overfull_warnings else ""
                self.log(f"LaTeX compiled successfully: {pdf_path} ({pdf_path.stat().st_size} bytes{page_info}{overfull_info})")
                return True
            else:
                self.log("LaTeX compilation failed: PDF not generated")
                return False
        except Exception as e:
            self.log(f"LaTeX compilation error: {e}")
            return False

    def _count_pdf_pages(self, pdf_path: Path) -> int:
        """Count total pages in a PDF file using PyMuPDF."""
        try:
            import fitz
            doc = fitz.open(str(pdf_path))
            count = len(doc)
            doc.close()
            return count
        except Exception as e:
            self.log(f"Page count check failed: {e}", "WARN")
        return 0

    def _count_body_pages(self, pdf_path: Path) -> float:
        """Count body pages (before References section) using PyMuPDF.

        Returns a float: integer part = complete pages before References,
        fractional part = how far down that page References starts (0.0–1.0).
        E.g. 6.3 means body fills 6 complete pages plus 30% of the next page.
        """
        try:
            import fitz
            doc = fitz.open(str(pdf_path))
            for i in range(len(doc)):
                page = doc[i]
                text = page.get_text()
                found = any(line.strip() == 'References' for line in text.split('\n'))
                if not found:
                    continue
                # References is on page i — measure its y-position
                page_height = page.rect.height
                for block in page.get_text("dict")["blocks"]:
                    for line_obj in block.get("lines", []):
                        line_text = "".join(s["text"] for s in line_obj.get("spans", []))
                        if line_text.strip() == "References":
                            ref_y = line_obj["bbox"][1]
                            doc.close()
                            return i + ref_y / page_height
                # Fallback: found in plain text but not in dict blocks
                doc.close()
                return float(i)
            total = len(doc)
            doc.close()
            return float(total)
        except Exception as e:
            self.log(f"Body page count failed: {e}", "WARN")
            return 0.0

    def _parse_overfull_warnings(self) -> list[str]:
        """Parse main.log for Overfull hbox warnings."""
        log_path = self.latex_dir / "main.log"
        if not log_path.exists():
            return []
        try:
            log_text = log_path.read_text(errors="replace")
            warnings = []
            for line in log_text.splitlines():
                if 'Overfull \\hbox' in line:
                    warnings.append(line.strip())
            return warnings
        except Exception:
            return []

    def _generate_figures_from_results(self) -> bool:
        """Generate figures from latest experiment results and copy to paper dir."""
        if hasattr(self.hooks, 'generate_figures_from_results'):
            return self.hooks.generate_figures_from_results(self)
        else:
            self.log("No generate_figures_from_results hook defined.", "WARN")
            return False

    def generate_figures(self) -> bool:
        """Run figure generation script."""
        if hasattr(self.hooks, 'generate_figures'):
            return self.hooks.generate_figures(self)

        self.log("Generating paper figures...", "INFO")
        script_path = self.config.get("create_figures_script", "scripts/create_paper_figures.py")
        env_name = self.config.get("conda_env", "base")
        try:
            self.figures_dir.mkdir(parents=True, exist_ok=True)

            result = subprocess.run(
                ["bash", "-c", f"source ~/.bashrc && mamba activate {env_name} && python {script_path}"],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=self.code_dir,
            )

            if result.returncode == 0:
                self.log("Figure generation completed successfully", "INFO")
                self._precheck_figure_quality()
                return True
            else:
                self.log(f"Figure generation warning: {result.stderr[:500]}", "WARN")
                return True  # Continue even with warnings
        except Exception as e:
            self.log(f"Figure generation error: {e}", "ERROR")
            return False

    def _precheck_figure_quality(self):
        """Pre-check figure quality before reviewer sees the paper."""
        self.log(">>> Figure quality pre-check...", "PRECHECK")
        try:
            from PIL import Image

            issues = []
            for fig_path in sorted(self.figures_dir.glob("fig*.png")):
                img = Image.open(fig_path)
                width, height = img.size
                aspect_ratio = height / width

                if aspect_ratio < 0.35:
                    issues.append(f"  Warning: {fig_path.name}: aspect ratio too flat ({aspect_ratio:.2f}), recommend height >= width * 0.4")
                if width < 600:
                    issues.append(f"  Warning: {fig_path.name}: width too small ({width}px), recommend >= 800px")
                img.close()

            for png_path in self.figures_dir.glob("fig*.png"):
                pdf_path = png_path.with_suffix('.pdf')
                if not pdf_path.exists():
                    issues.append(f"  Warning: {png_path.stem}: missing PDF version")

            if issues:
                self.log("Figure quality issues found:", "PRECHECK")
                for issue in issues:
                    self.log(issue, "PRECHECK")
                self.log("Recommend modifying scripts/create_paper_figures.py and regenerating", "PRECHECK")
            else:
                self.log("Figure quality check passed", "PRECHECK")

        except ImportError:
            self.log("Pillow not installed, skipping figure pre-check", "WARN")
        except Exception as e:
            self.log(f"Figure pre-check error: {e}", "WARN")

    def _generate_figure_config(self) -> dict:
        """Generate figure_config.json from template geometry for plotting scripts."""
        from ark.latex_geometry import get_geometry, write_figure_config

        venue = self.config.get("venue_format", "")
        if not venue:
            venue_name = self.config.get("venue", "").lower()
            venue = venue_name if venue_name else "acmart-sigplan"

        geo = get_geometry(venue)
        config_path = self.figures_dir / "figure_config.json"
        write_figure_config(geo, config_path)
        self.log(f"Figure config generated: {config_path} (column={geo['columnwidth_in']}in, font={geo['font_size_pt']}pt)", "INFO")
        return geo

    def _run_figure_phase(self):
        """Independent Figure Phase: ensure figures are template-aware and visually correct.

        Runs BEFORE the reviewer sees the paper. Loop:
        1. Generate figure_config.json
        2. Run figure generation script (if exists)
        3. Compile LaTeX + convert to images
        4. Run figure_fixer agent to visually inspect
        5. If issues found and fixed, re-run (max 2 loops)
        """
        MAX_FIGURE_LOOPS = 2

        # Step 1: Generate geometry config
        geo = self._generate_figure_config()

        # Step 2: Run figure generation script
        script_path = self.config.get("create_figures_script", "scripts/create_paper_figures.py")
        full_script = self.code_dir / script_path
        overlap_report = None
        if full_script.exists():
            self.log_step("Running figure generation script...", "progress")
            self.generate_figures()

            # Step 2.1: Programmatic overlap detection and auto-fix
            try:
                from ark.figure_overlap import check_and_fix_figures
                overlap_report = check_and_fix_figures(
                    full_script, self.figures_dir, geo, log_fn=self.log,
                )
                if overlap_report.get("summary", {}).get("with_overlaps", 0) > 0:
                    # Re-run figure generation after fixes were applied
                    self.log_step("Regenerating figures after overlap fixes...", "progress")
                    self.generate_figures()
            except Exception as e:
                self.log(f"Overlap detection error (non-fatal): {e}", "WARN")
        else:
            self.log_step(f"No figure script at {script_path}, skipping generation", "info")

        # Step 2.5: Generate AI concept figures (Nano Banana)
        # Track which files are AI-generated so the figure fixer won't touch them
        ai_generated_files = set()
        if self.config.get("figure_generation") == "nano_banana":
            self.log_step("Generating AI concept figures (Nano Banana)...", "progress")
            # Snapshot existing files before generation
            existing_before = {f.name for f in self.figures_dir.glob("*")}
            self._generate_nano_banana_figures()
            # Any new files are AI-generated concept figures
            existing_after = {f.name for f in self.figures_dir.glob("*")}
            ai_generated_files = existing_after - existing_before
            # Also include files that were already generated in earlier phases
            for f in self.figures_dir.glob("*.png"):
                if f.name.startswith("fig_") and f.name not in ai_generated_files:
                    # Check if this was generated by PaperBanana (large, not from matplotlib)
                    # PaperBanana PNGs are typically >100KB; matplotlib PNGs are smaller
                    if f.stat().st_size > 150_000:
                        ai_generated_files.add(f.name)

        for loop in range(MAX_FIGURE_LOOPS):
            # Step 3: Compile and convert to images
            self.compile_latex()
            page_images = self.pdf_to_images()

            if not page_images:
                self.log_step("No page images available, skipping figure check", "warning")
                break

            # Step 4: Run figure_fixer agent
            images_list = "\n".join(f"- {img}" for img in page_images)
            figure_files = list(self.figures_dir.glob("*"))
            figures_list = "\n".join(f"- {f.name}" for f in figure_files if f.suffix in (".pdf", ".png", ".jpg"))

            # Include overlap report if available
            overlap_section = ""
            overlap_report_path = self.figures_dir / "overlap_report.json"
            if overlap_report_path.exists():
                try:
                    or_data = json.loads(overlap_report_path.read_text())
                    figs_with_issues = [f for f in or_data.get("figures", []) if f.get("has_overlaps")]
                    if figs_with_issues:
                        overlap_lines = []
                        for f in figs_with_issues:
                            overlap_lines.append(f"- **{f['name']}**: {f['overlap_count']} overlaps, density={f['density']}")
                            for o in f.get("overlaps", [])[:5]:
                                overlap_lines.append(f"  - {o['type1']}({o['text1']}) ↔ {o['type2']}({o['text2']}), severity={o['severity']}")
                            if f.get("suggestions"):
                                overlap_lines.append(f"  - Suggestions: {', '.join(f['suggestions'])}")
                        overlap_section = f"""
### Programmatic Overlap Report (auto-detected)
The system detected text overlaps in these figures and attempted auto-fixes.
Verify the fixes are correct. If issues remain, fix them manually.

{chr(10).join(overlap_lines)}
"""
                except Exception:
                    pass

            # Build protected files section
            protected_section = ""
            if ai_generated_files:
                protected_list = "\n".join(f"- {f}" for f in sorted(ai_generated_files))
                protected_section = f"""
### ⚠️ PROTECTED AI-Generated Concept Figures (DO NOT MODIFY)
The following figures were generated by PaperBanana/Gemini AI and must NOT be
overwritten, regenerated, or replaced by matplotlib. Do NOT modify any Python
script to output to these filenames. Only check their LaTeX placement/sizing.
{protected_list}
"""

            fixer_prompt = f"""## Figure Quality Check (Loop {loop + 1}/{MAX_FIGURE_LOOPS})

### Template Geometry Parameters
- Column width: {geo['columnwidth_in']} inches
- Full width: {geo['textwidth_in']} inches
- Base font size: {geo['font_size_pt']}pt
- Config file: {self.figures_dir}/figure_config.json

### Current Figure Files
{figures_list}
{protected_section}{overlap_section}
### PDF Page Images (use Read tool to view each page)
{images_list}

### Check Requirements
1. Use the Read tool to read each page PNG image and carefully check:
   - Is the text in figures clearly readable (equivalent >= 8pt)?
   - Do figures overflow the column width boundaries?
   - Are there any overlapping labels? (Check the overlap report above for known issues)
   - Do tables overflow their boundaries?
   - Does the overall visual quality meet academic publication standards?
2. If issues are found:
   - Locate the corresponding Python plotting script or LaTeX table code
   - Modify figsize to column width {geo['columnwidth_in']}in or full width {geo['textwidth_in']}in
   - Modify font.size to {geo['font_size_pt']}pt
   - For overlapping x-labels: use `rotation=45, ha='right'` or switch to horizontal bars
   - For crowded plots: increase figsize height or use `constrained_layout=True`
   - Read {self.figures_dir}/figure_config.json for full configuration
   - Re-run the script to regenerate figures
3. If no issues or already fixed, output the verdict

### Output Format (last line must be one of the following)
FIGURES_OK
FIGURES_NEED_FIX"""

            self.log_step(f"Figure quality check (loop {loop + 1})...", "progress")
            result = self.run_agent("visualizer", fixer_prompt, timeout=1200)

            if "FIGURES_OK" in (result or ""):
                self.log_step("Figure quality check passed", "success")
                break
            elif "FIGURES_NEED_FIX" in (result or ""):
                self.log_step("Figure fixer made changes, will re-check...", "progress")
                if full_script.exists():
                    # Back up AI-generated concept figures before re-running script
                    backups = {}
                    for fname in ai_generated_files:
                        fpath = self.figures_dir / fname
                        if fpath.exists():
                            backups[fname] = fpath.read_bytes()
                    self.generate_figures()
                    # Restore any AI-generated files that were overwritten
                    for fname, data in backups.items():
                        fpath = self.figures_dir / fname
                        if not fpath.exists() or fpath.read_bytes() != data:
                            fpath.write_bytes(data)
                            self.log(f"Restored AI-generated figure: {fname}", "INFO")
            else:
                self.log_step("Figure fixer verdict unclear, continuing...", "warning")
                break

        # Final compile after figure phase
        self.compile_latex()

    def _should_skip_figure_phase(self) -> bool:
        """Check if figure phase can be skipped (smart skipping).

        Skip if no figure-related files changed since last commit AND
        the current score is above threshold - 1.
        """
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1"],
                capture_output=True, text=True, timeout=30,
                cwd=self.code_dir,
            )
            if result.returncode != 0:
                return False  # Can't determine, don't skip

            changed_files = result.stdout.strip().split("\n") if result.stdout.strip() else []
            scripts_dir = self.config.get("scripts_dir", "scripts")
            figures_dir_name = self.config.get("figures_dir", "Latex/figures")

            figure_related = any(
                f.endswith(".py") and (scripts_dir in f or "figure" in f.lower())
                or figures_dir_name in f
                for f in changed_files
            )

            if figure_related:
                return False  # Figure files changed, must run

            # Check score threshold
            current_score = self.memory.scores[-1] if self.memory.scores else 0
            threshold = self.paper_accept_threshold - 1
            if current_score >= threshold:
                self.log_step("Skipping figure phase (no figure changes, score above threshold-1)", "info")
                return True

            return False
        except Exception:
            return False  # On error, don't skip

    def pdf_to_images(self) -> list:
        """Convert PDF to page images for visual review using PyMuPDF."""
        self.log("Converting PDF to images for visual review...", "INFO")
        pdf_path = self.latex_dir / "main.pdf"
        if not pdf_path.exists():
            self.log("PDF not found, skipping image conversion", "WARN")
            return []

        # Direct import (same approach as _count_pdf_pages / _count_body_pages)
        try:
            import fitz
            doc = fitz.open(str(pdf_path))
            image_paths = []
            for i, page in enumerate(doc):
                img_path = self.latex_dir / f"page_{i+1:02d}.png"
                pix = page.get_pixmap(dpi=150)
                pix.save(str(img_path))
                image_paths.append(str(img_path))
            doc.close()
            self.log(f"Generated {len(image_paths)} page images", "INFO")
            return image_paths
        except ImportError:
            self.log("PyMuPDF not available, trying subprocess fallback", "WARN")
        except Exception as e:
            self.log(f"Direct pdf_to_images failed: {e}, trying fallback", "WARN")

        # Fallback: subprocess via conda env
        return self._pdf_to_images_fallback()

    def _generate_nano_banana_figures(self):
        """Generate AI concept figures using PaperBanana pipeline or Nano Banana fallback.

        Flow:
        1. Run agent to analyze paper and identify concept figures
        2. Agent outputs JSON list of figures with prompts
        3. For each figure: try PaperBanana (Retriever→Planner→Stylist→Visualizer→Critic),
           fall back to Nano Banana pipeline if PaperBanana unavailable
        4. Generated PNGs saved to figures_dir
        """
        from ark.nano_banana import get_api_key
        api_key = get_api_key()
        if not api_key:
            self.log("No Gemini API key found, skipping AI figure generation", "WARN")
            return

        venue = self.config.get("venue", "")
        latex_dir = self.config.get("latex_dir", "Latex")

        # Ask agent to identify concept figures that need AI generation
        analysis_output = self.run_agent("visualizer", f"""
Analyze the paper {latex_dir}/main.tex and identify ALL figures that would benefit from
AI-generated concept diagrams (architecture diagrams, mechanism illustrations, overview figures,
workflow diagrams, system diagrams, pipeline diagrams).

Do NOT include data plots (bar charts, line charts, scatter plots) — those should remain as matplotlib.

For each concept figure, output a JSON block with this exact format:

```json
[
  {{
    "name": "fig_overview",
    "caption": "System architecture overview",
    "section_context": "Detailed description of what the figure should show, including all components, connections, data flows, and key metrics mentioned in the paper. Be as detailed as possible — the more context, the better the generated figure.",
    "latex_label": "fig:overview",
    "placement": "full_width"
  }}
]
```

For each figure, you MUST decide the "placement" field:
- "full_width": for complex figures — multi-stage pipelines, system architectures with many components, diagrams that need horizontal space. Uses `\\begin{{figure*}}` spanning all columns.
- "single_column": for simpler figures — single concept with few components, small diagrams. Uses `\\begin{{figure}}` in one column.

Decision criteria: if the figure has 4+ components, multiple stages, or branching paths → "full_width". If it's a simple 2-3 component relationship → "single_column".

Only include figures that:
1. Are referenced in LaTeX but have no existing file, OR
2. Are concept/architecture/mechanism diagrams that could be improved with AI generation

If no concept figures are needed, output: NO_CONCEPT_FIGURES
""", timeout=600)

        if not analysis_output or "NO_CONCEPT_FIGURES" in analysis_output:
            self.log_step("No concept figures needed", "info")
            return

        # Parse figure list from agent output
        figures = []
        try:
            json_match = re.search(r'\[[\s\S]*?\]', analysis_output)
            if json_match:
                figures = json.loads(json_match.group())
        except (json.JSONDecodeError, AttributeError):
            self.log("Failed to parse concept figure list from agent output", "WARN")
            return

        if not figures:
            return

        # Read paper context for prompt generation
        paper_text = ""
        main_tex = self.latex_dir / "main.tex"
        if main_tex.exists():
            try:
                paper_text = main_tex.read_text()[:5000]
            except Exception:
                pass

        # Get geometry for sizing
        from ark.latex_geometry import get_geometry
        venue_format = self.config.get("venue_format", venue)
        geo = get_geometry(venue_format) if venue_format else {"columnwidth_in": 3.333}

        columns = geo.get("columns", 1)
        col_w = geo.get("columnwidth_in", 3.333)
        text_w = geo.get("textwidth_in", 7.0)

        generated = 0
        for fig in figures:
            name = fig.get("name", "concept_fig")
            caption = fig.get("caption", "")
            section_ctx = fig.get("section_context", "")
            placement = fig.get("placement", "full_width")
            output_path = self.figures_dir / f"{name}.png"

            # Skip if file already exists and is non-empty
            if output_path.exists() and output_path.stat().st_size > 0:
                self.log(f"  Skipping {name}: already exists", "INFO")
                continue

            # Determine aspect ratio and width based on agent's placement decision
            if columns == 1:
                # Single-column templates (NeurIPS): always use textwidth
                fig_width = text_w
                aspect_ratio = "16:10"
            elif placement == "full_width":
                fig_width = text_w
                aspect_ratio = "21:9"  # wide for spanning both columns
            else:
                fig_width = col_w
                aspect_ratio = "4:3"  # compact for single column

            self.log(f"  Generating: {name} (placement={placement}, {fig_width:.1f}in, ratio={aspect_ratio})...", "INFO")

            # Try PaperBanana pipeline first (best quality)
            ok = self._try_paperbanana(
                name=name,
                caption=caption,
                paper_context=section_ctx or paper_text[:3000],
                output_path=output_path,
                api_key=api_key,
                aspect_ratio=aspect_ratio,
            )

            # Fallback to our Nano Banana pipeline
            if not ok:
                self.log(f"  PaperBanana unavailable, falling back to Nano Banana...", "INFO")
                from ark.nano_banana import generate_figure_pipeline
                ok = generate_figure_pipeline(
                    figure_name=name,
                    caption=caption,
                    paper_context=section_ctx or paper_text[:2000],
                    output_path=output_path,
                    api_key=api_key,
                    model=self.config.get("nano_banana_model", "pro"),
                    venue=venue,
                    column_width_in=fig_width,
                    max_critic_rounds=self.config.get("nano_banana_critic_rounds", 3),
                    log_fn=self.log,
                )
            if ok:
                generated += 1
                self.log(f"  Generated: {output_path.name}", "INFO")
            else:
                self.log(f"  Failed: {name}", "WARN")

        if generated > 0:
            self.log_step(f"Generated {generated} AI concept figures", "success")
        else:
            self.log_step("No new concept figures generated", "info")

    def _try_paperbanana(self, name: str, caption: str, paper_context: str,
                          output_path, api_key: str, aspect_ratio: str = "16:9") -> bool:
        """Try to generate a concept figure using PaperBanana's full pipeline.

        PaperBanana uses: Retriever → Planner → Stylist → Visualizer → Critic (×3 rounds)
        with reference images from PaperBananaBench for few-shot learning.

        Returns True if figure was generated successfully, False if PaperBanana unavailable.
        """
        import asyncio

        try:
            import sys
            pb_dir = Path(__file__).parent.parent / "submodules" / "PaperBanana"
            if not pb_dir.exists():
                return False
            if str(pb_dir) not in sys.path:
                sys.path.insert(0, str(pb_dir))

            from agents.planner_agent import PlannerAgent
            from agents.visualizer_agent import VisualizerAgent
            from agents.stylist_agent import StylistAgent
            from agents.critic_agent import CriticAgent
            from agents.retriever_agent import RetrieverAgent
            from utils.config import ExpConfig
            from utils.paperviz_processor import PaperVizProcessor
        except ImportError as e:
            self.log(f"PaperBanana not available: {e}", "WARN")
            return False

        # Check if PaperBananaBench data exists (for reference retrieval)
        data_dir = pb_dir / "data" / "PaperBananaBench"
        retrieval = "auto" if (data_dir / "diagram" / "ref.json").exists() else "none"
        if retrieval == "auto":
            self.log(f"  Using PaperBanana with reference retrieval ({data_dir})", "INFO")
        else:
            self.log(f"  Using PaperBanana without reference retrieval", "INFO")

        # Configure — use env var or config for API key
        import os
        os.environ["GOOGLE_API_KEY"] = api_key

        try:
            exp_config = ExpConfig(
                dataset_name="PaperBananaBench",
                task_name="diagram",
                exp_mode="demo_full",
                retrieval_setting=retrieval,
                max_critic_rounds=3,
                work_dir=pb_dir,
            )

            processor = PaperVizProcessor(
                exp_config=exp_config,
                vanilla_agent=None,
                planner_agent=PlannerAgent(exp_config=exp_config),
                visualizer_agent=VisualizerAgent(exp_config=exp_config),
                stylist_agent=StylistAgent(exp_config=exp_config),
                critic_agent=CriticAgent(exp_config=exp_config),
                retriever_agent=RetrieverAgent(exp_config=exp_config),
                polish_agent=None,
            )

            data = {
                "candidate_id": name,
                "content": paper_context,
                "visual_intent": f"{caption} STYLE: Labels MAX 3-5 words, NO sentences inside components. BUT make icons detailed and elaborate (not simple flat shapes). Layout should be COMPACT — minimize whitespace, pack components closely. The figure should feel dense and information-rich through its visual elements, not through text.",
                "additional_info": {"rounded_ratio": aspect_ratio},
            }

            # Run async pipeline
            async def _run():
                return await processor.process_single_query(data, do_eval=False)

            try:
                result = asyncio.run(_run())
            except RuntimeError:
                # Already in an event loop (e.g., webapp context)
                loop = asyncio.get_event_loop()
                result = loop.run_until_complete(_run())

            # Extract best image
            import base64
            eval_field = result.get("eval_image_field", "")
            for key in sorted(result.keys(), reverse=True):
                if "base64_jpg" in key and result[key] and len(result[key]) > 100:
                    if key == eval_field or True:  # Use the last valid image
                        img_data = base64.b64decode(result[key])
                        output_path = Path(output_path)
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(img_data)
                        self.log(f"  PaperBanana generated: {output_path.name} ({len(img_data)} bytes)", "INFO")
                        return True

            self.log("  PaperBanana ran but produced no valid image", "WARN")
            return False

        except Exception as e:
            self.log(f"  PaperBanana error: {e}", "WARN")
            return False

    def _pdf_to_images_fallback(self) -> list:
        """Fallback: use external pdf_to_images.py script."""
        env_name = self.config.get("conda_env", "base")
        script_path = f"{self.config.get('scripts_dir', 'scripts')}/pdf_to_images.py"
        try:
            result = subprocess.run(
                ["bash", "-c", f"source ~/.bashrc && mamba activate {env_name} && python {script_path} {self.latex_dir}/main.pdf --dpi 150"],
                capture_output=True, text=True, timeout=120, cwd=self.code_dir,
            )
            if result.returncode == 0:
                images = list(self.latex_dir.glob("page_*.png"))
                return [str(img) for img in sorted(images)]
            else:
                self.log(f"PDF to images fallback warning: {result.stderr[:200]}", "WARN")
                return []
        except Exception as e:
            self.log(f"PDF to images fallback error: {e}", "ERROR")
            return []
