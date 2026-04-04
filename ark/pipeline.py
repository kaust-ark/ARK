"""PipelineMixin: main run loop, paper iteration, research iteration, dependency check."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import yaml
from datetime import datetime, timedelta
from pathlib import Path

from ark.ui import RateLimitCountdown


class PipelineMixin:
    """Mixin providing the top-level pipeline orchestration.

    Expects self to have: iteration, max_iterations, max_end_time, mode, model,
    project_name, code_dir, config, log, log_section, log_phase, log_step,
    log_summary_box, run_agent, memory, paper_accept_threshold,
    compile_latex, pdf_to_images, _run_figure_phase, _should_skip_figure_phase,
    generate_figures, run_planning_phase, _run_execute_phase, run_planner_cycle, self_repair,
    check_and_trigger_meta_debug,
    parse_review_score, extract_issue_ids, record_score_to_memory,
    cleanup_workspace, git_commit, save_checkpoint, send_notification,
    load_paper_state, save_paper_state, load_paper_requirements,
    _should_run_paper_initialize, _check_needs_experiment,
    _check_needs_literature_search, load_state, save_state, get_current_phase,
    check_user_updates, _last_score, hooks, _agent_stats, _write_cost_report.
    """

    def run_paper_iteration(self) -> bool:
        """Execute one paper review iteration. Returns whether to continue."""
        self.iteration += 1
        self._iteration_start = datetime.now()
        self._quota_exhausted = False  # Reset at start of each iteration
        self._asked_this_iteration = False  # Reset smart intervention flag

        # Load persistent user instructions (always active, never consumed)
        persistent_instructions = self.load_user_instructions()
        if persistent_instructions:
            base_anchor = self.config.get("goal_anchor", "")
            self.memory.set_goal_anchor(
                (base_anchor + "\n\n" if base_anchor else "")
                + f"## User Instructions (MUST follow throughout all iterations)\n\n{persistent_instructions}"
            )

        # Check user updates — if present, invalidate step cache (restart from step 1)
        user_updates = self.check_user_updates()
        if user_updates:
            self.log(f"Applying user updates to memory context...", "INFO")
            if hasattr(self.memory, 'goal_anchor') and self.memory.goal_anchor:
                self.memory.goal_anchor += f"\n\n## User Updates\n\n{user_updates}"
            else:
                self.memory.set_goal_anchor(f"## User Updates\n\n{user_updates}")

        paper_state = self.load_paper_state()
        paper_requirements = self.load_paper_requirements()
        current_score = paper_state.get("current_score", 0)

        # Step-level resume: skip already-completed steps
        resume_step = self.get_resume_step() if not user_updates else 0
        if resume_step > 0:
            self.log(f"Resuming from step {resume_step + 1} (steps 1-{resume_step} completed)", "INFO")

        # Iteration header
        self.log("", "RAW")
        self.log_section(f"Review Phase: Iteration {self.iteration}/{self.max_iterations}  |  Score: {current_score}/10 → ?  |  Target: {self.paper_accept_threshold}/10")

        total_steps = 5
        step_num = 0

        # Initialize variables that may be set by skipped steps
        review_output = ""
        score = current_score
        issue_ids = []
        post_accept_cleanup = False
        stop_after_cleanup = False
        planner_success = True

        # 1. Compile current LaTeX (with robust retry)
        MAX_COMPILE_RETRIES = 5
        step_num += 1
        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Compile LaTeX", "skipped")
        else:
            self.log_step_header(step_num, total_steps, "Compile LaTeX")
            compiled = False
            for attempt in range(1, MAX_COMPILE_RETRIES + 1):
                self.log_step(f"Compiling LaTeX (attempt {attempt}/{MAX_COMPILE_RETRIES})...", "progress")
                success, errors = self.compile_latex_with_errors()
                if success:
                    compiled = True
                    break

                self.log_step(f"Attempt {attempt} failed, sending errors to writer...", "warning")

                # Escalating prompts
                if attempt <= 1:
                    fix_prompt = (
                        f"LaTeX compilation failed. Fix the syntax errors below and ensure it compiles.\n\n"
                        f"{errors}"
                    )
                elif attempt == 2:
                    fix_prompt = (
                        f"LaTeX still fails to compile. Check for mismatched braces, undefined commands, "
                        f"and missing packages. Here are the errors:\n\n{errors}"
                    )
                else:
                    fix_prompt = (
                        f"LaTeX compilation has failed {attempt} times. Take a conservative approach: "
                        f"comment out the problematic section and replace with a minimal working version. "
                        f"The paper must compile.\n\nErrors:\n{errors}"
                    )

                self.run_agent("writer", fix_prompt)

            if not compiled:
                self.log_step(f"LaTeX failed after {MAX_COMPILE_RETRIES} attempts", "error")
                idx, reply = self.ask_user_decision(
                    f"LaTeX compilation failed after {MAX_COMPILE_RETRIES} writer attempts.\n\n"
                    f"Latest errors:\n{errors[:500]}",
                    options=[
                        "Skip this iteration",
                        f"Retry with {MAX_COMPILE_RETRIES} more writer attempts",
                        "I'll fix manually, then continue",
                    ],
                    timeout=900, default=0,
                )
                if idx == 1:
                    # Retry loop (one extra round)
                    for retry in range(1, MAX_COMPILE_RETRIES + 1):
                        self.log_step(f"Extra retry {retry}/{MAX_COMPILE_RETRIES}...", "progress")
                        success, errors = self.compile_latex_with_errors()
                        if success:
                            compiled = True
                            break
                        self.run_agent("writer", f"LaTeX still broken. Comment out broken parts.\n\n{errors}")
                elif idx == 2:
                    # User fixes manually — wait, then try once
                    self.log_step("Waiting for manual fix...", "progress")
                    success, errors = self.compile_latex_with_errors()
                    compiled = success

                if not compiled:
                    self.log_step("Cannot compile, skipping iteration", "error")
                    self.log_step_header(step_num, total_steps, "Compile LaTeX", "end")
                    return True

            self.log_step("PDF generated successfully", "success")
            self.log_step_header(step_num, total_steps, "Compile LaTeX", "end")
            self.save_step_checkpoint(step_num, "Compile LaTeX")

        # Citation Verification & Cleanup (runs every iteration)
        self._run_citation_verification()

        # Convert PDF to images for visual review
        page_images = self._maybe_generate_page_images()
        visual_review_section = ""
        if page_images:
            visual_review_section = f"""

## Visual Review

Please use the Read tool to read the following paper page images for visual review:
{chr(10).join(f'- {img}' for img in page_images)}

Key checks:
- Are figure sizes appropriate and fonts clearly readable?
- Is the layout professional (alignment, spacing, margins)?
- Is the information density appropriate?
- Does the overall visual quality meet research publication standards?
"""

        # 2. Reviewer Agent
        step_num += 1
        if step_num <= resume_step:
            # Reload review state from disk
            self.log_step_header(step_num, total_steps, "Review Paper", "skipped")
            review_file = self.state_dir / "latest_review.md"
            if review_file.exists():
                review_output = review_file.read_text()
            score = paper_state.get("current_score", 0)
            issue_ids = self.extract_issue_ids()
        else:
            self.log_step_header(step_num, total_steps, "Review Paper")

            try:
                venue_name = self.config.get('venue', 'top venue')
                review_output = self.run_agent(
                    "reviewer",
                    f"""Please review the current paper {self.config.get('latex_dir', 'Latex')}/main.tex and the generated {self.config.get('latex_dir', 'Latex')}/main.pdf.

Review according to {venue_name} standards:
- Technical Quality (40%)
- Paper Presentation (30%)
- Novelty (20%)
- Writing Quality (10%)
{visual_review_section}
Output a detailed review report including:
1. Overall Score (X/10)
2. Per-dimension scores
3. Major Issues (must fix)
4. Minor Issues (suggested fixes)
5. Specific improvement suggestions

Save the review report to auto_research/state/latest_review.md""",
                    timeout=2400,
                )
            except Exception as e:
                self.log(f"Review phase failed: {e}", "ERROR")
                self.log_step_header(step_num, total_steps, "Review Paper", "end")
                self.save_step_checkpoint(step_num - 1, "Compile LaTeX")  # Don't mark review as done
                return True

            # Parse score
            score = self.parse_review_score(review_output)

            # If score is 0 and review output exists, retry once
            if score == 0.0 and review_output and len(review_output.strip()) > 100:
                self.log("Score parsed as 0 but review exists, retrying reviewer for explicit score...", "WARN")
                retry_output = self.run_agent("reviewer", f"""
The previous review did not output an explicit score. Please read auto_research/state/latest_review.md,
provide an explicit overall score (format: Overall Score: X/10), and update the file.
""", timeout=600)
                retry_score = self.parse_review_score(retry_output)
                if retry_score > 0:
                    score = retry_score

            score_delta = score - current_score
            delta_str = f"+{score_delta:.1f}" if score_delta >= 0 else f"{score_delta:.1f}"
            self.log_step(f"Score: {score}/10 ({delta_str} from last)", "success" if score_delta >= 0 else "warning")

            # Record issues for repeat tracking
            issue_ids = self.extract_issue_ids()
            self.memory.record_issues(issue_ids, self.iteration)

            # Check for repeating issues
            repeat_issues = self.memory.get_repeat_issues(threshold=3)
            if repeat_issues:
                self.log("Warning: The following issues have repeated 3+ times, indicating previous fixes were ineffective!", "WARN")
                for issue_id, count in repeat_issues:
                    self.log(f"  - {issue_id}: appeared {count} times", "WARN")
                self.log("Suggestion: A completely different approach is needed", "WARN")

            self.log_step_header(step_num, total_steps, "Review Paper", "end")

            # Proactive intervention: ask user for direction after first review if score is low
            if self.iteration == 1 and score < 5.0 and self.telegram.is_configured:
                question, options = self._build_intervention_options(
                    score, 0, review_output,
                    trigger="First review score is low",
                )
                self.ask_user_decision(question, options, timeout=900)
                self._asked_this_iteration = True

            # Update paper state
            paper_state["reviews"].append({
                "iteration": self.iteration,
                "timestamp": datetime.now().isoformat(),
                "score": score,
                "log": str(self.log_file),
            })
            paper_state["current_score"] = score
            self.save_paper_state(paper_state)
            self.save_step_checkpoint(step_num, "Review Paper")

        # Check if accepted
        if score >= self.paper_accept_threshold:
            cleanup_done = paper_state.get("post_accept_cleanup_done", False)
            if issue_ids and not cleanup_done:
                post_accept_cleanup = True
                stop_after_cleanup = True
                self.log("", "RAW")
                self.log_section(
                    f"SCORE REACHED {score}/10, Running One Final Issue Cleanup Iteration",
                    "★"
                )
                self.log(
                    f"Accepted threshold reached but {len(issue_ids)} issues remain; "
                    "running one final cleanup iteration.",
                    "INFO",
                )
                paper_state["status"] = "accepted_pending_cleanup"
                paper_state["post_accept_cleanup_done"] = True
                paper_state["accepted_score"] = score
                paper_state["accepted_iteration"] = self.iteration
            else:
                self.log("", "RAW")
                self.log_section(f"PAPER ACCEPTED!  Score: {score}/10 >= {self.paper_accept_threshold}/10", "★")
                paper_state["status"] = "accepted"
                self.save_paper_state(paper_state)
                self._last_score = score
                self.git_commit(f"ACCEPTED: Final score {score}/10")
                self.send_notification(
                    "Paper Accepted",
                    f"{self.project_name.upper()} scored {score}/10 (target: {self.paper_accept_threshold}/10)\n"
                    f"After {self.iteration} iterations",
                )
                return False

        # ── Step 3: Plan ─────────────────────────────────────────────────────
        step_num += 1
        action_plan = None
        planner_output = ""
        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Plan", "skipped")
            # Reload review_output for later phases
            review_file = self.state_dir / "latest_review.md"
            if review_file.exists() and not review_output:
                review_output = review_file.read_text()
            # Reload saved action plan so Execute step can use it
            action_plan = self._load_action_plan()
        else:
            self.log_step_header(step_num, total_steps, "Plan")
            try:
                # Pre-check: self-repair if deeply stagnated (5+ rounds)
                if self.memory.stagnation_count >= 5:
                    self.log(f"Stagnation detected ({self.memory.stagnation_count} iterations), triggering self-repair", "REPAIR")
                    is_stagnating, stagnation_reason = self.memory.is_stagnating()
                    self.self_repair(stagnation_reason)

                # Reset stale experiments from previous crashed runs
                self._reset_stale_action_plan()

                action_plan, planner_output = self.run_planning_phase(review_output)
            except Exception as e:
                self.log(f"Plan phase failed: {e}", "ERROR")

            self.log_step_header(step_num, total_steps, "Plan", "end")
            self.save_step_checkpoint(step_num, "Plan")

        # ── Step 4: Execute ───────────────────────────────────────────────────
        step_num += 1
        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Execute", "skipped")
        else:
            self.log_step_header(step_num, total_steps, "Execute")
            try:
                execute_ok = False
                if action_plan:
                    execute_ok = self._run_execute_phase(action_plan, planner_output)

                if not execute_ok and not self._quota_exhausted:
                    self.log_step("Execute incomplete, using fallback writer", "warning")
                    req_str = ""
                    if paper_requirements:
                        quality_reqs = paper_requirements.get("quality_requirements", [])
                        if quality_reqs:
                            req_str = "\n\nKey quality requirements:\n" + "\n".join(f"- {r}" for r in quality_reqs)

                    self.run_agent(
                        "writer",
                        f"""Please read the latest review report auto_research/state/latest_review.md,
and improve the paper based on the review comments:

1. First address all Major Issues
2. Then address Minor Issues
3. Improve figure quality and information density
4. Ensure compliance with EuroMLSys two-column format (6 pages for body, unlimited for references and appendix)

Notes:
- Keep the core contributions of the paper unchanged
- Ensure LaTeX compiles successfully after each improvement
- Update report.md to keep it in sync{req_str}""",
                        timeout=3600,
                    )
            except Exception as e:
                self.log(f"Execute phase failed: {e}", "ERROR")

            self.log_step_header(step_num, total_steps, "Execute", "end")
            self.save_step_checkpoint(step_num, "Execute")

        # Quota exhaustion: abort iteration, pause, and retry
        if self._quota_exhausted:
            self.iteration -= 1  # Don't count this failed iteration
            self.log("", "RAW")
            self.log_summary_box(f"Iteration ABORTED (quota exhausted)", [
                f"Score: {score}/10 (unchanged)",
                "Writing phase failed: API quota exhausted",
                "Iteration not counted, will retry after quota resets",
            ], inside_phase=False)
            self.save_checkpoint()
            wait_time = 1800  # 30 min default
            self.log(f"Pausing {wait_time}s waiting for API quota to reset...", "ERROR")
            self.send_notification(
                "Quota Exhausted",
                f"Writing failed, pausing {wait_time // 60}min before retry",
                priority="critical"
            )
            RateLimitCountdown(wait_time).run()
            return True  # continue to retry

        # 4. Validate — figure quality check after writing
        step_num += 1
        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Validate", "skipped")
        else:
            self.log_step_header(step_num, total_steps, "Validate")
            if self._should_skip_figure_phase():
                self.log_step("Figure phase skipped (no relevant changes)", "info")
            else:
                self._run_figure_phase()
            self.log_step_header(step_num, total_steps, "Validate", "end")
            self.save_step_checkpoint(step_num, "Validate")

        self.save_paper_state(paper_state)
        self._last_score = score

        # Iteration summary
        self.log("", "RAW")
        gap = self.paper_accept_threshold - score
        if post_accept_cleanup:
            status = "POST_ACCEPT_CLEANUP"
        else:
            status = "CONTINUE" if gap > 0 else "ACCEPTED"
        self.log_summary_box(f"Iteration {self.iteration} Summary", [
            f"Score: {score}/10 (target: {self.paper_accept_threshold}/10)",
            f"Gap: {gap:.1f} points remaining" if gap > 0 else "Target reached!",
            f"Status: {status}",
        ], inside_phase=False)

        # Record to Memory
        self.record_score_to_memory(score)

        # Recompile after writing phase to get the latest PDF
        self.log_step("Recompiling after improvements...", "progress")
        self.compile_latex()

        # Send iteration summary + PDF to Telegram
        self.send_iteration_summary(score, current_score, review_output)

        # Smart human intervention check
        if not post_accept_cleanup:
            self._check_smart_intervention(score, current_score, review_output, planner_success)

        # Cleanup workspace
        self.cleanup_workspace()

        # Git commit
        self.git_commit(f"Iteration {self.iteration}: score {score}/10")

        # Save checkpoint
        self.save_checkpoint()

        # Post-accept cleanup done
        if stop_after_cleanup:
            paper_state["status"] = "accepted"
            self.save_paper_state(paper_state)
            self._last_score = score
            self.log("", "RAW")
            self.log_section(f"PAPER ACCEPTED AFTER CLEANUP  |  Score: {score}/10", "★")
            self.git_commit(f"ACCEPTED: Final score {score}/10 (after cleanup iteration)")
            self.send_notification(
                "Paper Accepted",
                f"{self.project_name.upper()} scored {score}/10 (after cleanup iteration)",
            )
            return False

        # Stagnation detection and self-repair
        is_stagnating, stagnation_reason = self.memory.is_stagnating()
        if is_stagnating:
            self.log(f"Stagnation detected: {stagnation_reason} (stagnation_count={self.memory.stagnation_count})", "WARN")

            if self.memory.stagnation_count >= 3:
                self.log("Triggering self-repair...", "REPAIR")
                self.self_repair(stagnation_reason)
            else:
                self.log("Stagnation count is low, delegating to Meta-Debugger", "WARN")

            if self.memory.stagnation_count >= 3 and self.telegram.is_configured:
                # Use structured intervention with concrete options
                review_src = review_output
                if not review_src and (self.state_dir / "latest_review.md").exists():
                    review_src = (self.state_dir / "latest_review.md").read_text()
                question, options = self._build_intervention_options(
                    score, current_score, review_src or "",
                    trigger=f"Stuck {self.memory.stagnation_count} rounds at {score}/10",
                )
                self.ask_user_decision(question, options, timeout=900)
                self._asked_this_iteration = True

        # Meta-Debugger check
        meta_result = self.check_and_trigger_meta_debug()
        if meta_result == "CONTINUE_WITH_FIX":
            self.log("Meta-Debugger has fixed the system issue", "META")

        return True

    def run_iteration(self) -> bool:
        """Execute one research iteration. Returns whether to continue."""
        if hasattr(self.hooks, 'run_research_iteration'):
            return self.hooks.run_research_iteration(self)

        self.iteration += 1
        self.log(f"\n{'='*60}")
        self.log(f"Iteration {self.iteration} started at {datetime.now()}")
        self.log(f"{'='*60}\n")

        state = self.load_state()
        phase = self.get_current_phase(state)
        self.log(f"Current phase: {phase}")

        if phase == "completed":
            self.log("All phases completed!")
            self.send_notification("Research Completed", "All research phases have been completed!")
            return False

        self.log("No research iteration logic defined in hooks.py", "WARN")
        return False
        # Dead code removed (was unreachable after return False)

    def check_dependencies(self):
        """Check that required CLI tools are available."""
        model_tools = {
            "gemini": "gemini",
            "claude": "claude",
            "codex": "codex",
        }
        tool = model_tools.get(self.model)
        if not tool:
            self.log(f"Error: Unsupported model backend: {self.model}", "ERROR")
            sys.exit(1)

        try:
            subprocess.run([tool, "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            if self.model == "gemini":
                self.log("Error: 'gemini' command not found. Please install: npm install -g @google/gemini-cli", "ERROR")
            elif self.model == "claude":
                self.log("Error: 'claude' command not found. Please install: npm install -g @anthropic-ai/claude-code", "ERROR")
            else:
                self.log("Error: 'codex' command not found. Please install Codex CLI and ensure it is available in PATH.", "ERROR")
            sys.exit(1)

        # Check LaTeX tools in paper mode
        if self.mode == "paper":
            self._check_latex_dependencies()

    def _check_latex_dependencies(self):
        """Check pdflatex and bibtex availability. Offer install if missing."""
        missing = []
        for tool in ("pdflatex", "bibtex"):
            if not shutil.which(tool):
                missing.append(tool)

        if not missing:
            return

        self.log(f"Missing LaTeX tools: {', '.join(missing)}", "WARN")

        install_cmd = self._detect_latex_install_command()
        question = (
            f"LaTeX tools missing: {', '.join(missing)}\n"
            f"Paper mode requires pdflatex and bibtex to compile."
        )
        options = [
            f"I'll install manually, then restart",
            f"Install now ({install_cmd})" if install_cmd else "Install now (no package manager detected)",
            "Continue anyway (compilation will fail)",
        ]

        idx, reply = self.ask_user_decision(question, options, timeout=900, default=0)

        if idx == 1 and install_cmd:
            self.log(f"Running: {install_cmd}", "INFO")
            result = subprocess.run(
                install_cmd, shell=True, capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                self.log(f"Install failed: {result.stderr[:500]}", "ERROR")
                self.log("Please install manually and restart.", "ERROR")
                sys.exit(1)
            self.log("LaTeX tools installed successfully.", "INFO")
        elif idx == 0:
            self.log("Please install LaTeX tools and restart ARK.", "INFO")
            sys.exit(0)
        else:
            self.log("Continuing without LaTeX tools — compilation will fail.", "WARN")

    @staticmethod
    def _detect_latex_install_command() -> str:
        """Detect platform package manager and return texlive install command."""
        managers = [
            ("apt-get", "sudo apt-get install -y texlive-full"),
            ("dnf", "sudo dnf install -y texlive-scheme-full"),
            ("yum", "sudo yum install -y texlive-scheme-full"),
            ("pacman", "sudo pacman -S --noconfirm texlive-full"),
            ("brew", "brew install --cask mactex"),
        ]
        for mgr, cmd in managers:
            if shutil.which(mgr):
                return cmd
        return ""

    # ==================== Research Phase (Deep Research) ====================

    def _should_run_research_phase(self) -> bool:
        """Check if the Research Phase should run.

        Returns True if:
        - deep_research.md does not exist yet
        - skip_deep_research is not set in config
        - A Gemini API key is available
        """
        deep_research_file = self.state_dir / "deep_research.md"
        if deep_research_file.exists():
            return False

        if self.config.get("skip_deep_research", False):
            return False

        try:
            from ark.deep_research import get_gemini_api_key
            api_key = get_gemini_api_key()
            if not api_key:
                return False
        except ImportError:
            return False

        return True

    def _run_research_phase(self):
        """Run the Research Phase: Gemini Deep Research for literature & background.

        This phase runs Deep Research synchronously (blocking) before the Dev Phase.
        It gathers background knowledge, related work, and literature survey
        that subsequent phases use for experiment planning and paper writing.
        """
        self.log("", "RAW")
        self.log_section("Research Phase  |  Literature & Background Survey")

        if self.telegram.is_configured:
            self.telegram.send("<b>🔬 ══ RESEARCH PHASE ══</b>\nRunning Deep Research (5-20 min)...", parse_mode="HTML")

        # Step 1: Run Deep Research
        self.log_step_header(1, 1, "Deep Research (Gemini)")

        from ark.deep_research import run_deep_research, get_gemini_api_key
        api_key = get_gemini_api_key()

        try:
            result = run_deep_research(
                config=self.config,
                output_dir=self.state_dir,
                api_key=api_key,
            )

            if result:
                self.log(f"Deep Research completed: {result}", "INFO")
                self._send_deep_research_telegram(result)
            else:
                self.log("Deep Research returned no result.", "WARN")
                if self.telegram.is_configured:
                    self.telegram.send("Deep Research returned no result — continuing without it.")
        except Exception as e:
            self.log(f"Deep Research failed: {e}", "WARN")
            if self.telegram.is_configured:
                self.telegram.send(f"Deep Research failed: {str(e)[:200]} — continuing without it.")

        self.log("", "RAW")

        # Step 2: Extract citations from Deep Research report
        self._bootstrap_citations_from_deep_research()

        self.log_section("Research Phase Complete")

    # ==================== Citation Bootstrapping ====================

    def _bootstrap_citations_from_deep_research(self):
        """Extract paper titles from Deep Research report via LLM, then fetch BibTeX via API.

        1. LLM reads the report and extracts paper titles as JSON list
        2. Each title is searched via DBLP/CrossRef/arXiv/S2
        3. Found papers get official BibTeX written to references.bib
        4. Not-found titles get a keyword retry, then [NEEDS-CHECK] + Telegram notification
        """
        from ark.citation import bootstrap_citations

        deep_research_file = self.state_dir / "deep_research.md"
        if not deep_research_file.exists():
            return

        bib_path = str(self.latex_dir / "references.bib")
        literature_path = str(self.state_dir / "literature.yaml")

        self.log_step("Extracting citations from Deep Research report...", "progress")

        # Step 1: LLM extracts paper titles from the report
        report_text = deep_research_file.read_text()
        # Truncate if very long to stay within context limits
        if len(report_text) > 15000:
            report_text = report_text[:15000] + "\n\n... (truncated)"

        extract_prompt = f"""Read the following research report and extract ALL academic papers mentioned in it.

For each paper, return a JSON object with these fields:
- "title": the paper's actual full title
- "authors": first author surname (e.g. "Vaswani")
- "year": publication year as integer (e.g. 2017)
- "query": a search query to find it (title + author + year)
- "context": a 1-2 sentence summary of what the report says about this paper (what it does, why it matters)

Return a JSON array. Example:
[
  {{"title": "Attention Is All You Need", "authors": "Vaswani", "year": 2017, "query": "Attention Is All You Need Vaswani 2017", "context": "Introduces the Transformer architecture based solely on attention mechanisms, replacing recurrence and convolutions."}},
  {{"title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding", "authors": "Devlin", "year": 2019, "query": "BERT Pre-training Deep Bidirectional Transformers Devlin 2019", "context": "Proposes bidirectional pre-training for language representations, achieving SOTA on multiple NLP benchmarks."}}
]

Rules:
- "title" must be the paper's actual full title as it would appear on the paper itself
- "context" should summarize what the report says about this paper, NOT what you think the paper is about
- "query" should include the title plus first author surname and year to help search
- If only an abbreviation is given (e.g. "TimeGAN by Yoon et al., 2019"), infer the full title for "title" and construct a rich "query"
- Do NOT include book titles, dataset names, or tool names
- Do NOT invent papers not mentioned in the report
- If no papers are mentioned, return []

## Research Report

{report_text}
"""
        agent_output = self.run_agent("researcher", extract_prompt, timeout=300)

        # Parse the JSON array from agent output
        papers_info = self._parse_paper_info_list(agent_output)
        if not papers_info:
            self.log_step("No paper titles extracted from Deep Research report", "warning")
            return

        titles = [p["title"] for p in papers_info]
        queries = [p["query"] for p in papers_info]
        authors_list = [p.get("authors", "") for p in papers_info]
        years_list = [p.get("year", 0) for p in papers_info]
        contexts_list = [p.get("context", "") for p in papers_info]
        self.log_step(f"Extracted {len(titles)} paper titles, searching APIs...", "progress")

        # Step 2: Search APIs and fetch BibTeX (use queries for search, titles for display)
        result = bootstrap_citations(
            titles, bib_path, literature_path,
            search_queries=queries, authors=authors_list, years=years_list,
            contexts=contexts_list,
        )

        # Step 3: Log results
        if result.found_keys:
            self.log_step(f"Added {len(result.found_keys)} citations to references.bib", "success")

        if result.needs_check:
            self.log_step(f"{len(result.needs_check)} papers not found in any database", "warning")
            # Telegram notification
            self.send_notification(
                "Citation Check",
                f"Deep Research mentioned {len(result.needs_check)} paper(s) not found in academic databases:\n"
                + "\n".join(f"- {t}" for t in result.needs_check[:10]),
                priority="warning",
            )

        # Summary
        total = len(titles)
        found = len(result.found_keys)
        missing = len(result.needs_check)
        self.log_step(f"Citation bootstrap: {found}/{total} found, {missing} needs-check", "success")

    def _parse_title_list(self, agent_output: str) -> list:
        """Parse a JSON array of paper titles from LLM output.

        Handles cases where the LLM wraps JSON in markdown code blocks.
        """
        import json

        if not agent_output:
            return []

        text = agent_output.strip()

        # Strip markdown code block if present
        if "```" in text:
            # Extract content between ``` markers
            import re
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        # Try to find a JSON array in the text
        # Look for the first [ ... ] block
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                titles = json.loads(text[start:end + 1])
                if isinstance(titles, list):
                    return [t for t in titles if isinstance(t, str) and len(t) > 5]
            except json.JSONDecodeError:
                pass

        # Fallback: try line-by-line parsing (one title per line)
        titles = []
        for line in text.split("\n"):
            line = line.strip().strip("-").strip("*").strip('"').strip("'").strip()
            if len(line) > 10 and not line.startswith(("{", "[", "#", "//")):
                titles.append(line)

        return titles

    def _parse_paper_info_list(self, agent_output: str) -> list:
        """Parse a JSON array of {title, query} objects from LLM output.

        Falls back to _parse_title_list if the output is a flat string array.
        """
        import json

        if not agent_output:
            return []

        text = agent_output.strip()

        # Strip markdown code block if present
        if "```" in text:
            import re
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        # Try to find a JSON array
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
                if isinstance(parsed, list):
                    # Check if it's [{title, query}, ...] or ["string", ...]
                    if parsed and isinstance(parsed[0], dict):
                        return [
                            {
                                "title": p.get("title", ""),
                                "query": p.get("query", p.get("title", "")),
                                "authors": p.get("authors", ""),
                                "year": p.get("year", 0),
                                "context": p.get("context", ""),
                            }
                            for p in parsed
                            if isinstance(p, dict) and p.get("title")
                        ]
                    elif parsed and isinstance(parsed[0], str):
                        # Fallback: flat string list, use as both title and query
                        return [{"title": s, "query": s} for s in parsed if isinstance(s, str) and len(s) > 5]
            except json.JSONDecodeError:
                pass

        # Fallback: use _parse_title_list
        titles = self._parse_title_list(agent_output)
        return [{"title": t, "query": t} for t in titles]

    # ==================== Dev Phase (Experiment-First) ====================

    def _should_run_dev_phase(self) -> bool:
        """Check if the dev phase should run before the review loop.

        Returns True if:
        - skip_dev_phase is not set in config
        - No findings.yaml exists (no experiments done yet)
        - No reviews in paper_state.yaml (haven't entered review loop)
        - Dev phase not already completed (check dev_phase_state.yaml)
        """
        if self.config.get("skip_dev_phase", False):
            return False
        dev_state_file = self.state_dir / "dev_phase_state.yaml"
        if dev_state_file.exists():
            try:
                with open(dev_state_file) as f:
                    dev_state = yaml.safe_load(f) or {}
                if dev_state.get("status") == "completed":
                    return False
            except Exception:
                pass

        # If findings already exist and paper has reviews, skip
        paper_state = self.load_paper_state()
        if paper_state.get("reviews"):
            return False

        # If paper already has substantial content, skip
        if self._paper_has_substantial_content():
            return False

        return True

    def _load_dev_phase_state(self) -> dict:
        """Load dev phase state."""
        dev_state_file = self.state_dir / "dev_phase_state.yaml"
        if dev_state_file.exists():
            try:
                with open(dev_state_file) as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {"iteration": 0, "status": "pending", "experiments": []}

    def _save_dev_phase_state(self, state: dict):
        """Save dev phase state."""
        dev_state_file = self.state_dir / "dev_phase_state.yaml"
        with open(dev_state_file, "w") as f:
            yaml.dump(state, f, default_flow_style=False, allow_unicode=True)

    def _run_dev_phase(self):
        """Run the Dev Phase: iterative experiments → initial paper draft.

        Flow per iteration:
          1. Plan experiments (planner)
          2. Run ALL experiments in batch (experimenter + compute)
          3. Analyze results (researcher)
          4. Evaluate completeness (planner)
          → If sufficient: write initial draft and exit
          → If not: next iteration

        After loop: writer produces complete initial draft.
        """
        max_dev_iters = self.config.get("max_dev_iterations", 3)
        dev_state = self._load_dev_phase_state()

        # Resume from previous iteration if restarting
        start_iter = dev_state.get("iteration", 0)

        self.log("", "RAW")
        self.log_section(f"Dev Phase  |  Building experiments & data  |  max {max_dev_iters} iterations")
        self._send_dev_phase_telegram("start", 0, max_dev_iters)

        # Gather Deep Research context (Research Phase already completed by now)
        deep_research_file = self.state_dir / "deep_research.md"
        deep_research_ctx = ""
        if deep_research_file.exists():
            deep_research_ctx = deep_research_file.read_text()[:3000]

        research_idea = self.config.get("research_idea", "")
        findings_summary = self._load_findings_summary()

        for dev_iter in range(start_iter + 1, max_dev_iters + 1):
            dev_state["iteration"] = dev_iter
            dev_state["status"] = "in_progress"
            self._save_dev_phase_state(dev_state)

            self.log("", "RAW")
            self.log_section(f"Dev Phase: Iteration {dev_iter}/{max_dev_iters}")
            self._send_dev_phase_telegram("iteration", dev_iter, max_dev_iters)

            # Step 1: Plan experiments
            self.log_step_header(1, 4, "Plan Experiments")
            plan_output = self.run_agent("planner", f"""
You are planning experiments for a research project. This is Dev Phase iteration {dev_iter}/{max_dev_iters}.

## Research Idea
{research_idea}

## Deep Research Context
{deep_research_ctx[:2000] if deep_research_ctx else "No deep research available yet."}

## Current Findings
{findings_summary if findings_summary else "No experiments run yet."}

## Task
Design a comprehensive experiment plan:
1. What experiments to run (with specific scripts, parameters, baselines)
2. What metrics to measure
3. What baselines to compare against
4. Expected outcomes

Save the experiment plan to auto_research/state/experiment_plan.yaml with format:
```yaml
experiments:
  - id: "exp1"
    title: "Experiment title"
    description: "What to test"
    script: "path/to/script.py"
    parameters: "key params"
    metrics: ["metric1", "metric2"]
    baseline: "comparison baseline"
```
""", timeout=1200)
            self.log_step_header(1, 4, "Plan Experiments", "end")

            # Step 2: Run experiments (batch)
            self.log_step_header(2, 4, "Run Experiments")
            self._send_dev_phase_telegram("experiments", dev_iter, max_dev_iters)

            compute_ctx = self._compute_backend.setup()
            compute_instructions = self._compute_backend.get_agent_instructions()

            try:
                exp_output = self.run_agent("experimenter", f"""
Execute ALL planned experiments for this dev iteration.

## Experiment Plan
Read auto_research/state/experiment_plan.yaml for the full plan.

## Previous Planner Output
{plan_output[:1500] if plan_output else "See experiment_plan.yaml"}

{compute_instructions}

## Requirements
- Write and submit ALL experiment scripts at once
- Each script should save results to results/ directory
- Use clear naming: results/exp1_results.csv, results/exp2_results.csv, etc.
- Handle errors gracefully (log failures, continue with remaining experiments)
""", timeout=1800)

                self.log_step("Waiting for all experiments to complete...", "progress")
                self._compute_backend.wait_for_completion(max_wait_hours=4)
                self._compute_backend.collect_results()
            finally:
                self._compute_backend.teardown()

            self.log_step_header(2, 4, "Run Experiments", "end")

            # Step 3: Analyze results
            self.log_step_header(3, 4, "Analyze Results")
            research_output = self.run_agent("researcher", f"""
Analyze ALL experiment results from this dev iteration.

## What was run
{exp_output[:1500] if exp_output else "Check results/ directory for new files."}

## Task
1. Check all result files in results/ directory
2. Verify experiments completed successfully (no errors, valid outputs)
3. Summarize key findings
4. Compare against baselines
5. Update auto_research/state/findings.yaml with ALL findings

Format for findings.yaml:
```yaml
findings:
  - id: "finding1"
    experiment: "exp1"
    result: "Key result description"
    metrics: {{metric1: value1, metric2: value2}}
    significance: "Why this matters"
    supports_claim: "Which paper claim this supports"
```
""", timeout=1200)
            self.log_step_header(3, 4, "Analyze Results", "end")

            # Step 4: Evaluate completeness
            self.log_step_header(4, 4, "Evaluate Completeness")

            # Reload findings
            findings_summary = self._load_findings_summary()

            eval_output = self.run_agent("planner", f"""
Evaluate whether we have sufficient experimental data for the paper.

## Research Idea
{research_idea}

## Current Findings
{findings_summary}

## Researcher Analysis
{research_output[:1500] if research_output else "No analysis available."}

## Task
Determine if the experiments are sufficient to write a complete paper:
1. Do we have data for ALL major claims?
2. Are baselines properly compared?
3. Are the results statistically significant?
4. Are there obvious gaps that need more experiments?

Output your evaluation in JSON format:
```json
{{
  "sufficient": true/false,
  "coverage_pct": 0-100,
  "gaps": ["gap1", "gap2"],
  "recommendation": "proceed_to_writing" | "need_more_experiments",
  "reason": "explanation"
}}
```
""", timeout=600)
            self.log_step_header(4, 4, "Evaluate Completeness", "end")

            # Parse evaluation
            import json as _json
            import re as _re
            sufficient = False
            try:
                json_match = _re.search(r'\{[^{}]*"sufficient"[^{}]*\}', eval_output, _re.DOTALL)
                if json_match:
                    eval_json = _json.loads(json_match.group())
                    sufficient = eval_json.get("sufficient", False)
                else:
                    sufficient = '"sufficient": true' in eval_output.lower()
            except Exception:
                sufficient = "sufficient.*true" in eval_output.lower()

            if sufficient:
                self.log_step("Experiments sufficient, proceeding to initial draft", "success")
                break

            self.log_step(f"Dev iter {dev_iter}: more experiments needed", "warning")

        # Dev phase complete — write initial draft
        self.log("", "RAW")
        self.log_section("✏️ Writing Initial Paper Draft")
        self._send_dev_phase_telegram("writing", 0, 0)

        # Generate figures from results
        self.log_step("Generating figures from experiment results...", "progress")
        self.generate_figures()

        # Generate AI concept figures (Nano Banana) if enabled
        if self.config.get("figure_generation") == "nano_banana":
            self.log_step("Generating AI concept figures (Nano Banana)...", "progress")
            self._generate_nano_banana_figures()

        # Writer produces complete initial draft
        paper_requirements = self.load_paper_requirements()
        req_summary = yaml.dump(paper_requirements, allow_unicode=True) if paper_requirements else "No special requirements"
        findings_summary = self._load_findings_summary()

        base_prompt = self.config.get("initial_paper_writing_prompt", "")
        if base_prompt:
            prompt = base_prompt.replace("{req_summary}", req_summary)
            # Enhance with findings context
            prompt += f"\n\n## Experiment Findings\n{findings_summary}"
        else:
            venue_pages = self.config.get('venue_pages', 9)
            latex_dir = self.config.get('latex_dir', 'paper')
            figures_dir = self.config.get('figures_dir', 'paper/figures')
            prompt = f"""Write a COMPLETE, SUBMISSION-READY research paper draft.

## Research Idea
{research_idea}

## Experiment Findings
{findings_summary}

## Paper Requirements
{req_summary}

## MANDATORY — every item below is required, NO exceptions:

### 1. All sections must be fully written (zero placeholders)
- Abstract (150-250 words): problem, method, key results with actual numbers
- Introduction: motivation, gap, 3-5 numbered contributions, paper roadmap
- Related Work: 3-4 subsections, at least 10 cited works, explain how we differ
- Method: full technical description, equations where appropriate
- Experiments: setup table, baselines listed, main results table with numbers, ablation
- Analysis/Discussion: explain WHY results are good/bad, failure cases
- Conclusion: 1 paragraph summary + 1 paragraph future work

### 2. Figures are REQUIRED (paper will fail without them)
- Minimum 2 figures in the body:
  a) System/architecture overview (TikZ diagram OR simple block diagram in LaTeX)
  b) Main results figure (bar chart or line plot from actual results data)
- Each figure needs: \\caption{{...}} and \\label{{fig:...}}
- Generate result figures using Python: save to {figures_dir}/ then \\includegraphics

### 3. Data integrity
- Every performance claim must use actual numbers from findings
- Include at least one \\begin{{table}} comparing against baselines
- No vague statements like "our method is better" — use exact percentages

### 4. Page target: {venue_pages} pages of body text
- Every section must be substantively written
- Related Work and Experiments sections should each be 1.5-2 pages
- Do NOT leave any section with only 1-2 sentences

### 5. LaTeX mechanics
- Edit {latex_dir}/main.tex directly
- Verify compilation: cd {latex_dir} && pdflatex -interaction=nonstopmode main.tex
- All \\ref and \\cite must resolve (no undefined references)
- If figures don't exist yet, create simple placeholder TikZ diagrams

Produce the complete paper. Do not stop until all sections are written and it compiles.
"""

        self.run_agent("writer", prompt, timeout=3600)

        # Compile initial draft
        self.log_step("Compiling initial draft...", "progress")
        if self.compile_latex():
            self.log_step("Initial draft compiled successfully", "success")
            # Send initial draft PDF via Telegram
            if self.telegram.is_configured:
                pdf_path = self.latex_dir / "main.pdf"
                if pdf_path.exists():
                    ok = self.telegram.send_document(
                        pdf_path,
                        caption=f"📄 <b>Initial draft ready</b> — {self.project_name}\n"
                                f"Dev Phase complete ({dev_state['iteration']} iterations)\n"
                                f"Entering Review Phase now.",
                    )
                    if not ok:
                        self.telegram.send("📄 Initial draft compiled (PDF too large to send, download from portal)")
        else:
            self.log_step("Initial compilation failed, writer will fix in review loop", "warning")

        # Mark dev phase as completed
        dev_state["status"] = "completed"
        dev_state["completed_at"] = datetime.now().isoformat()
        self._save_dev_phase_state(dev_state)

        self._send_dev_phase_telegram("complete", dev_state["iteration"], max_dev_iters)
        self.git_commit(f"Dev phase complete: {dev_state['iteration']} iterations")

        self.log("", "RAW")
        self.log_section(f"Dev Phase Complete  |  {dev_state['iteration']} iterations  |  → Review Phase")

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

    def _reset_stale_action_plan(self):
        """Reset stale pending/in_progress experiments from a previous crashed run.

        If the process was killed mid-iteration, action_plan.yaml may have
        experiments stuck in 'pending' or 'in_progress'. Reset them so
        the planner generates a fresh plan instead of re-running stale tasks.
        """
        action_plan = self._load_action_plan()
        issues = action_plan.get("issues", [])
        if not issues:
            return

        stale = [
            i for i in issues
            if i.get("status") in ("pending", "in_progress")
        ]
        if not stale:
            return

        self.log(f"Resetting {len(stale)} stale tasks from previous run", "INFO")
        for issue in stale:
            issue["status"] = "reset"
        self._save_action_plan(action_plan)

    def _summarize_review_for_telegram(self) -> str:
        """Extract major issues from latest_review.md for a Telegram summary."""
        review_file = self.state_dir / "latest_review.md"
        if not review_file.exists():
            return "No review details available."
        try:
            text = review_file.read_text()
            # Find major issues section
            for marker in ["Major Issues", "## Major", "### Major", "重大问题"]:
                idx = text.find(marker)
                if idx >= 0:
                    snippet = text[idx:idx + 600]
                    # Trim to last complete line
                    last_nl = snippet.rfind("\n")
                    if last_nl > 100:
                        snippet = snippet[:last_nl]
                    return snippet
            # Fallback: first 400 chars after score
            return text[:400]
        except Exception:
            return "Could not read review."

    def _extract_issue_summaries(self, review_output: str, level: str = "major") -> list:
        """Parse review markdown for issue summaries.

        Looks for patterns like 'M1: description', '- M2: description',
        '**M3**: description' for major issues, or 'm1:', 'm2:' for minor.

        Args:
            review_output: Raw review markdown text.
            level: "major" for M-prefixed issues, "minor" for m-prefixed.

        Returns:
            List of (id, one_line_summary) tuples.
        """
        if not review_output:
            return []

        prefix = "M" if level == "major" else "m"
        # Match patterns: M1: desc, - M1: desc, **M1**: desc, **M1:** desc
        pattern = rf'(?:^|\n)\s*[-*]*\s*\**({prefix}\d+)\**:?\**\s*(.+)'
        matches = re.findall(pattern, review_output, re.IGNORECASE)

        results = []
        seen = set()
        for issue_id, summary in matches:
            issue_id = issue_id.upper() if level == "major" else issue_id.lower()
            if issue_id not in seen:
                seen.add(issue_id)
                # Trim to one line, max 80 chars
                summary = summary.strip().split("\n")[0][:80]
                results.append((issue_id, summary))
        return results

    def _build_intervention_options(self, score: float, prev_score: float,
                                    review_output: str, trigger: str) -> tuple:
        """Build concrete intervention choices from the actual review.

        Returns (question_text, options_list) for ask_user_decision().
        """
        major_issues = self._extract_issue_summaries(review_output, "major")
        minor_issues = self._extract_issue_summaries(review_output, "minor")

        # Annotate repeated issues
        repeat_map = {}
        if hasattr(self.memory, 'get_repeat_issues'):
            for iid, cnt in self.memory.get_repeat_issues(threshold=2):
                repeat_map[iid.upper()] = cnt

        options = []

        # Add top 2 major issues as individual focus options
        for issue_id, summary in major_issues[:2]:
            label = f"Focus on {issue_id}: {summary}"
            repeat_cnt = repeat_map.get(issue_id.upper(), 0)
            if repeat_cnt >= 2:
                label += f" [repeated {repeat_cnt}x]"
            options.append(label)

        # "Address all N major issues"
        if len(major_issues) > 1:
            options.append(f"Address all {len(major_issues)} major issues")

        # If any issue repeated 3+, offer "try different approach"
        highly_repeated = [(iid, cnt) for iid, cnt in repeat_map.items() if cnt >= 3]
        if highly_repeated:
            worst_id = max(highly_repeated, key=lambda x: x[1])[0]
            matching = [s for i, s in major_issues if i.upper() == worst_id]
            desc = matching[0] if matching else worst_id
            options.append(f"Try different approach for {worst_id}: {desc}"[:80])

        # Always add custom option
        options.append("Custom direction (type your response)")

        # Ensure at least 2 options
        if len(options) < 2:
            options = [
                "Continue with reviewer recommendations",
                "Custom direction (type your response)",
            ]

        score_delta = score - prev_score
        delta_str = f"{score_delta:+.1f}" if prev_score > 0 else ""
        question = (
            f"{self.project_name} iter {self.iteration}: {score}/10{delta_str}\n"
            f"Trigger: {trigger}"
        )

        return question, options

    def _check_smart_intervention(self, score: float, prev_score: float,
                                  review_output: str, planner_success: bool):
        """Check trigger conditions and ask human with concrete choices."""
        # Guards: skip if not applicable
        if not self.telegram.is_configured:
            return
        if not self.config.get("smart_intervention", True):
            return
        if self._asked_this_iteration:
            return
        # Already handled by hard-coded stagnation block
        if hasattr(self.memory, 'stagnation_count') and self.memory.stagnation_count >= 3:
            return
        # Already handled by first-review block
        if self.iteration == 1 and score < 5.0:
            return

        score_delta = score - prev_score
        stagnation_count = getattr(self.memory, 'stagnation_count', 0)
        repeat_issues = self.memory.get_repeat_issues(threshold=3) if hasattr(self.memory, 'get_repeat_issues') else []

        trigger = None

        # T1: Score regression >= 0.5
        if score_delta <= -0.5:
            trigger = f"Score dropped {score_delta:+.1f} (from {prev_score} to {score})"

        # T2: Flat score + early stagnation
        elif score_delta == 0 and stagnation_count >= 2:
            trigger = f"Score unchanged at {score}/10 for {stagnation_count} rounds"

        # T3: Any single issue repeated 5+ times
        elif any(count >= 5 for _, count in repeat_issues):
            worst = max(repeat_issues, key=lambda x: x[1])
            trigger = f"Issue '{worst[0]}' has repeated {worst[1]} times"

        # T4: Planner failed (not quota)
        elif not planner_success and not self._quota_exhausted:
            trigger = "Planner cycle failed — agent may be stuck"

        # T5: Score < 6 after 3+ iterations, not improving
        elif score < 6.0 and self.iteration >= 3 and score_delta <= 0:
            trigger = f"Score still {score}/10 after {self.iteration} iterations (not improving)"

        # T6: 3+ different issues each repeating 3+ times (scattered stagnation)
        elif len(repeat_issues) >= 3:
            trigger = f"{len(repeat_issues)} different issues each repeating 3+ times"

        if not trigger:
            return

        self.log(f"Smart intervention triggered: {trigger}", "INFO")

        question, options = self._build_intervention_options(
            score, prev_score, review_output, trigger,
        )
        idx, reply = self.ask_user_decision(question, options, timeout=900)
        self._asked_this_iteration = True
        if reply:
            self.log(f"User intervention reply: {reply[:200]}", "INFO")

    def _send_dev_phase_telegram(self, event: str, current: int, total: int):
        """Send dev phase notifications to Telegram."""
        if not self.telegram.is_configured:
            return
        try:
            if event == "start":
                self.telegram.send(f"<b>⚙️ ══ DEV PHASE ══</b>\nMax {total} iterations", parse_mode="HTML")
            elif event == "iteration":
                self.telegram.send(f"🔬 Dev {current}/{total}: Planning experiments...")
            elif event == "experiments":
                self.telegram.send(f"🧪 Dev {current}/{total}: Running experiments...")
            elif event == "writing":
                self.telegram.send(f"✏️ Dev done → Writing initial draft...")
            elif event == "complete":
                self.telegram.send(f"<b>✅ Dev Phase Complete</b> → Review Phase", parse_mode="HTML")
        except Exception:
            pass

    def _write_cost_report(self):
        """Write per-agent and total cost/stats to cost_report.yaml."""
        if not self._agent_stats:
            return

        # Aggregate per agent type
        by_type = {}
        for stat in self._agent_stats:
            atype = stat["agent_type"]
            if atype not in by_type:
                by_type[atype] = {"calls": 0, "total_seconds": 0, "total_prompt_len": 0, "total_output_len": 0}
            by_type[atype]["calls"] += 1
            by_type[atype]["total_seconds"] += stat.get("elapsed_seconds", 0)
            by_type[atype]["total_prompt_len"] += stat.get("prompt_len", 0)
            by_type[atype]["total_output_len"] += stat.get("output_len", 0)

        total_calls = sum(d["calls"] for d in by_type.values())
        total_time = sum(d["total_seconds"] for d in by_type.values())

        report = {
            "generated_at": datetime.now().isoformat(),
            "total_agent_calls": total_calls,
            "total_agent_seconds": total_time,
            "per_agent": by_type,
            "raw_stats": self._agent_stats[-100:],  # Keep last 100 entries
        }

        report_path = self.state_dir / "cost_report.yaml"
        with open(report_path, "w") as f:
            yaml.dump(report, f, default_flow_style=False, allow_unicode=True)
        self.log(f"Cost report written: {report_path} ({total_calls} calls, {total_time}s total)", "INFO")

    def run(self):
        """Main loop."""
        self.check_dependencies()

        # Try to resume
        self.resume_from_checkpoint()

        self.log_section(f"{self.project_name.upper()} Started  |  Mode: {self.mode.upper()}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        self.log(f"Max iterations: {self.max_iterations}  |  Max time: {self.max_end_time.strftime('%Y-%m-%d %H:%M')}", "RAW")
        self.log(f"Log: {self.log_file}", "RAW")
        self.log("", "RAW")

        # Start background Telegram listener for bidirectional communication
        self.start_telegram_listener()

        # Send session banner (replaces verbose "Started" notification)
        self._send_session_banner()

        # Research Phase: run Deep Research (blocking) before other phases
        if self._should_run_research_phase():
            self._run_research_phase()

        # max_iterations is per-run: adjust to be relative to resumed iteration
        max_iteration_target = self.iteration + self.max_iterations

        try:
            # Paper mode: run Dev Phase first if needed
            if self.mode == "paper" and self._should_run_dev_phase():
                self._run_dev_phase()

            while (
                datetime.now() < self.max_end_time
                and self.iteration < max_iteration_target
            ):
                if self.mode == "paper":
                    should_continue = self.run_paper_iteration()
                elif self.mode == "dev":
                    should_continue = self.run_dev_iteration()
                else:
                    should_continue = self.run_iteration()

                if not should_continue:
                    break

        except KeyboardInterrupt:
            self.log("", "RAW")
            self.log_section("INTERRUPTED BY USER", "!")
        except Exception as e:
            self.log("", "RAW")
            self.log_section(f"ERROR: {str(e)[:50]}", "!")
            self.send_notification("Error", f"{self.project_name.upper()}: {e}")
            raise
        finally:
            self.stop_telegram_listener()
            # Always write cost report
            self._write_cost_report()

        # End summary
        self.log("", "RAW")
        if self.mode == "paper":
            paper_state = self.load_paper_state()
            final_score = paper_state.get('current_score', 0)
            status = paper_state.get('status', 'unknown')
            self.log_section(f"{self.project_name.upper()} Finished  |  Score: {final_score}/10  |  Status: {status.upper()}")
            if status not in ("accepted",):
                self.send_notification(
                    f"{self.project_name.upper()} Finished",
                    f"Score: {final_score}/10 (target: {self.paper_accept_threshold}/10)\n"
                    f"Iterations: {self.iteration} | Status: {status}\n\n"
                    f"Reply with a new direction →\nauto-applied on next ark run",
                    priority="critical",
                )
        elif self.mode == "dev":
            dev_state = self.load_dev_state()
            tasks = dev_state.get("tasks", [])
            completed = len([t for t in tasks if t.get("status") == "completed"])
            total = len(tasks)
            review_scores = dev_state.get("code_review_scores", [])
            latest_review = review_scores[-1]["score"] if review_scores else 0
            self.log_section(f"{self.project_name.upper()} Dev Finished  |  Tasks: {completed}/{total}  |  Review: {latest_review}/10")
            self.send_notification(
                f"{self.project_name.upper()} Dev Finished",
                f"Tasks: {completed}/{total} | Review: {latest_review}/10\n"
                f"Iterations: {self.iteration}\n\n"
                f"Reply: next steps / 'paper' / 'done'",
                priority="critical",
            )
        else:
            self.log_section(f"{self.project_name.upper()} Finished  |  Iterations: {self.iteration}")
            self.send_notification(
                f"{self.project_name.upper()} Research Completed",
                f"Iterations: {self.iteration}\n\n"
                f"Reply: next steps / 'done'",
                priority="critical",
            )
        self.log(f"Total iterations: {self.iteration}", "RAW")

    # ═══════════════════════════════════════════════════════════
    #  Citation Verification (runs every iteration)
    # ═══════════════════════════════════════════════════════════

    def _run_citation_verification(self):
        """Verify references.bib, fix errors, mark NEEDS-CHECK, clean unused."""
        from ark.citation import verify_bib, fix_bib, cleanup_unused, mark_needs_check_in_tex

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
                    summary.append(f"{len(corrected)} corrected")
                if needs_check:
                    summary.append(f"{len(needs_check)} needs-check")
                if summary:
                    self.log_step(f"Citations: {', '.join(summary)}", "success")

            # 4. Enforce critical citations — if writer dropped a MUST CITE paper, add it back
            self._enforce_critical_citations(lit_path, tex_dir)

            # 5. Mark [NEEDS-CHECK] citations in tex (reads from literature.yaml)
            marked = mark_needs_check_in_tex(bib_str, tex_dir, literature_path=lit_path)
            if marked:
                self.log_step(f"Marked {marked} [NEEDS-CHECK] citation(s) in tex", "success")

            # 6. Clean up unused entries
            removed = cleanup_unused(bib_str, tex_dir)
            if removed:
                self.log_step(f"Removed {len(removed)} unused citations", "success")

            # 7. Recompile if anything changed
            if (results and (corrected or needs_check)) or marked or removed:
                self.log_step("Recompiling after citation updates...", "progress")
                self.compile_latex()

        except Exception as e:
            self.log(f"Citation verification error: {e}", "WARN")

    def _enforce_critical_citations(self, lit_path: str, tex_dir: str):
        """Check that all critical (MUST CITE) papers are cited in tex.

        If a critical paper is missing from tex, ask writer to add it.
        Checks both references and needs_check in literature.yaml.
        """
        import yaml

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
