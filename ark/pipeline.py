"""PipelineMixin: main run loop, paper iteration, research iteration, dependency check."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import yaml
from datetime import datetime, timedelta
from pathlib import Path

from ark.execution import QuotaExhaustedError
from ark.ui import RateLimitCountdown


# --------------- Title generation helpers ---------------

_TITLE_MIN_LEN = 10
_TITLE_MAX_LEN = 200
_TITLE_MAX_RETRIES = 3


def _generate_title_via_llm(idea_text: str, timeout: int = 60) -> str:
    """Call ``claude -p`` to generate a paper title from idea text.

    Returns the title string, or "" on failure.  The prompt is tightly
    constrained: output ONLY the title, nothing else.
    """
    prompt = (
        "You are a scientific title generator. "
        "Given the research summary below, output EXACTLY ONE concise academic paper title.\n\n"
        "Rules:\n"
        "- Output ONLY the title text, nothing else\n"
        "- No quotes, no labels, no prefixes like 'Title:'\n"
        "- No explanation, no newlines, no markdown\n"
        "- Between 10 and 200 characters\n\n"
        f"Research summary:\n{idea_text[:4000]}"
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            ["claude", "--print", "-p", prompt],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode != 0:
            return ""
        title = result.stdout.strip().strip('"').strip("'").strip()
        # Strip common LLM prefix leaks
        for prefix in ("Title:", "title:", "Title :", "Generated title:"):
            if title.lower().startswith(prefix.lower()):
                title = title[len(prefix):].strip().strip('"').strip("'").strip()
        return title
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        return ""


def _validate_title(title: str) -> bool:
    """Check that a title is plausible."""
    if not title:
        return False
    if len(title) < _TITLE_MIN_LEN or len(title) > _TITLE_MAX_LEN:
        return False
    # Reject if it looks like LLM meta-output rather than a real title
    lower = title.lower()
    if any(phrase in lower for phrase in (
        "here is", "i suggest", "current title", "appropriate",
        "as requested", "certainly", "sure,",
    )):
        return False
    # Must contain at least one letter
    if not any(c.isalpha() for c in title):
        return False
    return True


def _fallback_title_from_idea(idea_text: str) -> str:
    """Deterministic fallback: extract first substantive sentence from idea text."""
    for line in idea_text.splitlines():
        line = line.strip().lstrip("#").lstrip("-").lstrip("*").strip()
        if len(line) >= _TITLE_MIN_LEN and not line.startswith("```"):
            # Truncate to first sentence or max length
            for sep in (". ", "。", "! ", "? "):
                idx = line.find(sep)
                if 0 < idx < _TITLE_MAX_LEN:
                    line = line[:idx]
                    break
            if len(line) > _TITLE_MAX_LEN:
                line = line[:_TITLE_MAX_LEN - 3] + "..."
            return line
    # Absolute last resort
    return idea_text[:80].strip().replace("\n", " ")


class PipelineMixin:
    """Mixin providing the top-level pipeline orchestration.

    Expects self to have: iteration, max_iterations, max_end_time, mode, model,
    project_name, code_dir, config, log, log_section, log_phase, log_step,
    log_summary_box, run_agent, memory, paper_accept_threshold,
    compile_latex, pdf_to_images, _run_figure_phase, _should_skip_figure_phase,
    generate_figures, run_planning_phase, _run_execute_phase, run_planner_cycle, self_repair,
    parse_review_score, extract_issue_ids, record_score_to_memory,
    cleanup_workspace, git_commit, save_checkpoint, send_notification,
    load_paper_state, save_paper_state, load_paper_requirements,
    _should_run_paper_initialize, _check_needs_experiment,
    _check_needs_literature_search, load_state, save_state, get_current_phase,
    check_user_updates, _last_score, hooks, _agent_stats, _write_cost_report.
    """

    @property
    def _research_idea(self) -> str:
        """Get research idea from config, checking both field names."""
        return self.config.get("research_idea", "") or self.config.get("idea", "")

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
                    f"LaTeX compilation failed after {MAX_COMPILE_RETRIES} writer attempts.",
                    options=[
                        "Skip this iteration",
                        f"Retry with {MAX_COMPILE_RETRIES} more writer attempts",
                        "I'll fix manually, then continue",
                    ],
                    timeout=900, default=0,
                    what_happened=(
                        f"LaTeX failed to compile {MAX_COMPILE_RETRIES} times in a row "
                        f"this iteration. The writer agent could not recover."
                    ),
                    background=[
                        f"Iteration {self.iteration}",
                        f"Latest errors (truncated): {errors[:300]}",
                    ],
                    option_details=[
                        "Score stays at the last value; the iteration is marked done and we move on.",
                        f"Spends another ~{MAX_COMPILE_RETRIES} writer attempts (~6 min) before giving up again.",
                        "Pauses here. You fix the .tex files in your editor and reply when ready; I'll re-compile once.",
                    ],
                    phase="latex_compile",
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
            self.notify_progress("Compile", "PDF generated", level="done")

        # Citation Verification & Cleanup (runs every iteration)
        self._run_citation_verification()

        # Convert PDF to images for visual review
        page_images = self._maybe_generate_page_images()
        visual_review_section = ""
        if page_images:
            # Load figure manifest to tell reviewer which figures are AI-generated
            figure_types_section = ""
            try:
                from ark.figure_manifest import load_manifest, get_protected_files
                manifest = load_manifest(self.figures_dir)
                figures = manifest.get("figures", {})
                ai_figs = [f for f, info in figures.items()
                           if info.get("source") in ("paperbanana", "nano_banana")]
                mpl_figs = [f for f, info in figures.items()
                            if info.get("source") == "matplotlib"]
                if ai_figs or mpl_figs:
                    figure_types_section = "\n\nFigure sources (for review guidance):\n"
                    if ai_figs:
                        figure_types_section += f"- AI-generated concept figures (do not flag for matplotlib style): {', '.join(ai_figs)}\n"
                    if mpl_figs:
                        figure_types_section += f"- Matplotlib data plots (can flag for code fixes): {', '.join(mpl_figs)}\n"
            except Exception:
                pass

            visual_review_section = f"""

## Visual Review

Please use the Read tool to read the following paper page images for visual review:
{chr(10).join(f'- {img}' for img in page_images)}

Key checks:
- Are figure sizes appropriate and fonts clearly readable?
- Is the layout professional (alignment, spacing, margins)?
- Is the information density appropriate?
- Does the overall visual quality meet research publication standards?
{figure_types_section}"""

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
            self.notify_progress(
                "Review",
                f"Score {score}/10 ({delta_str} from last)",
                level="done" if score_delta >= 0 else "warn",
            )

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
                background = self._build_decision_background(
                    review_output, options, score=score,
                )
                self.ask_user_decision(
                    question, options, timeout=900,
                    what_happened=(
                        f"First review came back at {score}/10 — below the 5.0 floor "
                        f"(target {self.paper_accept_threshold}/10)."
                    ),
                    background=background,
                    option_details=self._build_option_details(options, review_output),
                    phase="first_review",
                )
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
            try:
                n_actions = 0
                if isinstance(action_plan, dict):
                    n_actions = len(action_plan.get("actions") or action_plan.get("issues") or [])
                elif isinstance(action_plan, list):
                    n_actions = len(action_plan)
                self.notify_progress("Plan", f"{n_actions} action(s) queued", level="done")
            except Exception:
                self.notify_progress("Plan", "ready", level="done")

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
                    self._check_human_intervention(stage="Execute")

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
            try:
                _ok = bool(execute_ok)  # noqa: F821 - defined in the try above
            except NameError:
                _ok = False
            self.notify_progress(
                "Execute",
                "completed" if _ok else "incomplete (fallback writer used)",
                level="done" if _ok else "warn",
            )

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
            self.notify_progress("Validate", "figures checked", level="done")

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

        # ── Pre-delivery checks (all hard guarantees) ──
        # Order matters: citation verification can add [NEEDS-CHECK] markers
        # that push the paper over the page limit, so enforce page count AFTER
        # citation verification.
        self.log_step("Pre-delivery checks...", "progress")
        self._ensure_clearpage_before_bibliography()
        self._ensure_float_barrier()
        self.compile_latex()
        self._fix_overfull(context="pre-delivery")
        self._run_citation_verification()
        try:
            self._enforce_page_count(context="pre-delivery")
        except QuotaExhaustedError as e:
            self.iteration -= 1  # Don't count this failed iteration
            self.log("", "RAW")
            self.log_summary_box("Iteration ABORTED (quota exhausted during page enforcement)", [
                f"Score: {score}/10 (unchanged)",
                f"Page count: {e.page_count:.1f}/{e.venue_pages} (over limit, cannot compress)",
                "Iteration not counted, will retry after quota resets",
            ], inside_phase=False)
            self.save_checkpoint()
            wait_time = 1800  # 30 min
            self.log(f"Pausing {wait_time}s waiting for API quota to reset...", "ERROR")
            self.send_notification(
                "Quota Exhausted",
                f"Page compression failed ({e.page_count:.1f}/{e.venue_pages} pages), "
                f"pausing {wait_time // 60}min before retry",
                priority="critical",
            )
            RateLimitCountdown(wait_time).run()
            return True  # continue to retry

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
                trigger = f"Stuck {self.memory.stagnation_count} rounds at {score}/10"
                question, options = self._build_intervention_options(
                    score, current_score, review_src or "", trigger=trigger,
                )
                background = self._build_decision_background(
                    review_src or "", options, score=score,
                )
                self.ask_user_decision(
                    question, options, timeout=900,
                    what_happened=(
                        f"Stagnation triggered: score has stayed at {score}/10 for "
                        f"{self.memory.stagnation_count} consecutive iterations "
                        f"(progress < 0.3 each round). Self-repair will trigger at 5 rounds."
                    ),
                    background=background,
                    option_details=self._build_option_details(options, review_src or ""),
                    phase="stagnation_intervention",
                )
                self._asked_this_iteration = True

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

        idx, reply = self.ask_user_decision(
            question, options, timeout=900, default=0,
            what_happened=f"Required LaTeX binaries are missing: {', '.join(missing)}.",
            background=[
                "Paper mode needs pdflatex + bibtex to compile.",
                f"Install command detected: {install_cmd or 'none'}",
            ],
            option_details=[
                "Exits ARK so you can install manually, then re-launch.",
                "Runs the install command above (needs sudo for apt/dnf).",
                "Proceeds without LaTeX — the compile step will fail every iteration.",
            ],
            phase="latex_tools_check",
        )

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

    # ==================== Research Phase ====================

    def _should_run_research_phase(self) -> bool:
        """Check if the Research Phase should run.

        Returns True if any sub-step still needs to run:
        - idea.md missing (proposal not analyzed yet)
        - deep_research.md missing (Gemini hasn't run yet)
        - project_context.md missing (specialization not done yet)
        """
        if self.config.get("skip_deep_research", False):
            return False

        idea_done = (self.state_dir / "idea.md").exists()
        dr_done = (self.state_dir / "deep_research.md").exists()
        ctx_done = (self.state_dir / "project_context.md").exists()

        if idea_done and dr_done and ctx_done:
            return False

        return True

    def _run_research_phase(self):
        """Run the Research Phase: understand project, gather background, specialize.

        All sub-steps are idempotent — each checks if its output exists and skips if so.

        Step 0: Setup
            Provision per-project conda env at <project_dir>/.env (clones ark-base).
            Idempotent: skipped if .env already exists.

        Step 1: Analyze Proposal
            researcher reads uploaded PDF / idea → idea.md (including a
            suggested title) + deep research query. Title is parsed and
            committed to config.yaml + DB immediately after this step so
            Deep Research and Telegram UX have a real title.

        Step 2: Deep Research
            Gemini Deep Research API → deep_research.md → PDF sent to user via Telegram

        Step 3: Specialization
            researcher reads idea.md + deep_research.md →
            3.1 generate project_context.md (web-verified)
            3.2 specialize agent prompts (template + project knowledge → agents/ dir)
            3.3 select skills from library

        Step 4: Bootstrap
            4.1 install builtin skills
            4.2 bootstrap citations → references.bib
        """
        self._sync_db(phase="research")
        self.log("", "RAW")
        self.log_section("Research Phase  |  Understanding Project & Building Foundation")

        if self.telegram.is_configured:
            self.telegram.send(
                f"<b>🔬 {self.display_name}</b>\nResearch Phase — analyzing proposal & building foundation...",
                parse_mode="HTML",
            )

        # ── Step 0: Setup (conda env provisioning) ──────────────────────
        self.log_step_header(0, 4, "Setup")
        try:
            from website.dashboard.jobs import provision_project_env, project_env_ready
            if not project_env_ready(self.code_dir):
                base_env = self.config.get("base_conda_env", "ark-base")
                self.log_step(f"Provisioning conda environment (cloning {base_env})...", "progress")
                success, msg = provision_project_env(self.code_dir, base_env)
                if success:
                    self.log_step(f"Conda env ready: {msg}", "success")
                else:
                    # Hard fail: the whole pipeline depends on this env for
                    # experiments. Surface the error; caller will mark failed.
                    self.log_step(f"Conda env provisioning failed: {msg}", "error")
                    raise RuntimeError(f"Conda env provisioning failed: {msg}")
            else:
                self.log_step("Conda env already exists", "success")
        except ImportError as e:
            self.log(f"Conda env provisioning skipped (webapp.jobs unavailable): {e}", "WARN")
        self.log_step_header(0, 4, "Setup", "end")

        # ── Step 1: Analyze Proposal ────────────────────────────────────
        idea_file = self.state_dir / "idea.md"
        dr_query = None  # Will be set by researcher output

        if not idea_file.exists():
            self.log_step_header(1, 4, "Analyze Proposal")

            uploaded_pdf = self.config.get("uploaded_pdf", "")
            if uploaded_pdf and Path(uploaded_pdf).exists():
                source_instruction = f"Read the uploaded PDF at `{uploaded_pdf}` carefully."
            else:
                source_instruction = (
                    f"The research idea is provided below:\n\n"
                    f"{self._research_idea}"
                )

            venue = self.config.get("venue", "")
            venue_pages = self.config.get("venue_pages", "")

            dr_query = self.run_agent("researcher", f"""
Analyze the project proposal and produce two outputs.

## Source Material
{source_instruction}

## Target Venue
{venue} ({venue_pages} pages body text)

## Output 1: idea.md
Write the file `auto_research/state/idea.md` with these sections:

### Research Summary
A clear 2-3 paragraph summary: what problem is addressed, what the authors propose,
and what contributions are expected.

### External Systems & Platforms
List EVERY external system, platform, tool, framework, or dataset mentioned.
For each one: what it is, how it is used in this research, any details mentioned.

### Proposed Methodology
What experiments do the authors plan? What data? What metrics? What baselines?

## Output 2: Deep Research Query
After writing idea.md, output a focused deep research query for Gemini.
The query should:
- Summarize the research topic for a literature search engine
- Ask 5-8 specific questions about related work, baselines, benchmarks
- Ask about the external systems mentioned (what they are, how to install them, alternatives)
- Ask about concrete experimental methodology for this type of research
- Request a section on "Required Systems & Setup" with install instructions

Output the query as plain text at the END of your response, after a line that says
"DEEP_RESEARCH_QUERY:" — everything after that line is the query.

Be thorough and faithful to the proposal.
""", timeout=600)

            self.log_step_header(1, 4, "Analyze Proposal", "end")
        else:
            self.log_step("idea.md exists, skipping proposal analysis", "info")

        # Generate title from idea.md via dedicated LLM call (validated + retry).
        self._update_title_from_idea()

        # ── Step 2: Deep Research ───────────────────────────────────────
        dr_file = self.state_dir / "deep_research.md"
        if not dr_file.exists():
            self.log_step_header(2, 4, "Deep Research (Gemini)")

            from ark.deep_research import run_deep_research, get_gemini_api_key
            api_key = self.config.get("gemini_api_key", "") or get_gemini_api_key()

            if api_key:
                # Extract query from researcher output, or build from idea.md
                query = None
                if dr_query and "DEEP_RESEARCH_QUERY:" in dr_query:
                    query = dr_query.split("DEEP_RESEARCH_QUERY:", 1)[1].strip()

                if not query and idea_file.exists():
                    # Build query from idea.md content
                    idea_content = idea_file.read_text()
                    title = self.config.get("title", "")
                    venue = self.config.get("venue", "")
                    query = (
                        f"I am writing an academic paper titled \"{title}\" targeting {venue}.\n\n"
                        f"Research summary:\n{idea_content[:6000]}\n\n"
                        "Please conduct comprehensive research. I need:\n"
                        "1. Literature review of relevant recent papers (2022-2026)\n"
                        "2. State-of-the-art approaches, benchmarks, and baselines\n"
                        "3. Key technical challenges and open problems\n"
                        "4. External systems/tools this research depends on, with install instructions\n"
                        "5. Concrete experimental methodology and evaluation metrics\n"
                        "6. API keys or credentials needed\n\n"
                        "Include a '## Required Systems & Setup' section."
                    )

                try:
                    result = run_deep_research(
                        config=self.config,
                        output_dir=self.state_dir,
                        api_key=api_key,
                        custom_query=query,
                    )
                    if result:
                        self.log(f"Deep Research completed: {result}", "INFO")
                        self._send_deep_research_telegram(result)
                    else:
                        self.log("Deep Research returned no result.", "WARN")
                except Exception as e:
                    self.log(f"Deep Research failed: {e}", "WARN")
            else:
                self.log("No Gemini API key — skipping Deep Research", "WARN")

            self.log_step_header(2, 4, "Deep Research (Gemini)", "end")
        else:
            self.log_step("Deep research report exists, skipping", "info")

        # ── Step 3: Specialization ──────────────────────────────────────
        ctx_file = self.state_dir / "project_context.md"
        if not ctx_file.exists():
            self.log_step_header(3, 4, "Specialization")

            idea_content = idea_file.read_text()[:8000] if idea_file.exists() else ""
            dr_content = dr_file.read_text()[:12000] if dr_file.exists() else ""

            # 3.1: Generate project_context.md (web-verified)
            self.log_step("Generating project context (web-verified)...", "progress")
            self.run_agent("researcher", f"""
Read the idea summary and deep research report, then generate a verified
project context document.

## idea.md
{idea_content}

## Deep Research Report
{dr_content}

## Your Task

For EACH external system mentioned, you MUST search the web to verify:
- What it actually is (do NOT guess from name)
- Official URL and repository
- Correct install command (MUST be project-isolated — never global installs)
- Key CLI commands or API usage for experiments

Write `auto_research/state/project_context.md` with sections:
## External Systems, ## Environment Setup, ## Experiment Guidance, ## Credentials & Access
""", timeout=600)
            self.log_step("Project context generated", "success")

            # 3.2: Specialize agent prompts (code-driven, one call per agent)
            self.log_step("Specializing agent prompts...", "progress")
            self._specialize_agent_prompts(idea_content, dr_content)

            # 3.3: Select and install skills
            self.log_step("Selecting skills...", "progress")
            skills_index = self._load_skills_index()
            ctx_content_for_skills = ctx_file.read_text()[:4000] if ctx_file.exists() else ""
            if skills_index and "No skills" not in skills_index:
                self.run_agent("researcher", f"""
Select skills from the library that match techniques this project will IMPLEMENT.

## Selection Rules
- Only select skills for methods/tools the project will BUILD or RUN code for
- Do NOT select skills just because a topic is MENTIONED as a benchmark or baseline
  Example: if the project EVALUATES on RL environments but does NOT train RL agents,
  do NOT select RL training skills
- Select 1-5 skills. Zero is acceptable if nothing matches well.
- When in doubt, leave it out — a wrong skill pollutes the agent context

## Project Context (verified)
{ctx_content_for_skills}

## Research Idea
{idea_content[:2000]}

## Skills Library
{skills_index[:8000]}

Write `auto_research/state/selected_skills.json` containing a JSON array of
selected skill paths (or an empty array `[]` if nothing matches).
""", timeout=300)
            self._install_selected_skills()
            self.log_step("Specialization complete", "success")

            self.log_step_header(3, 4, "Specialization", "end")
        else:
            self.log_step("Project context exists, skipping specialization", "info")

        # ── Step 4: Bootstrap ───────────────────────────────────────────
        self.log_step_header(4, 4, "Bootstrap")

        # 4.1: Install builtin skills (auto-inherited by all projects)
        self._install_builtin_skills()

        # 4.2: Bootstrap citations
        self._bootstrap_citations_from_deep_research()

        self.log_step_header(4, 4, "Bootstrap", "end")

        self.log_section("Research Phase Complete")

    def _load_skills_index(self) -> str:
        """Load the skills library index as a compact string for the researcher."""
        import json
        index_path = Path(__file__).parent.parent / "skills" / "index.json"
        if not index_path.exists():
            return "No skills library available."
        try:
            with open(index_path) as f:
                skills = json.load(f)
            lines = []
            for s in skills:
                tags = ", ".join(s.get("tags", [])[:3])
                lines.append(f"- {s['name']}: {s['description'][:80]} [{tags}] @ {s['path']}")
            return "\n".join(lines)
        except Exception:
            return "Skills index could not be loaded."

    def _install_selected_skills(self):
        """Copy selected skills to the project directory."""
        import json
        selected_file = self.state_dir / "selected_skills.json"
        if not selected_file.exists():
            return

        try:
            with open(selected_file) as f:
                selected_paths = json.load(f)

            if not isinstance(selected_paths, list):
                return

            skills_dest = Path(self.code_dir) / ".claude" / "skills"
            skills_dest.mkdir(parents=True, exist_ok=True)

            installed = []
            for skill_path in selected_paths:
                src = Path(skill_path)
                if src.exists() and (src / "SKILL.md").exists():
                    dest = skills_dest / src.name
                    if not dest.exists():
                        import shutil
                        shutil.copytree(src, dest)
                        installed.append(src.name)

            if installed:
                self.log_step(f"Installed {len(installed)} skills: {', '.join(installed)}", "success")
        except Exception as e:
            self.log(f"Skills installation failed: {e}", "WARN")

    def _install_builtin_skills(self):
        """Copy ARK builtin skills to the project's .claude/skills/ directory."""
        import shutil
        builtin_dir = Path(__file__).parent.parent / "skills" / "builtin"
        if not builtin_dir.exists():
            return

        dest_dir = Path(self.code_dir) / ".claude" / "skills"
        dest_dir.mkdir(parents=True, exist_ok=True)

        installed = []
        for skill_dir in sorted(builtin_dir.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                dest = dest_dir / skill_dir.name
                if not dest.exists():
                    shutil.copytree(skill_dir, dest)
                    installed.append(skill_dir.name)

        if installed:
            self.log_step(f"Builtin skills installed: {', '.join(installed)}", "success")

    def _check_human_intervention(self, stage: str = "") -> bool:
        """Check if an agent requested human intervention via needs_human.json.

        If the file exists and urgency is "blocking", sends a Telegram notification
        and blocks until the user responds. Returns True if human responded (caller
        should re-run the blocked work), False otherwise.
        """
        import json
        needs_file = Path(self.code_dir) / "results" / "needs_human.json"
        if not needs_file.exists():
            return False

        try:
            with open(needs_file) as f:
                request = json.load(f)
        except Exception:
            return False

        summary = request.get("summary", "Agent needs help")
        details = request.get("details", "")
        urgency = request.get("urgency", "blocking")
        timeout_min = request.get("timeout_minutes", 60)
        needed_items = request.get("needed_items", [])
        commands_tried = request.get("commands_tried", [])
        error_output = request.get("error_output", "")

        self.log(f"Human intervention requested [{urgency}]: {summary}", "WARN")

        # Build Telegram message
        items_text = ""
        if needed_items:
            items_text = "\n".join(
                f"  • <code>{item.get('key', '?')}</code>: {item.get('purpose', '')}"
                for item in needed_items
            )

        tg_msg = (
            f"🚨 <b>{self.display_name}</b> — blocked, needs help\n\n"
            f"<b>Stage:</b> {stage}\n"
            f"<b>Issue:</b> {summary}\n"
        )
        if items_text:
            tg_msg += f"\n<b>Needed:</b>\n{items_text}\n"
        if commands_tried:
            tg_msg += f"\n<b>Commands tried:</b>\n"
            for cmd in commands_tried[:5]:
                tg_msg += f"  <code>{cmd[:80]}</code>\n"
        if error_output:
            tg_msg += f"\n<b>Error:</b>\n<pre>{error_output[:300]}</pre>\n"
        if details:
            tg_msg += f"\n{details[:400]}\n"
        tg_msg += f"\n⏳ Waiting up to {timeout_min} min for your reply..."

        reply = None
        if self.telegram.is_configured:
            reply = self.telegram.ask(tg_msg, timeout=timeout_min * 60)

            if reply:
                # Save user response
                response_file = Path(self.code_dir) / "results" / "human_response.json"
                with open(response_file, "w") as f:
                    json.dump({"reply": reply, "stage": stage,
                               "original_request": summary}, f, indent=2)
                self.inject_user_update(reply)
                self.log(f"User responded: {reply[:100]}", "INFO")
            else:
                self.log(f"No response after {timeout_min}min — experiments remain blocked.", "WARN")
        else:
            self.log(f"Telegram not configured — experiments remain blocked.", "WARN")

        # Remove the request file so it doesn't trigger again
        needs_file.unlink(missing_ok=True)
        return reply is not None

    def _specialize_agent_prompts(self, idea_content: str, dr_content: str):
        """Specialize each agent's prompt with project-specific knowledge.

        For each agent (except researcher itself), calls the researcher
        to generate a '## Project-Specific Knowledge' section, then appends it to
        the agent's prompt file. Verifies the append succeeded.
        """
        ctx_file = self.state_dir / "project_context.md"
        ctx_content = ctx_file.read_text()[:6000] if ctx_file.exists() else ""

        # What knowledge each agent should receive
        agent_focus = {
            "experimenter": "install commands, environment setup, what experiments to run, how to use the target systems, isolation requirements",
            "planner": "experiment directions, system capabilities, what baselines to compare, what datasets exist, how to analyze results",
            "reviewer": "domain-specific review criteria, what integrity checks matter, common pitfalls in this field",
            "writer": "key terminology, contribution framing, related work positioning, anonymity requirements",
            "coder": "relevant frameworks, libraries, and coding patterns for this domain",
        }

        # Use the same agents_dir that run_agent() uses
        agents_dir = getattr(self, 'agents_dir', None)
        if not agents_dir or not agents_dir.exists():
            self.log("Agents directory not found, skipping prompt specialization", "WARN")
            return

        specialized_count = 0
        for agent_name, focus in agent_focus.items():
            prompt_file = agents_dir / f"{agent_name}.prompt"
            if not prompt_file.exists():
                self.log(f"  Agent prompt missing: {prompt_file}, cannot specialize", "WARN")
                continue

            current_prompt = prompt_file.read_text()
            # Skip if already specialized
            if "## Project-Specific Knowledge" in current_prompt:
                specialized_count += 1
                continue

            # Ask researcher to generate the specialization section
            result = self.run_agent("researcher", f"""
Generate a "## Project-Specific Knowledge" section for the {agent_name} agent.

This section will be appended to the agent's prompt to give it domain expertise
for this specific project.

## Project Context
{ctx_content[:4000]}

## Focus Areas for {agent_name}
{focus}

## Rules
- Output ONLY the "## Project-Specific Knowledge" section content (with the heading)
- Be concise but comprehensive (200-400 words)
- Include specific tool names, commands, URLs, and technical details
- For experimenter: emphasize project-isolated installs and checking existing services
- For writer: include anonymity rules (no author names in title or text for blind review)
- Do NOT repeat generic instructions already in the agent's base prompt
""", timeout=300)

            if result and len(result.strip()) > 50:
                # Append to prompt file
                with open(prompt_file, "a") as f:
                    f.write(f"\n\n{result.strip()}\n")
                specialized_count += 1
                self.log(f"  Specialized {agent_name} prompt ({len(result)} chars)", "INFO")
            else:
                self.log(f"  Failed to specialize {agent_name} (empty result)", "WARN")

        self.log_step(f"Specialized {specialized_count}/{len(agent_focus)} agent prompts", "success")

    def _update_title_from_idea(self):
        """Generate a title from idea.md via LLM and commit it.

        Uses ``claude -p`` with a tightly constrained prompt to generate
        the title, validates the output, retries on failure, and falls back
        to deterministic text extraction as a last resort.  The title is
        guaranteed to be non-empty after this method completes (or it raises).
        """
        idea_file = self.state_dir / "idea.md"
        if not idea_file.exists():
            self.log("idea.md not found — cannot generate title", "WARN")
            return

        current = (self.config.get("title") or "").strip()
        is_placeholder = (
            not current
            or len(current) < 4
            or re.fullmatch(r"[0-9a-fA-F-]{30,}", current) is not None
        )
        if not is_placeholder:
            return

        idea_text = idea_file.read_text().strip()
        if not idea_text:
            self.log("idea.md is empty — cannot generate title", "WARN")
            return

        # --- Attempt: LLM call with validation + retry ---
        new_title = ""
        for attempt in range(1, _TITLE_MAX_RETRIES + 1):
            candidate = _generate_title_via_llm(idea_text)
            if _validate_title(candidate):
                new_title = candidate
                self.log(f"Title generated via LLM (attempt {attempt}): {new_title}", "INFO")
                break
            self.log(
                f"Title generation attempt {attempt}/{_TITLE_MAX_RETRIES} failed "
                f"(got: {candidate!r})", "WARN"
            )

        # --- Fallback: deterministic extraction ---
        if not new_title:
            new_title = _fallback_title_from_idea(idea_text)
            self.log(f"Title fallback from idea.md text: {new_title}", "WARN")

        # --- Commit to config.yaml + DB ---
        self.config["title"] = new_title
        config_path = self.code_dir / "config.yaml"
        if config_path.exists():
            cfg = yaml.safe_load(config_path.read_text()) or {}
            cfg["title"] = new_title
            config_path.write_text(
                yaml.dump(cfg, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            )
        self._sync_db(title=new_title, name=new_title)
        self.log(f"Title committed: {new_title}", "INFO")

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
        # Sync to DB
        dev_status = state.get("status", "pending")
        phase = "dev" if dev_status == "in_progress" else ("review" if dev_status in ("completed", "complete") else "")
        self._sync_db(
            dev_iteration=int(state.get("iteration", 0)),
            dev_status=dev_status,
            phase=phase,
        )

    def _run_dev_phase(self):
        """Run the Dev Phase: iterative experiments → initial paper draft.

        Steps:
          1. Plan experiments (planner)
          2. Run experiments (experimenter + compute)
          3. Analyze results (researcher)
          4. Evaluate completeness (planner) → loop if insufficient
          5. Generate figures (matplotlib + AI concept)
          6. Write initial draft (writer)
          7. Deliver (compile, verify, notify)
        """
        max_dev_iters = self.config.get("max_dev_iterations", 3)
        dev_state = self._load_dev_phase_state()
        start_iter = dev_state.get("iteration", 0)

        self.log("", "RAW")
        self.log_section(f"Dev Phase  |  Building experiments & data  |  max {max_dev_iters} iterations")
        self._send_dev_phase_telegram("start", 0, max_dev_iters)

        research_idea = self._research_idea

        # Steps 1-4: Iterative experiment loop
        self._run_experiment_loop(dev_state, start_iter, max_dev_iters, research_idea)

        # Steps 5-7: Generate figures, write draft, deliver
        self.log("", "RAW")
        self.log_section("✏️ Writing Initial Paper Draft")
        self._send_dev_phase_telegram("writing", 0, 0)

        self._generate_all_figures()
        self._write_initial_draft(research_idea)
        self._deliver_dev_phase(dev_state, max_dev_iters)

    def _run_experiment_loop(self, dev_state: dict, start_iter: int,
                             max_dev_iters: int, research_idea: str):
        """Steps 1-4: Iterative experiment planning, execution, analysis, and evaluation.

        Loops until experiments are sufficient or max iterations reached.
        """
        deep_research_file = self.state_dir / "deep_research.md"
        deep_research_ctx = ""
        if deep_research_file.exists():
            deep_research_ctx = deep_research_file.read_text()[:8000]

        findings_summary = self._load_findings_summary()

        for dev_iter in range(start_iter + 1, max_dev_iters + 1):
            dev_state["iteration"] = dev_iter
            dev_state["status"] = "in_progress"
            self._save_dev_phase_state(dev_state)

            self.log("", "RAW")
            self.log_section(f"Dev Phase: Iteration {dev_iter}/{max_dev_iters}")
            self._send_dev_phase_telegram("iteration", dev_iter, max_dev_iters)

            # Step 1: Plan experiments
            plan_output = self._plan_experiments(dev_iter, max_dev_iters,
                                                  research_idea, deep_research_ctx,
                                                  findings_summary)

            # Step 2: Run experiments
            exp_output = self._run_experiments(dev_iter, max_dev_iters, plan_output)

            # Step 3: Analyze results
            research_output = self._analyze_results(exp_output)

            # Step 4: Evaluate completeness
            findings_summary = self._load_findings_summary()
            sufficient = self._evaluate_completeness(research_idea, findings_summary,
                                                      research_output)

            if sufficient:
                self.log_step("Experiments sufficient, proceeding to initial draft", "success")
                break

            self.log_step(f"Dev iter {dev_iter}: more experiments needed", "warning")

    def _plan_experiments(self, dev_iter: int, max_dev_iters: int,
                          research_idea: str, deep_research_ctx: str,
                          findings_summary: str) -> str:
        """Step 1: Plan experiments using planner agent."""
        self.log_step_header(1, 4, "Plan Experiments")
        venue_pages = int(self.config.get("venue_pages", 9) or 9)
        # Page-aware experiment budget. A 1-page workshop poster doesn't need
        # 8 experiments; a full conference paper does. Cap accordingly so the
        # experimenter agent can finish within its timeout budget.
        if venue_pages <= 2:
            max_exps = 1
            scope_note = ("This is a very short paper ({}p). Plan exactly ONE focused, fast "
                          "experiment that can run in under 5 minutes. Use small parameter "
                          "sweeps and small datasets.").format(venue_pages)
        elif venue_pages <= 4:
            max_exps = 2
            scope_note = ("This is a short paper ({}p). Plan AT MOST 2 experiments, each "
                          "expected to run in under 10 minutes.").format(venue_pages)
        elif venue_pages <= 6:
            max_exps = 3
            scope_note = ("This is a short paper ({}p). Plan AT MOST 3 experiments.").format(venue_pages)
        else:
            max_exps = 5
            scope_note = "Plan a comprehensive set of AT MOST 5 experiments."
        output = self.run_agent("planner", f"""
You are planning experiments for a research project. This is Dev Phase iteration {dev_iter}/{max_dev_iters}.

## Research Idea
{research_idea}

## Deep Research Context
{deep_research_ctx[:6000] if deep_research_ctx else "No deep research available yet."}

## Project-Specific Context
Read auto_research/state/project_context.md for verified information about what
external systems this project requires and how to install them. Use this as your
primary source for the required_systems section — do not re-derive from deep research.

## Current Findings
{findings_summary if findings_summary else "No experiments run yet."}

## Scope & Budget
{scope_note}
You MUST plan no more than {max_exps} experiments total. Pick the minimum
set that demonstrates the core idea — favor running 1 well-designed
experiment over many shallow ones.

## Task
Design a focused experiment plan ({max_exps} experiments max):
1. First, identify what external systems, tools, libraries, or datasets the project requires based on the research idea and deep research context. These are tools that must be INSTALLED and USED — not re-implemented from scratch.
2. What experiments to run (with specific scripts, parameters, baselines)
3. What metrics to measure
4. What baselines to compare against
5. Expected outcomes

Save the experiment plan to auto_research/state/experiment_plan.yaml with format:
```yaml
# Systems that must be installed before experiments can run.
# The experimenter will install these first and verify they work.
# Only list external packages/tools the project DEPENDS ON — do not list
# standard libraries (numpy, pandas, etc.) or tools the experimenter writes.
required_systems:
  - name: "human-readable name"
    why: "why this system is needed for the experiments"
    install_hint: "pip install X, or conda install X, or git clone URL"
    verify: "python -c 'import X; print(X.__version__)'"

experiments:
  - id: "exp1"
    title: "Experiment title"
    description: "What to test"
    script: "path/to/script.py"
    parameters: "key params"
    metrics: ["metric1", "metric2"]
    baseline: "comparison baseline"
```

IMPORTANT: If the research idea describes a specific platform, framework, or system (e.g., "evaluate on OpenClaw", "benchmark on MLPerf"), you MUST list it under required_systems. The experimenter is NOT allowed to re-implement these from scratch — they must install and use the real thing. If you are unsure how to install something, write your best guess for install_hint and the experimenter will search online for the correct method.
""", timeout=1200)
        self.log_step_header(1, 4, "Plan Experiments", "end")
        return output

    def _run_experiments(self, dev_iter: int, max_dev_iters: int,
                         plan_output: str) -> str:
        """Step 2: Run experiments using experimenter agent + compute backend."""
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
{plan_output[:4000] if plan_output else "See experiment_plan.yaml"}

{compute_instructions}

## MANDATORY: Environment Setup First

Before writing ANY experiment scripts, you must:

1. Read the experiment plan's `required_systems` section
2. For EACH required system:
   a. **First, search the web** for the system's official website, GitHub repo, and
      installation instructions. Do NOT blindly trust the install_hint — verify it
      by searching online. The planner may have guessed wrong about what the system
      is or how to install it.
   b. Once you know the correct package name and install method, install it.
      Try ALL available methods if the first one fails:
      - pip install
      - npm install -g (for Node.js tools)
      - conda install
      - git clone + install from source
      - Docker (if available)
   c. You MUST try at least 2-3 different install methods before declaring failure.
      "Heavy dependency chain" or "takes too long" is NOT a valid reason to skip.
   d. Run the verify command to confirm it works
   e. Only if ALL install methods fail, write a failure report to results/setup_failure.json
3. Save the setup results to results/environment_setup.json:
   ```json
   {{"systems": [{{"name": "...", "installed": true, "version": "...", "verify_passed": true}}]}}
   ```
4. ONLY after all required systems are verified, proceed to write experiment scripts

## Critical Rule: Use Real Libraries

Your experiment scripts MUST import and use the installed required_systems packages.
Do NOT re-implement the target system from scratch. For example:
- If the plan says "required: Open WebUI" → install it (`pip install open-webui`) and use its API
- If the plan says "required: mlperf" → use the actual mlperf harness
- Writing your own substitute class instead of using the real package is NOT acceptable

If a required system cannot be installed after trying all methods, report failure honestly.
Do NOT build a "workaround" or "standalone mode" — the experiment either runs on the real
system or it fails with a clear report of what is needed.

## Other Requirements
- Write and submit ALL experiment scripts at once
- Each script should save results to results/ directory
- Use clear naming: results/exp1_results.json, results/exp2_results.json, etc.
- Handle errors gracefully (log failures, continue with remaining experiments)
- Keep experiments small enough to finish within the agent budget
""", timeout=7200)

            self.log_step("Waiting for all experiments to complete...", "progress")
            self._compute_backend.wait_for_completion(max_wait_hours=4)
            self._compute_backend.collect_results()
        finally:
            self._compute_backend.teardown()

        # Check if experimenter requested human intervention
        self._check_human_intervention(stage="Run Experiments")

        self.log_step_header(2, 4, "Run Experiments", "end")
        return exp_output

    def _analyze_results(self, exp_output: str) -> str:
        """Step 3: Analyze experiment results using planner agent."""
        self.log_step_header(3, 4, "Analyze Results")
        output = self.run_agent("planner", f"""
Analyze ALL experiment results from this dev iteration.

## What was run
{exp_output[:4000] if exp_output else "Check results/ directory for new files."}

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
        return output

    def _evaluate_completeness(self, research_idea: str, findings_summary: str,
                                research_output: str) -> bool:
        """Step 4: Evaluate if experiments are sufficient to write paper."""
        self.log_step_header(4, 4, "Evaluate Completeness")

        eval_output = self.run_agent("planner", f"""
Evaluate whether we have sufficient experimental data for the paper.

## Research Idea
{research_idea}

## Current Findings
{findings_summary}

## Results Analysis
{research_output[:4000] if research_output else "No analysis available."}

## Task
Determine if the experiments are sufficient to write a complete paper:
1. Do we have data for ALL major claims?
2. Are baselines properly compared?
3. Are the results statistically significant?
4. Are there obvious gaps that need more experiments?
5. Read `auto_research/state/project_context.md` and check: were ALL external systems
   listed there actually installed, configured, and used in experiments? If any system
   was listed but never used (e.g., never started, never called its API, never imported
   its package), that is a critical gap.
6. Check `results/environment_setup.json` and `results/credentials_needed.json` — are
   there any systems marked as "blocked" or credentials still missing? Those represent
   incomplete experiments.

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
        sufficient = False
        try:
            json_match = re.search(r'\{[^{}]*"sufficient"[^{}]*\}', eval_output, re.DOTALL)
            if json_match:
                eval_json = json.loads(json_match.group())
                sufficient = eval_json.get("sufficient", False)
            else:
                sufficient = '"sufficient": true' in eval_output.lower()
        except Exception:
            sufficient = "sufficient.*true" in eval_output.lower()

        return sufficient

    def _generate_all_figures(self):
        """Generate all figures: geometry config, matplotlib plots, AI concept figures.

        Must run before _write_initial_draft() so writer knows which figures are available.
        """
        # Generate figure_config.json with correct venue geometry
        self._generate_figure_config()

        # Create plotting script from experiment results
        self._create_plotting_script_if_needed()

        # Generate matplotlib figures
        self.log_step("Generating statistical figures from experiment results...", "progress")
        self.generate_figures()

        # Generate AI concept figures
        if self.config.get("figure_generation") == "nano_banana":
            self.log_step("Generating AI concept figures (PaperBanana)...", "progress")
            n = self._generate_nano_banana_figures()
            if n == 0:
                self.log("No concept figures were generated", "WARN")

    def _write_initial_draft(self, research_idea: str):
        """Write the initial paper draft using writer agent.

        Assumes all figures are already generated (call _generate_all_figures first).
        """
        figure_list = self._list_available_figures()

        paper_requirements = self.load_paper_requirements()
        req_summary = yaml.dump(paper_requirements, allow_unicode=True) if paper_requirements else "No special requirements"
        findings_summary = self._load_findings_summary()

        venue_pages = self.config.get('venue_pages', 9)
        latex_dir = self.config.get('latex_dir', 'paper')
        figures_dir = self.config.get('figures_dir', 'paper/figures')

        base_prompt = self.config.get("initial_paper_writing_prompt", "")
        if base_prompt:
            prompt = base_prompt.replace("{req_summary}", req_summary)
            prompt += f"\n\n## Experiment Findings\n{findings_summary}"
            prompt += f"\n\n## Available Figures (already generated)\n{figure_list}"
        else:
            prompt = f"""Write a COMPLETE, SUBMISSION-READY research paper draft.

## Research Idea
{research_idea}

## Experiment Findings
{findings_summary}

## Paper Requirements
{req_summary}

## Available Figures (already generated — DO NOT recreate these)
{figure_list}

**CRITICAL**: The figures above are already generated. Use \\includegraphics to include them.
- AI concept figures (marked as "AI concept") must NOT be recreated as TikZ or matplotlib.
- Statistical plots (marked as "matplotlib") are already generated from experiment data.
- Use the EXACT filenames listed above in your \\includegraphics commands.
- For multi-column templates, use \\begin{{figure*}} for wide concept figures, \\begin{{figure}} for single plots.

## MANDATORY — every item below is required, NO exceptions:

### 1. All sections must be fully written (zero placeholders)
- Abstract (150-250 words): problem, method, key results with actual numbers
- Introduction: motivation, gap, 3-5 numbered contributions, paper roadmap
- Related Work: 3-4 subsections, at least 10 cited works, explain how we differ
- Method: full technical description, equations where appropriate
- Experiments: setup table, baselines listed, main results table with numbers, ablation
- Analysis/Discussion: explain WHY results are good/bad, failure cases
- Conclusion: 1 paragraph summary + 1 paragraph future work

### 2. Appendix policy (use `\\appendix` only when content genuinely belongs there)
- Belongs in appendix: full proofs/derivations, extended ablation tables, hyperparameter sweeps, prompt templates, implementation/config details, additional qualitative examples, dataset statistics beyond a summary
- Belongs in body: problem, core method, headline results, primary ablation, key analysis
- The body-page limit excludes `\\appendix` — prefer appendix over cutting body when supplementary material is worth keeping
- Do NOT create an empty or single-paragraph appendix just to have one

### 3. Data integrity
- Every performance claim must use actual numbers from findings
- Include at least one \\begin{{table}} comparing against baselines
- No vague statements like "our method is better" — use exact percentages

### 4. Page target: {venue_pages} pages of body text
- The last page must be at least 90% filled
- Ensure `\\clearpage` before `\\bibliography{{...}}`

### 5. LaTeX mechanics
- Edit {latex_dir}/main.tex directly
- Verify compilation: cd {latex_dir} && pdflatex -interaction=nonstopmode main.tex
- All \\ref and \\cite must resolve

Produce the complete paper. Do not stop until all sections are written and it compiles.
"""

        self.run_agent("writer", prompt, timeout=3600)

    def _deliver_dev_phase(self, dev_state: dict, max_dev_iters: int):
        """Compile, verify, and deliver the dev phase draft.

        Handles: clearpage injection, compilation, page count, citations,
        Telegram notification, and marking dev phase as completed.
        """
        self._ensure_clearpage_before_bibliography()
        self.log_step("Compiling initial draft...", "progress")
        draft_compiled = self._compile_until_success(
            context=f"Dev Phase complete ({dev_state['iteration']} iterations)"
        )

        if draft_compiled:
            # Citation verification before page enforcement: fix bib entries
            # and clean unused refs so page count reflects final state.
            self._ensure_float_barrier()
            self.compile_latex()
            self._fix_overfull(context="dev-phase-delivery")
            self._run_citation_verification()
            try:
                self._enforce_page_count(context="dev-phase-delivery")
            except QuotaExhaustedError as e:
                wait_time = 1800
                self.log(f"Quota exhausted during dev phase page enforcement "
                         f"({e.page_count:.1f}/{e.venue_pages} pages), "
                         f"pausing {wait_time // 60}min before retry...", "ERROR")
                self.send_notification(
                    "Quota Exhausted",
                    f"Dev phase page enforcement failed "
                    f"({e.page_count:.1f}/{e.venue_pages} pages), "
                    f"pausing {wait_time // 60}min before retry",
                    priority="critical",
                )
                RateLimitCountdown(wait_time).run()
                self._quota_exhausted = False  # Reset for retry
                self._enforce_page_count(context="dev-phase-delivery-retry")

        if draft_compiled and self.telegram.is_configured:
            pdf_path = self.latex_dir / "main.pdf"
            if pdf_path.exists():
                ok = self.telegram.send_document(
                    pdf_path,
                    caption=f"📄 <b>Initial draft ready</b> — {self.display_name}\n"
                            f"Dev Phase complete ({dev_state['iteration']} iterations)\n"
                            f"Entering Review Phase now.",
                )
                if not ok:
                    self.telegram.send("📄 Initial draft compiled (PDF too large to send, download from portal)")

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

        Handles real reviewer formats:
            ### M1. Title
            ### M1: Title
            **M1**: Title
            - M1: Title
            M1: Title

        Args:
            review_output: Raw review markdown text.
            level: "major" for M-prefixed issues, "minor" for m-prefixed.

        Returns:
            List of (id, one_line_summary) tuples.
        """
        if not review_output:
            return []

        prefix = "M" if level == "major" else "m"
        # Allow leading `#` (markdown headers), `-`/`*` (list markers), `**`
        # (bold), then the ID, then `.` or `:` separators, then the title.
        pattern = rf'(?:^|\n)[#\s]*[-*]*\s*\**({prefix}\d+)\**[.:]?\**\s*(.+)'
        # `re.MULTILINE` so `^` matches each line. Case-insensitive so we
        # accept "m1" as well, then we normalize.
        matches = re.findall(pattern, review_output, re.IGNORECASE | re.MULTILINE)

        results = []
        seen = set()
        for issue_id, summary in matches:
            issue_id = issue_id.upper() if level == "major" else issue_id.lower()
            # For the minor level, skip anything that case-folds to an upper-M
            # match (since the pattern is case-insensitive by necessity).
            if level == "minor" and issue_id != issue_id.lower():
                continue
            if issue_id not in seen:
                seen.add(issue_id)
                # Trim to one line, max 100 chars
                summary = summary.strip().split("\n")[0][:100]
                # Strip trailing markdown/bold leftovers
                summary = summary.rstrip("*").strip()
                if summary:
                    results.append((issue_id, summary))
        return results

    def _extract_issue_details(self, review_output: str, ids: list,
                               level: str = "major", max_chars: int = 600) -> dict:
        """Extract the full multi-line description block for each requested issue.

        Returns {id: description_text}. The description is the text between the
        issue header and the next `### M\\d`, `---`, or top-level section
        (`## `), trimmed and capped at `max_chars` characters.
        """
        if not review_output or not ids:
            return {}

        prefix = "M" if level == "major" else "m"
        wanted = {iid.upper() if level == "major" else iid.lower() for iid in ids}

        # Find every header position
        header_pat = rf'(?:^|\n)[#\s]*[-*]*\s*\**({prefix}\d+)\**[.:]?\**\s*(.+)'
        header_re = re.compile(header_pat, re.IGNORECASE | re.MULTILINE)

        all_matches = list(header_re.finditer(review_output))
        if not all_matches:
            return {}

        # Patterns that mark the end of a description block
        end_pat = re.compile(r'\n\s*---\s*\n|\n##\s+|\n###\s*' + prefix + r'\d+',
                             re.IGNORECASE)

        out = {}
        for i, m in enumerate(all_matches):
            iid_raw = m.group(1)
            iid = iid_raw.upper() if level == "major" else iid_raw.lower()
            if level == "minor" and iid != iid.lower():
                continue
            if iid not in wanted or iid in out:
                continue

            start = m.end()  # body starts after the header line
            # Find the end of this issue's body
            tail = review_output[start:]
            stop_match = end_pat.search(tail)
            body = tail[: stop_match.start()] if stop_match else tail

            # Clean: collapse blank lines, strip leading/trailing whitespace,
            # remove markdown bold/italic markers for readability
            body = body.strip()
            body = re.sub(r'\n{3,}', '\n\n', body)
            body = re.sub(r'\*\*([^*]+)\*\*', r'\1', body)  # **bold** → bold
            body = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', body)  # *italic* → italic

            if len(body) > max_chars:
                body = body[: max_chars - 1].rstrip() + "…"
            out[iid] = body

        return out

    def _ids_referenced_in_options(self, options: list) -> list:
        """Pull issue IDs (M1, M2, m3, ...) referenced inside option labels."""
        ids = []
        for opt in options or []:
            for m in re.findall(r'\b([Mm]\d+)\b', opt or ""):
                if m not in ids:
                    ids.append(m)
        return ids

    def _build_decision_background(self, review_output: str, options: list,
                                    score: float = 0.0) -> list:
        """Background bullets for a decision prompt: score history, stagnation
        rule, repeat issues, and the FULL descriptions of any issues whose
        IDs are referenced in the option labels (so the user actually knows
        what M1/M2 mean instead of seeing bare IDs).
        """
        bg = []

        # Score history
        try:
            recent = [r.get("score", 0) for r in (self.load_paper_state().get("reviews") or [])[-6:]]
            if recent:
                bg.append("Score history: " + " → ".join(f"{s:.1f}" for s in recent))
        except Exception:
            pass

        # Stagnation, with the rule explained inline
        stag = getattr(self.memory, "stagnation_count", 0)
        if stag > 0:
            bg.append(
                f"Stagnation: {stag}/5 rounds without ≥0.3 score gain "
                f"(self-repair triggers at 5)."
            )

        # Repeating issues
        if hasattr(self.memory, "get_repeat_issues"):
            try:
                repeat = self.memory.get_repeat_issues(threshold=2) or []
                if repeat:
                    parts = ", ".join(f"{rid} (×{cnt})" for rid, cnt in repeat[:5])
                    bg.append(f"Repeating issues: {parts}")
            except Exception:
                pass

        # Full descriptions of any major issues referenced in the options
        ids = self._ids_referenced_in_options(options)
        major_ids = [i for i in ids if i.upper() == i and i.startswith("M")]
        if not major_ids:
            # No IDs in options — fall back to the top 2 majors so the user
            # at least sees the headline issues.
            top_majors = self._extract_issue_summaries(review_output, "major")[:2]
            major_ids = [iid for iid, _ in top_majors]

        if major_ids:
            details = self._extract_issue_details(
                review_output, major_ids, level="major", max_chars=400,
            )
            summaries = dict(self._extract_issue_summaries(review_output, "major"))
            # Cap how many full descriptions we attach so the message stays
            # under Telegram's 4096-char limit even after polish.
            for iid in major_ids[:2]:
                title = summaries.get(iid, "")
                body = details.get(iid, "")
                # Combine header + body into a single bullet entry. Use
                # plain-text decoration (no HTML tags) so the orchestrator's
                # html.escape() doesn't mangle it. A leading "▸" makes the
                # issue header stand out as a sub-section inside Background.
                header = f"▸ {iid}: {title}" if title else f"▸ {iid}"
                if body:
                    flat = " ".join(body.split())
                    bg.append(f"{header}\n   {flat}")
                else:
                    bg.append(header)

        return bg

    def _build_option_details(self, options: list, review_output: str) -> list:
        """Per-option detail strings shown under each numbered choice."""
        summaries = dict(self._extract_issue_summaries(review_output, "major"))
        details = []
        for opt in options or []:
            ids = re.findall(r'\b(M\d+)\b', opt or "")
            if ids:
                # First referenced issue → tell the user what will happen
                iid = ids[0]
                title = summaries.get(iid, "")
                if title:
                    details.append(
                        f"Spends the next iteration on {iid} ({title}). "
                        f"Other issues are deferred."
                    )
                else:
                    details.append(
                        f"Spends the next iteration on {iid}. Other issues deferred."
                    )
            elif "all" in (opt or "").lower() and "major" in (opt or "").lower():
                details.append(
                    "Tries to address every major issue in one iteration. "
                    "Risk of shallow fixes; works best when issues are small."
                )
            elif "different approach" in (opt or "").lower():
                details.append(
                    "Drops the previous strategy. The agent is forced to try a "
                    "new method (e.g., new experiment, new figure type)."
                )
            elif "custom" in (opt or "").lower():
                details.append(
                    "Free text — whatever you reply becomes the next directive "
                    "for the agent."
                )
            else:
                details.append("")
        return details

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
        background = self._build_decision_background(
            review_output, options, score=score,
        )
        idx, reply = self.ask_user_decision(
            question, options, timeout=900,
            what_happened=trigger,
            background=background,
            option_details=self._build_option_details(options, review_output),
            phase="smart_intervention",
        )
        self._asked_this_iteration = True
        if reply:
            self.log(f"User intervention reply: {reply[:200]}", "INFO")

    def _create_plotting_script_if_needed(self):
        """Create a matplotlib plotting script from experiment results if one doesn't exist.

        Uses the coder agent to read results/ and findings.yaml, then generate
        a create_paper_figures.py script with publication-quality statistical figures.
        """
        from pathlib import Path

        results_dir = self.code_dir / "results"
        script_rel = self.config.get("create_figures_script", "scripts/create_paper_figures.py")
        script_path = self.code_dir / script_rel
        figures_dir = self.config.get("figures_dir", "paper/figures")

        # Skip if script already exists or no results to plot
        if script_path.exists():
            self.log(f"Plotting script already exists: {script_rel}", "INFO")
            return
        if not results_dir.exists() or not any(results_dir.iterdir()):
            self.log("No experiment results found, skipping plotting script creation", "INFO")
            return

        self.log_step("Creating plotting script from experiment results...", "progress")

        # Gather context for the coder agent
        findings = self._load_findings_summary()
        result_files = sorted(
            f.name for f in results_dir.iterdir()
            if f.suffix in (".json", ".csv", ".txt") and f.stat().st_size > 0
        )
        if not result_files:
            self.log("No data files in results/, skipping", "INFO")
            return

        # Read a sample of result data for context
        data_samples = []
        for fname in result_files[:5]:
            fpath = results_dir / fname
            try:
                content = fpath.read_text()[:1000]
                data_samples.append(f"### {fname}\n```\n{content}\n```")
            except Exception:
                pass

        # Ensure script directory exists
        script_path.parent.mkdir(parents=True, exist_ok=True)

        self.run_agent("coder", f"""Create a Python plotting script that generates publication-quality
statistical figures from the experiment results in this project.

## Output
Save the script to: {script_rel}
The script must be self-contained and runnable with: python {script_rel}

## Data files in results/ directory:
{chr(10).join(f'- {f}' for f in result_files)}

## Sample data (first 1000 chars of each):
{chr(10).join(data_samples)}

## Experiment findings summary:
{findings[:3000]}

## CRITICAL: Figure Config (read this FIRST)
{figures_dir}/figure_config.json contains the EXACT dimensions from the LaTeX template.
You MUST load it and use its values. The config has this structure:
```json
{{
  "geometry": {{
    "columnwidth_in": 3.333,  // width for single-column figures
    "textwidth_in": 7.0,      // width for full-width figures
    "font_size_pt": 10        // base font size matching LaTeX body text
  }},
  "matplotlib_rcparams": {{ ... }},  // apply ALL of these via plt.rcParams.update()
  "sizes": {{
    "single_column": [3.333, 2.333],     // figsize for single-column figures
    "double_column": [7.0, 2.45]         // figsize for full-width figures
  }}
}}
```

Load it like this:
```python
with open('{figures_dir}/figure_config.json') as f:
    cfg = json.load(f)
plt.rcParams.update(cfg['matplotlib_rcparams'])
COL_W = cfg['geometry']['columnwidth_in']   # for single-column figures
TEXT_W = cfg['geometry']['textwidth_in']     # for full-width figures
```

Most statistical figures should use single_column size: figsize=(COL_W, COL_W*0.7).
Only use double_column for multi-panel figures (side-by-side subplots).

## Requirements:
1. Load figure_config.json as shown above — do NOT hardcode dimensions
2. Generate at least 2 statistical figures:
   a) Main results comparison (bar chart or horizontal bar chart)
   b) Ablation or analysis chart (grouped bars, line chart, or heatmap)
3. Save each figure as BOTH PDF and PNG to {figures_dir}/
4. Name figures descriptively: fig_main_results.pdf, fig_ablation.pdf, etc.

## Style Guide (MUST follow):
- Apply ALL rcParams from figure_config.json (font sizes match LaTeX template)
- Wong colorblind-safe palette: ['#0072B2', '#D55E00', '#009E73', '#CC79A7', '#E69F00', '#56B4E9']
- Add hatching patterns for bar charts (colorblind accessibility)
- Use horizontal bars when there are 5+ categories (avoids x-label overlap)
- constrained_layout=True on all figures
- DPI 300, sans-serif fonts exclusively
- No figure titles inside plots (LaTeX \\caption handles titles)
- Light dashed grid lines behind data
- Error bars with caps where applicable
- Bold font for "Ours" method labels
""", timeout=600)

        if script_path.exists():
            self.log_step(f"Plotting script created: {script_rel}", "success")
        else:
            self.log(f"Coder agent did not create {script_rel}", "WARN")

    def _list_available_figures(self) -> str:
        """List all figures in paper/figures/ with their type (AI concept vs matplotlib)."""
        if not self.figures_dir.exists():
            return "No figures generated yet."
        lines = []
        for f in sorted(self.figures_dir.iterdir()):
            if f.suffix not in (".png", ".pdf"):
                continue
            size_kb = f.stat().st_size // 1024
            # AI concept figures are typically >150KB (PaperBanana/Gemini output)
            fig_type = "AI concept diagram — DO NOT recreate" if size_kb > 150 else "matplotlib statistical plot"
            lines.append(f"- {f.name} ({size_kb}KB, {fig_type})")
        return "\n".join(lines) if lines else "No figures generated yet."

    def _send_dev_phase_telegram(self, event: str, current: int, total: int):
        """Send dev phase notifications to Telegram."""
        if not self.telegram.is_configured:
            return
        try:
            name = self.display_name
            if event == "start":
                self.telegram.send(f"<b>⚙️ {name}</b>\nDev Phase — max {total} iterations", parse_mode="HTML")
            elif event == "iteration":
                self.telegram.send(f"🔬 Dev {current}/{total}: Planning experiments...")
            elif event == "experiments":
                self.telegram.send(f"🧪 Dev {current}/{total}: Running experiments...")
            elif event == "writing":
                self.telegram.send(f"✏️ Dev done → Writing initial draft...")
            elif event == "complete":
                self.telegram.send(f"<b>✅ {name}</b> — Dev Phase Complete → Review", parse_mode="HTML")
        except Exception:
            pass

    def _write_cost_report(self):
        """Write per-agent and total cost/stats to cost_report.yaml.

        Called after every agent invocation so the webapp SSE stream can pick
        up live updates within ~2s. Writes atomically (.tmp + os.replace) so
        readers never see a partial file. Aggregates real token & USD fields
        when the claude JSON envelope was parsed; falls back to character
        counts otherwise.
        """
        if not self._agent_stats:
            return

        # Aggregate per agent type. Each bucket carries both legacy char-count
        # fields (for backwards compat with telegram_daemon / older tests) and
        # the new token + cost fields populated from claude JSON output.
        by_type = {}
        for stat in self._agent_stats:
            atype = stat["agent_type"]
            if atype not in by_type:
                by_type[atype] = {
                    "calls": 0,
                    "total_seconds": 0,
                    "total_prompt_len": 0,
                    "total_output_len": 0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cache_read_tokens": 0,
                    "total_cache_creation_tokens": 0,
                    "total_cost_usd": 0.0,
                }
            b = by_type[atype]
            b["calls"] += 1
            b["total_seconds"] += stat.get("elapsed_seconds", 0)
            b["total_prompt_len"] += stat.get("prompt_len", 0)
            b["total_output_len"] += stat.get("output_len", 0)
            b["total_input_tokens"] += stat.get("input_tokens", 0)
            b["total_output_tokens"] += stat.get("output_tokens", 0)
            b["total_cache_read_tokens"] += stat.get("cache_read_tokens", 0)
            b["total_cache_creation_tokens"] += stat.get("cache_creation_tokens", 0)
            b["total_cost_usd"] += float(stat.get("cost_usd", 0.0) or 0.0)

        total_calls = sum(d["calls"] for d in by_type.values())
        total_time = sum(d["total_seconds"] for d in by_type.values())
        total_cost_usd = sum(d["total_cost_usd"] for d in by_type.values())
        total_input_tokens = sum(d["total_input_tokens"] for d in by_type.values())
        total_output_tokens = sum(d["total_output_tokens"] for d in by_type.values())
        total_cache_read_tokens = sum(d["total_cache_read_tokens"] for d in by_type.values())
        total_cache_creation_tokens = sum(d["total_cache_creation_tokens"] for d in by_type.values())

        report = {
            "generated_at": datetime.now().isoformat(),
            "total_agent_calls": total_calls,
            "total_agent_seconds": total_time,
            "total_cost_usd": round(total_cost_usd, 6),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "per_agent": by_type,
            "raw_stats": self._agent_stats[-100:],  # Keep last 100 entries
        }

        report_path = self.state_dir / "cost_report.yaml"
        tmp_path = report_path.with_suffix(".yaml.tmp")
        try:
            with open(tmp_path, "w") as f:
                yaml.dump(report, f, default_flow_style=False, allow_unicode=True)
            os.replace(tmp_path, report_path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        # Sync cost totals to DB
        self._sync_db(
            total_cost_usd=round(total_cost_usd, 6),
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_agent_calls=total_calls,
        )

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

        # Research Phase: understand project, gather background, extract requirements
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
                    summary.append(f"{len(corrected)} corrected")
                if needs_check:
                    summary.append(f"{len(needs_check)} needs-check")
                if summary:
                    self.log_step(f"Citations: {', '.join(summary)}", "success")

            # 4. Enforce critical citations — if writer dropped a MUST CITE paper, add it back
            self._enforce_critical_citations(lit_path, tex_dir)

            # 5. NEEDS-CHECK markers stay in References only (via fix_bib note field).
            # Do NOT mark body text — inserting markers after \cite disrupts page count.

            # 6. Clean up unused entries
            removed = cleanup_unused(bib_str, tex_dir)
            if removed:
                self.log_step(f"Removed {len(removed)} unused citations", "success")

            # 7. Recompile if anything changed
            if (results and (corrected or needs_check)) or removed:
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
