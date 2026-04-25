"""CompilerMixin: LaTeX compilation, figure generation, PDF-to-image conversion."""

import json
import re
import subprocess
import yaml
from pathlib import Path
from ark.latex import utils as latex_utils


GEMINI_IMAGE_ASPECT_RATIOS = frozenset({
    "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1",
    "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9",
})


class CompilerMixin:
    """Mixin providing LaTeX compilation and figure management.

    Expects self to have: latex_dir, figures_dir, code_dir, config, hooks,
    log, log_step, run_agent, state_dir.
    """

    def compile_latex_with_errors(self) -> tuple:
        """Compile LaTeX and return (success, error_string).

        On success with no errors: returns (True, "").
        On success with errors (PDF exists but has issues): returns (True, error_string).
        On failure: returns (False, structured_errors).
        """
        success = self.compile_latex()
        compile_errors = getattr(self, '_last_compile_errors', [])

        if success and not compile_errors:
            return True, ""

        if success and compile_errors:
            # PDF exists but has issues (undefined refs, bibtex errors, etc.)
            error_str = "PDF was generated but has the following issues:\n\n"
            error_str += "\n".join(f"- {e[:150]}" for e in compile_errors[:10])
            return True, error_str

        # PDF not generated — real failure
        log_path = self.latex_dir / "main.log"
        errors = self._extract_latex_errors(log_path)

        stderr = getattr(self, '_last_compile_stderr', '')
        if "no specific errors found" in errors and stderr:
            errors += f"\n\nStderr from last pdflatex run:\n{stderr[:1000]}"

        if compile_errors:
            errors += "\n\nAdditional issues:\n" + "\n".join(f"- {e[:150]}" for e in compile_errors[:10])

        return False, errors

    def _extract_latex_errors(self, log_path: Path) -> str:
        """Parse LaTeX log for structured error messages."""
        return latex_utils.extract_latex_errors(log_path)

    def _compile_until_success(self, context: str = "") -> bool:
        """Keep fixing and recompiling until LaTeX compiles successfully.

        Strategy escalation:
        1. Attempts 1-3: Writer fixes errors normally
        2. Attempt 4: Programmatic fix (strip non-UTF8 bytes, fix common issues)
        3. Attempts 5-7: Writer with aggressive "comment out broken parts" prompt
        4. Attempt 8+: Writer rewrites broken sections from scratch

        Returns True when compiled, False only if 10 attempts all fail
        (should be extremely rare — our own agents generated this).
        """
        MAX_ATTEMPTS = 10
        last_errors = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            success, errors = self.compile_latex_with_errors()
            if success:
                if attempt > 1:
                    self.log_step(f"Compilation fixed on attempt {attempt}", "success")
                else:
                    self.log_step("Initial draft compiled successfully", "success")
                return True

            self.log_step(f"Compile attempt {attempt} failed", "warning")

            # Strategy 1 (attempts 1-3): normal writer fix
            if attempt <= 3:
                self.run_agent("writer",
                    f"LaTeX compilation failed. Read paper/main.tex carefully, find and fix "
                    f"the syntax errors below. Do NOT remove content — fix the LaTeX syntax.\n\n{errors}")

            # Strategy 2 (attempt 4): programmatic fix for common issues
            elif attempt == 4:
                self.log_step("Trying programmatic fixes...", "progress")
                self._auto_fix_latex()
                # Also let writer have another look after programmatic fix
                success, errors = self.compile_latex_with_errors()
                if success:
                    self.log_step("Programmatic fix worked", "success")
                    return True
                self.run_agent("writer",
                    f"After programmatic cleanup, LaTeX still fails. Fix these remaining errors:\n\n{errors}")

            # Strategy 3 (attempts 5-7): aggressive — comment out broken parts
            elif attempt <= 7:
                same_error = errors[:200] == last_errors[:200]
                self.run_agent("writer",
                    f"LaTeX has failed {attempt} times{' with the same error' if same_error else ''}. "
                    f"Take aggressive action: COMMENT OUT the broken section entirely and replace "
                    f"with a brief placeholder like '% TODO: fix this section'. "
                    f"The paper MUST compile.\n\n{errors}")

            # Strategy 4 (attempt 8+): nuclear — rewrite from scratch
            else:
                self.run_agent("writer",
                    f"LaTeX has failed {attempt} times. Read the ENTIRE main.tex file, identify ALL "
                    f"syntax errors, and rewrite any broken sections from scratch. Remove any "
                    f"non-standard packages or commands that might cause issues. "
                    f"Priority: the paper MUST compile, even if some content is lost.\n\n{errors}")

            last_errors = errors

        self.log_step(f"Compilation failed after {MAX_ATTEMPTS} attempts", "error")
        return False

    def _auto_fix_latex(self):
        """Programmatic fixes for common LaTeX compilation issues."""
        latex_utils.auto_fix_latex(self.latex_dir, log_fn=self.log)

    def compile_latex(self) -> bool:
        """Compile the LaTeX paper.

        Uses nonstopmode to avoid interactive prompts, but parses log/blg
        files for actual errors (undefined citations, bibtex syntax errors, etc.).

        On success, stores the PDF path in self._latest_pdf.
        Returns True/False for backward compat.
        """
        self.log("Compiling LaTeX...")
        self._last_compile_stderr = ""
        self._last_compile_errors = []
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
                    timeout=120,
                    cwd=self.latex_dir,
                )
                stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
                stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
                if result.returncode != 0 and "main.tex" in cmd:
                    self._last_compile_stderr = stderr[:1000] or stdout[-1000:]

            # Parse log for real errors (not just warnings)
            log_path = self.latex_dir / "main.log"
            blg_path = self.latex_dir / "main.blg"

            if log_path.exists():
                log_text = log_path.read_text(errors="replace")
                # LaTeX errors start with "!"
                latex_errors = [l.strip() for l in log_text.split("\n") if l.startswith("!")]
                if latex_errors:
                    self._last_compile_errors.extend(latex_errors[:10])

            if blg_path.exists():
                blg_text = blg_path.read_text(errors="replace")
                # BibTeX errors
                import re as _re
                bib_errors = _re.findall(r"^.*error message.*$|^I was expecting.*$|^Warning--I didn't find a database entry.*$",
                                         blg_text, _re.MULTILINE)
                if bib_errors:
                    self._last_compile_errors.extend(bib_errors[:10])

            pdf_path = self.latex_dir / "main.pdf"
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                self._latest_pdf = pdf_path
                self._pdf_page_count = self._count_pdf_pages(pdf_path)
                self._body_page_count = self._count_body_pages(pdf_path)
                self._overfull_warnings = self._parse_overfull_warnings()
                page_info = f", {self._body_page_count:.1f} body pages ({self._pdf_page_count} total)" if self._body_page_count else ""
                overfull_info = f", {len(self._overfull_warnings)} overfull warnings" if self._overfull_warnings else ""
                error_info = f", {len(self._last_compile_errors)} errors" if self._last_compile_errors else ""
                self.log(f"LaTeX compiled successfully: {pdf_path} ({pdf_path.stat().st_size} bytes{page_info}{overfull_info}{error_info})")
                if self._last_compile_errors:
                    for err in self._last_compile_errors[:5]:
                        self.log(f"  Compile issue: {err[:120]}", "WARN")
                return True
            else:
                self.log("LaTeX compilation failed: PDF not generated")
                if self._last_compile_errors:
                    for err in self._last_compile_errors[:5]:
                        self.log(f"  Error: {err[:120]}", "ERROR")
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
        """Count body pages (before References section).

        Primary method: read \\arkBodyEndY / \\arkPageH from the .aux file
        (injected by _ensure_clearpage_before_bibliography via \\pdfsavepos).
        This gives the exact y-position where body text ends, immune to
        page-number / header / footer interference.

        Fallback: PyMuPDF text-block analysis (less accurate).

        Returns a float: e.g. 5.8 means 5 full pages + last page 80% filled.
        """
        # ── Try aux-based measurement first ──
        result = self._count_body_pages_from_aux(pdf_path)
        if result is not None:
            return result

        # ── Fallback: PyMuPDF text-block analysis ──
        return self._count_body_pages_from_pdf(pdf_path)

    def _count_body_pages_from_aux(self, pdf_path: Path) -> float | None:
        """Read body-end position from .aux file (written by \\pdfsavepos).

        Returns body page count as a float, or None if aux data unavailable.
        """
        aux_path = self.latex_dir / "main.aux"
        if not aux_path.exists():
            return None
        try:
            aux_text = aux_path.read_text(errors="replace")
            import re
            m_y = re.search(r'\\gdef\\arkBodyEndY\{(\d+)\}', aux_text)
            m_h = re.search(r'\\gdef\\arkPageH\{(\d+)\}', aux_text)
            if not m_y or not m_h:
                return None

            body_end_y_sp = int(m_y.group(1))   # sp from page bottom
            page_height_sp = int(m_h.group(1))   # total page height in sp
            if page_height_sp <= 0:
                return None

            # fill_ratio = fraction of page used (from top)
            fill_ratio = 1.0 - (body_end_y_sp / page_height_sp)

            # Determine which page the body ends on by finding References page
            import fitz
            doc = fitz.open(str(pdf_path))
            ref_page_idx = None
            for i in range(len(doc)):
                text = doc[i].get_text()
                if any(line.strip() == 'References' for line in text.split('\n')):
                    ref_page_idx = i
                    break
            doc.close()

            if ref_page_idx is None:
                return None  # can't determine without References marker

            # Body ends on the page before References (since we inject
            # the marker right before \clearpage\bibliography)
            last_body_idx = max(ref_page_idx - 1, 0)
            result = last_body_idx + fill_ratio
            self.log(f"Body page count (aux): {result:.2f} "
                     f"(page {last_body_idx+1}, {fill_ratio:.1%} filled)", "DEBUG")
            return result
        except Exception as e:
            self.log(f"Aux-based page count failed: {e}", "DEBUG")
            return None

    def _count_body_pages_from_pdf(self, pdf_path: Path) -> float:
        """Fallback: count body pages via PyMuPDF text-block analysis."""
        try:
            import fitz
            doc = fitz.open(str(pdf_path))

            # Find which page has "References"
            ref_page_idx = None
            for i in range(len(doc)):
                text = doc[i].get_text()
                if any(line.strip() == 'References' for line in text.split('\n')):
                    ref_page_idx = i
                    break

            if ref_page_idx is None:
                # No References found — all pages are body
                total = len(doc)
                doc.close()
                return float(total)

            # The last body page is the page BEFORE References
            # (if References has its own page via \clearpage)
            # OR the same page (if References starts mid-page)
            ref_page = doc[ref_page_idx]

            # Check if References is at the very top of its page (i.e., \clearpage was used)
            ref_y = 0
            for block in ref_page.get_text("dict")["blocks"]:
                for line_obj in block.get("lines", []):
                    line_text = "".join(s["text"] for s in line_obj.get("spans", []))
                    if line_text.strip() == "References":
                        ref_y = line_obj["bbox"][1]
                        break
                if ref_y > 0:
                    break

            page_height = ref_page.rect.height
            ref_at_top = ref_y < page_height * 0.15  # References in top 15% = separate page

            if ref_at_top and ref_page_idx > 0:
                # References on its own page — last body page is previous page
                last_body_idx = ref_page_idx - 1
            else:
                # References starts mid-page — body ends partway through this page
                last_body_idx = ref_page_idx

            last_body_page = doc[last_body_idx]
            page_width = last_body_page.rect.width
            page_height = last_body_page.rect.height

            # Detect dual-column by checking if text exists in both halves
            # Filter out headers (top 6%) and footers (bottom 4%) — page numbers, running titles
            blocks = last_body_page.get_text("blocks")
            content_blocks = [
                b for b in blocks
                if b[3] > page_height * 0.06
                and b[1] < page_height * 0.96
            ]
            mid_x = page_width / 2
            left_blocks = [b for b in content_blocks if b[0] < mid_x]
            right_blocks = [b for b in content_blocks if b[0] >= mid_x]

            is_dual_column = len(left_blocks) > 0 and len(right_blocks) > 0

            if is_dual_column:
                # Dual column: fill ratio = right column's last text y / page height
                right_last_y = max(b[3] for b in right_blocks) if right_blocks else 0
                fill_ratio = right_last_y / page_height
            elif ref_at_top:
                # Single column, References on separate page: check last body page fill
                body_blocks = [
                    b for b in blocks
                    if b[3] > page_height * 0.06       # below header
                    and b[1] < page_height * 0.96       # above footer
                ]
                if body_blocks:
                    last_y = max(b[3] for b in body_blocks)
                    fill_ratio = last_y / page_height
                else:
                    fill_ratio = 0.0
            else:
                # Single column, References mid-page: body ends at References y
                fill_ratio = ref_y / page_height

            result = last_body_idx + fill_ratio
            doc.close()
            return result
        except Exception as e:
            self.log(f"Body page count failed: {e}", "WARN")
            return 0.0

    def _parse_overfull_warnings(self) -> list[str]:
        """Parse main.log for Overfull hbox warnings."""
        return latex_utils.parse_overfull_warnings(self.latex_dir)

    def _generate_figures_from_results(self) -> bool:
        """Generate figures from latest experiment results and copy to paper dir."""
        if hasattr(self.hooks, 'generate_figures_from_results'):
            return self.hooks.generate_figures_from_results(self)
        else:
            self.log("No generate_figures_from_results hook defined.", "WARN")
            return False

    def generate_figures(self) -> bool:
        """Run figure generation script. If script doesn't exist, try to create it first."""
        if hasattr(self.hooks, 'generate_figures'):
            return self.hooks.generate_figures(self)

        self.log("Generating paper figures...", "INFO")
        script_path = self.config.get("create_figures_script", "scripts/create_paper_figures.py")
        full_script = self.code_dir / script_path

        # If script doesn't exist, try to create it from results
        if not full_script.exists():
            self._create_plotting_script_if_needed()

        if not full_script.exists():
            self.log(f"No figure script at {script_path}, skipping", "INFO")
            return True

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

        geo = get_geometry(venue, paper_dir=self.latex_dir)
        config_path = self.figures_dir / "figure_config.json"
        write_figure_config(geo, config_path)
        self.log(f"Figure config generated: {config_path} (column={geo['columnwidth_in']}in, font={geo['font_size_pt']}pt)", "INFO")
        return geo

    def _run_figure_phase(self):
        """Figure Phase: generate figures and ensure template-aware sizing.

        1. Generate figure_config.json (geometry)
        2. Load manifest, backup protected (AI-generated) figures
        3. Run matplotlib figure script (if exists) + overlap detection
        4. Restore any overwritten protected figures
        5. Generate AI concept figures (Nano Banana, if enabled)
        6. Compile LaTeX

        Figure *quality* issues are handled by the reviewer, not here.
        """
        from ark.figure_manifest import (
            load_manifest, save_manifest, register_figure,
            backup_protected, restore_protected,
        )

        # Step 1: Generate geometry config
        geo = self._generate_figure_config()

        # Step 2: Load manifest (auto-migrates if missing)
        manifest = load_manifest(self.figures_dir)

        # Step 3: Run figure generation script
        script_path = self.config.get("create_figures_script", "scripts/create_paper_figures.py")
        full_script = self.code_dir / script_path
        if full_script.exists():
            self.log_step("Running figure generation script...", "progress")

            # Backup protected files before running matplotlib script
            backups = backup_protected(self.figures_dir, manifest)

            self.generate_figures()

            # Restore any AI-generated files overwritten by the script
            restore_protected(self.figures_dir, backups, log_fn=self.log)

            # Register matplotlib outputs in manifest. Pass figures_dir so
            # register_figure can read the actual PDF width (in inches) from
            # disk — that width is what decides placement single vs full,
            # which is what the writer reads to pick figure vs figure*.
            for fig_file in self.figures_dir.glob("fig*"):
                if fig_file.suffix in (".pdf", ".png", ".jpg"):
                    if fig_file.name not in manifest.get("figures", {}):
                        register_figure(
                            manifest, fig_file.name, "matplotlib",
                            figures_dir=self.figures_dir,
                        )
                    elif manifest["figures"][fig_file.name].get("source") == "matplotlib":
                        pass  # Already registered
            save_manifest(self.figures_dir, manifest)

            # Step 3.1: Programmatic overlap detection and auto-fix
            try:
                from ark.figure_overlap import check_and_fix_figures
                overlap_report = check_and_fix_figures(
                    full_script, self.figures_dir, geo, log_fn=self.log,
                )
                if overlap_report.get("summary", {}).get("with_overlaps", 0) > 0:
                    self.log_step("Regenerating figures after overlap fixes...", "progress")
                    backups = backup_protected(self.figures_dir, manifest)
                    self.generate_figures()
                    restore_protected(self.figures_dir, backups, log_fn=self.log)
            except Exception as e:
                self.log(f"Overlap detection error (non-fatal): {e}", "WARN")
        else:
            self.log_step(f"No figure script at {script_path}, skipping generation", "info")

        # Step 4: Generate AI concept figures (Nano Banana)
        if self.config.get("figure_generation") == "nano_banana":
            self.log_step("Generating AI concept figures (Nano Banana)...", "progress")
            self._generate_nano_banana_figures()

        # Step 5: Compile LaTeX
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
            # self.log("PyMuPDF not available, trying subprocess fallback", "WARN")
            pass
        except Exception as e:
            self.log(f"Direct pdf_to_images failed: {e}", "WARN")

        return []

    def _maybe_generate_page_images(self) -> list:
        """Convert PDF to page images, skipping if images are already up-to-date."""
        pdf_path = self.latex_dir / "main.pdf"
        if not pdf_path.exists():
            return self.pdf_to_images()

        first_page = self.latex_dir / "page_01.png"
        if first_page.exists():
            try:
                if first_page.stat().st_mtime >= pdf_path.stat().st_mtime:
                    # Images are up-to-date
                    images = sorted(self.latex_dir.glob("page_*.png"))
                    if images:
                        self.log("Page images up-to-date, skipping regeneration", "INFO")
                        return [str(img) for img in images]
            except OSError:
                pass  # Fall through to regenerate

        return self.pdf_to_images()

    def _run_citation_verification(self):
        """Verify references.bib, fix errors, mark NEEDS-CHECK, clean unused."""
        from ark.citation import verify_bib, fix_bib, cleanup_unused

        bib_path = self.latex_dir / "references.bib"
        if not bib_path.exists():
            return

        lit_path = str(self.state_dir / "literature.yaml")
        bib_str = str(bib_path)
        tex_dir = str(self.latex_dir)

        self.log_step("Citation verification...", "progress")

        try:
            # 1. Verify each entry against DBLP/CrossRef
            results = verify_bib(bib_str)

            if results:
                needs_check = [r for r in results if r.status == "NEEDS-CHECK"]
                corrected = [r for r in results if r.status == "CORRECTED"]

                # 2. Apply fixes (add note field for NEEDS-CHECK, overwrite CORRECTED)
                if corrected or needs_check:
                    fix_bib(bib_str, results)

                # 3. Log summary
                summary = []
                verified = [r for r in results if r.status == "VERIFIED"]
                if verified:
                    summary.append(f"{len(verified)} verified")
                if corrected:
                    summary.append(f"{len(corrected)} fixed")
                if needs_check:
                    summary.append(f"{len(needs_check)} marked needs-check")

                if summary:
                    self.log(f"BibTeX audit: {', '.join(summary)}", "INFO")

                # 4. Notify about needs-check
                if needs_check:
                    self.send_notification(
                        "BibTeX Check Needed",
                        f"{len(needs_check)} citation(s) in references.bib need manual verification. "
                        "Marked with 'ark-note: NEEDS-CHECK' in the .bib file.",
                        priority="warning",
                    )

                # 4.5. Enforce critical citations
                self._enforce_critical_citations(lit_path, tex_dir)
            else:
                # Still try cleanup even if verify returns nothing or errors
                pass

            # 5. Clean up unused entries
            original_size = bib_path.stat().st_size
            cleanup_unused(bib_str, tex_dir)
            cleaned_size = bib_path.stat().st_size
            if cleaned_size < original_size:
                self.log(f"BibTeX cleanup: removed unused entries ({original_size} -> {cleaned_size} bytes)", "INFO")

        except Exception as e:
            self.log(f"Citation verification failed: {e}", "WARN")

    def _enforce_critical_citations(self, lit_path: str, tex_dir: str):
        """Check that all critical (MUST CITE) papers are cited in tex."""
        lit_file = Path(lit_path)
        if not lit_file.exists():
            return

        try:
            lit_data = yaml.safe_load(lit_file.read_text()) or {}
        except Exception:
            return

        # Collect all critical cite keys
        critical = []
        for ref in lit_data.get("references", []):
            if isinstance(ref, dict) and ref.get("importance") == "critical":
                critical.append((ref.get("bibtex_key", ""), ref.get("title", "")))
        for nc in lit_data.get("needs_check", []):
            if isinstance(nc, dict) and nc.get("importance") == "critical":
                critical.append((nc.get("bibtex_key", ""), nc.get("title", "")))

        if not critical:
            return

        # Collect all cited keys from tex
        import re
        cited_keys = set()
        tex_path = Path(tex_dir)
        for tex_file in tex_path.glob("**/*.tex"):
            content = tex_file.read_text(errors="replace")
            for m in re.finditer(r"\\cite[pt]?\{([^}]+)\}", content):
                for key in m.group(1).split(","):
                    cited_keys.add(key.strip())

        # Find missing critical citations
        missing = [(key, title) for key, title in critical if key and key not in cited_keys]

        if not missing:
            return

        self.log_step(f"{len(missing)} critical citation(s) missing from paper, asking writer to add", "warning")

        missing_list = "\n".join(f"- \\cite{{{key}}} — {title}" for key, title in missing)
        latex_dir_name = self.config.get("latex_dir", "Latex")

        self.run_agent("writer", f"""
The following critical citations are missing from the paper. They are marked as MUST CITE
in the research report but are not currently referenced anywhere in {latex_dir_name}/main.tex.

{missing_list}

Add each of these citations to the most appropriate location in the paper (typically Related Work).
Write a brief sentence or clause that naturally incorporates each \\cite{{}} command.
Do NOT remove any existing citations. Do NOT modify references.bib.
""", timeout=600)

    def _generate_nano_banana_figures(self) -> int:
        """Generate AI concept figures using PaperBanana pipeline or Nano Banana fallback.

        All concept figure data lives in figures_dir (paper/figures/):
        - concept_figures.json — spec of what figures to generate
        - fig_*.png — generated images
        - figure_manifest.json — tracks provenance (source: paperbanana/nano_banana)

        Flow:
        1. Check if spec + all PNGs already exist → skip planner
        2. Otherwise run planner agent to identify concept figures
        3. Read spec from: figures_dir (designated) → agent text → rglob (fallback)
        4. Normalize spec to figures_dir/concept_figures.json
        5. For each figure not yet in manifest: PaperBanana → Nano Banana fallback

        Returns:
            Number of concept figures available (existing + newly generated).
        """
        from ark.nano_banana import get_api_key
        from ark.figure_manifest import load_manifest, save_manifest, register_figure, AI_SOURCES

        spec_path = self.figures_dir / "concept_figures.json"
        manifest = load_manifest(self.figures_dir)

        # ── Phase 0: Check if all concept figures already exist ──
        if spec_path.exists():
            try:
                figures = json.loads(spec_path.read_text())
                if isinstance(figures, list) and figures:
                    all_done = all(
                        (self.figures_dir / f"{fig.get('name', '')}.png").exists()
                        and manifest.get("figures", {}).get(f"{fig.get('name', '')}.png", {}).get("source") in AI_SOURCES
                        for fig in figures if isinstance(fig, dict)
                    )
                    if all_done:
                        self.log(f"All {len(figures)} concept figures already exist, skipping", "INFO")
                        return len(figures)
            except (json.JSONDecodeError, OSError):
                pass  # Spec file corrupt, will re-generate

        # ── Phase 1: Get API key ──
        api_key = get_api_key()
        if not api_key:
            self.log("No Gemini API key found, skipping AI figure generation", "WARN")
            return 0

        venue = self.config.get("venue", "")

        # ── Phase 2: Gather context for planner ──
        title = self.config.get("title", "")
        findings_file = self.state_dir / "findings.yaml" if hasattr(self, 'state_dir') else None
        dr_file = self.state_dir / "deep_research.md" if hasattr(self, 'state_dir') else None
        main_tex = self.latex_dir / "main.tex"

        source_lines = [f"Paper title: {title}", f"Venue: {venue}", "",
                        "## Source Material (MANDATORY — Read in full before planning)"]
        source_lines.append("- `idea.md` — research idea")
        if dr_file and dr_file.exists():
            source_lines.append("- `auto_research/state/deep_research.md` — background research")
        if findings_file and findings_file.exists():
            source_lines.append("- `auto_research/state/findings.yaml` — experiment findings")
        if main_tex.exists():
            main_tex_rel = main_tex.relative_to(self.code_dir) if main_tex.is_relative_to(self.code_dir) else main_tex
            source_lines.append(f"- `{main_tex_rel}` — current paper draft (if already written)")
        source_lines.append("")
        source_lines.append("Use Read to load each file in full. Do NOT guess at the research content —")
        source_lines.append("concept figures only make sense when grounded in what the paper actually claims.")

        paper_context = "\n".join(source_lines)

        # ── Phase 3: Run planner agent ──
        figures_dir_rel = self.figures_dir.relative_to(self.code_dir) if self.figures_dir.is_relative_to(self.code_dir) else self.figures_dir
        analysis_output = self.run_agent("planner", f"""Based on the research described below, identify concept figures that should be
AI-generated for this paper. These are architecture diagrams, system overviews, pipeline illustrations,
mechanism diagrams — NOT data plots (bar charts, line charts etc. are handled separately).

## Research Context
{paper_context}

## Your Task
Identify 1-3 concept figures that would best illustrate this research. Every paper needs at least
one system overview/architecture figure.

IMPORTANT: Save the JSON array to the file: {figures_dir_rel}/concept_figures.json
Also output the same JSON in your response text.

The JSON format:
```json
[
  {{
    "name": "fig_overview",
    "caption": "System architecture overview showing the main components and data flow",
    "section_context": "Detailed description of what the figure should show — all components, connections, stages, data flows. Be specific about the research system's structure.",
    "latex_label": "fig:overview",
    "placement": "full_width"
  }}
]
```

Rules for "placement":
- "full_width": for complex multi-stage pipelines, architectures with 4+ components
- "single_column": for simple 2-3 component diagrams

We encourage at least 1 system overview figure if the research has a multi-component system.
Output up to 3 figures maximum. If the research truly has no visual architecture to illustrate,
output: NO_CONCEPT_FIGURES
""", timeout=600)

        if not analysis_output or "NO_CONCEPT_FIGURES" in analysis_output:
            self.log_step("Planner decided no concept figures needed", "info")
            return 0

        # ── Phase 4: Read concept figure spec (2-level fallback) ──
        figures = []

        # Level 1: Read from designated path (agent was told to save here)
        if spec_path.exists():
            try:
                parsed = json.loads(spec_path.read_text())
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    figures = parsed
                    self.log(f"Loaded concept figures from {spec_path}", "INFO")
            except (json.JSONDecodeError, OSError):
                pass

        # Level 2: Parse from agent text response
        if not figures and analysis_output:
            bracket_blocks = re.findall(r'\[[\s\S]*?\]', analysis_output)
            for block in sorted(bracket_blocks, key=len, reverse=True):
                if len(block) < 20:
                    continue
                try:
                    parsed = json.loads(block)
                    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                        figures = parsed
                        self.log("Parsed concept figures from planner text output", "INFO")
                        break
                except json.JSONDecodeError:
                    continue

        if not figures:
            self.log("No concept figures from designated path or planner text output", "WARN")
            return 0

        # ── Phase 4.5: Normalize — save spec to designated path ──
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(figures, indent=2, ensure_ascii=False) + "\n")

        # ── Phase 5: Generate images ──
        paper_text = paper_context

        from ark.latex_geometry import get_geometry
        venue_format = self.config.get("venue_format", venue)
        geo = get_geometry(venue_format, paper_dir=self.latex_dir) if venue_format else {"columnwidth_in": 3.333}

        columns = geo.get("columns", 1)
        col_w = geo.get("columnwidth_in", 3.333)
        text_w = geo.get("textwidth_in", 7.0)

        generated = 0
        skipped = 0
        for fig in figures:
            name = fig.get("name", "concept_fig")
            caption = fig.get("caption", "")
            section_ctx = fig.get("section_context", "")
            placement = fig.get("placement", "full_width")
            output_path = self.figures_dir / f"{name}.png"

            # Skip if already registered in manifest as AI-generated
            fig_info = manifest.get("figures", {}).get(f"{name}.png", {})
            if fig_info.get("source") in AI_SOURCES and output_path.exists():
                self.log(f"  Skipping {name}: already in manifest as {fig_info['source']}", "INFO")
                skipped += 1
                continue

            # Determine aspect ratio and width based on placement
            if columns == 1:
                fig_width = text_w
                aspect_ratio = "16:9"
            elif placement == "full_width":
                fig_width = text_w
                aspect_ratio = "21:9"
            else:
                fig_width = col_w
                aspect_ratio = "4:3"

            if aspect_ratio not in GEMINI_IMAGE_ASPECT_RATIOS:
                raise ValueError(
                    f"Illegal aspect_ratio {aspect_ratio!r} for Gemini image API; "
                    f"must be one of {sorted(GEMINI_IMAGE_ASPECT_RATIOS)}"
                )

            self.log(f"  Generating: {name} (placement={placement}, {fig_width:.1f}in, ratio={aspect_ratio})...", "INFO")

            # Try PaperBanana pipeline first (best quality)
            source = "paperbanana"
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
                source = "nano_banana"
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
                # Pass the authoritative placement (from concept_figures.json)
                # and actual rendered width through to the manifest, so the
                # writer and compression skill don't have to infer from
                # pixel count — AI PNG pixel dimensions are a model choice,
                # not a physical paper measurement.
                register_figure(
                    manifest, output_path.name, source,
                    figures_dir=self.figures_dir,
                    width_in=fig_width,
                    placement=placement,
                    # scalable defaults to True for AI sources (bitmap)
                )
                save_manifest(self.figures_dir, manifest)
            else:
                self.log(f"  Failed: {name}", "WARN")

        if generated > 0:
            self.log_step(f"Generated {generated} AI concept figures", "success")
        elif skipped > 0:
            self.log_step(f"All {skipped} concept figures already exist", "info")
        else:
            self.log_step("No new concept figures generated", "info")

        return generated + skipped

    def _try_paperbanana(self, name: str, caption: str, paper_context: str,
                          output_path, api_key: str, aspect_ratio: str = "16:9") -> bool:
        """Try to generate a concept figure using PaperBanana's full pipeline.

        PaperBanana uses: Retriever → Planner → Stylist → Visualizer → Critic (×3 rounds)
        with reference images from PaperBananaBench for few-shot learning.

        Returns True if figure was generated successfully, False if PaperBanana unavailable.
        """
        import asyncio

        # Set API key BEFORE importing PaperBanana — its generation_utils
        # module calls reinitialize_clients() at import time, which reads
        # GOOGLE_API_KEY from the environment.
        import os
        os.environ["GOOGLE_API_KEY"] = api_key

        try:
            import sys
            # compiler.py lives at ark/latex/compiler.py — three .parent
            # walks (compiler.py → latex/ → ark/ → repo root) reach the
            # submodules/ directory. The pre-refactor path used
            # ``parent.parent`` because compiler.py was at ark/compiler.py.
            # That two-step walk now stops at ark/ and silently returns
            # False here, sending every concept-figure call to the Nano
            # Banana fallback even when PaperBanana is fully installed.
            pb_dir = Path(__file__).parent.parent.parent / "submodules" / "PaperBanana"
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
