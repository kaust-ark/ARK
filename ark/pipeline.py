"""PipelineMixin: main run loop, paper iteration, research iteration, dependency check."""
from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import yaml
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path
from ark.engines.cli import selected_agent_runtime
from ark.execution import (
    EXPERIMENT_RESUME_NOTE, QuotaExhaustedError, collect_after_jobs, settle_interrupted_experiments,
)
from ark.ui import RateLimitCountdown
from ark.latex import utils as latex_utils
from ark.workflow import ExperimentIteration, PaperIteration, ResearchDecision
# Re-bind public names from latex.utils into this module's namespace under
# their historical underscore-prefixed names. Two reasons:
#   1. ``_update_title_from_idea`` below was written when these helpers
#      lived in pipeline.py (under ``_<name>``). Keeping the prefixes
#      avoids a 3-site rewrite of the method.
#   2. Several tests patch via ``patch("ark.pipeline._generate_title_via_llm",
#      …)`` and ``ark.pipeline._validate_title``; the patch target must be
#      an attribute on the module, so we bind these aliases at module load.
from ark.latex.utils import (
    generate_title_via_llm as _generate_title_via_llm,
    validate_title as _validate_title,
    fallback_title_from_idea as _fallback_title_from_idea,
    _TITLE_MAX_RETRIES,
)
from ark.config import defaults


from ark.hitl import (
    _normalise_needs_human,
    _append_hitl_history,
    _update_hitl_decisions,
    _NONBLOCKING_URGENCIES,
)
from ark.quality_gate import extract_json


# Reports the experimenter task asks for before (or instead of) any experiment.
# They describe the environment, not an outcome, so they are never evidence.
_SETUP_REPORTS = frozenset({"environment_setup.json", "setup_failure.json",
                            "credentials_needed.json"})


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
        paper = PaperIteration(self)
        for step in (paper.compile, paper.review, paper.plan, paper.execute, paper.validate, paper.finalize):
            decision = step()
            if decision != "next":
                return decision == "repeat"

    def _iteration_prep(self):
        """Prepare for a new iteration: increment counters, reset flags, load instructions."""
        self.iteration += 1
        self._iteration_start = datetime.now()
        self._quota_exhausted = False
        self._terminal_error = None
        self._asked_this_iteration = False

        # HITL checkpoint: apply pending control commands, honor pause, stop cleanly.
        self.checkpoint("review iteration")

        # Load persistent user instructions
        persistent_instructions = self.load_user_instructions()
        if persistent_instructions:
            base_anchor = self.config.get("goal_anchor", "")
            self.memory.set_goal_anchor(
                (base_anchor + "\n\n" if base_anchor else "")
                + f"## User Instructions (MUST follow throughout all iterations)\n\n{persistent_instructions}"
            )

        # Check user updates
        user_updates = self.check_user_updates()
        if user_updates:
            self.log(f"Applying user updates to memory context...", "INFO")
            if hasattr(self.memory, 'goal_anchor') and self.memory.goal_anchor:
                self.memory.goal_anchor += f"\n\n## User Updates\n\n{user_updates}"
            else:
                self.memory.set_goal_anchor(f"## User Updates\n\n{user_updates}")

    def _step_compile(self, step_num: int, total_steps: int, resume_step: int) -> bool:
        """Step 1: Compile current LaTeX (with robust retry)."""
        MAX_COMPILE_RETRIES = 5
        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Compile LaTeX", "skipped")
            return True

        self.log_step_header(step_num, total_steps, "Compile LaTeX")
        compiled = False
        errors = ""
        chain = []  # later rounds continue the first round's session
        for attempt in range(1, MAX_COMPILE_RETRIES + 1):
            self.log_step(f"Compiling LaTeX (attempt {attempt}/{MAX_COMPILE_RETRIES})...", "progress")
            success, errors = self.compile_latex_with_errors()
            if success:
                compiled = True
                break

            self.log_step(f"Attempt {attempt} failed, sending errors to writer...", "warning")
            fix_prompt = self._build_latex_fix_prompt(attempt, errors)
            self.run_agent_chain(chain, "writer", fix_prompt)

        if not compiled:
            return self._handle_compile_failure(step_num, total_steps, MAX_COMPILE_RETRIES, errors)

        self.log_step("PDF generated successfully", "success")
        self.log_step_header(step_num, total_steps, "Compile LaTeX", "end")
        self.save_step_checkpoint(step_num, "Compile LaTeX")
        self.notify_progress("Compile", "PDF generated", level="done")
        return True

    def _audit_numbers_at_review(self, tex: str) -> None:
        """Trace the paper's numbers against the results that exist right now.

        This is the moment the check has power: both fabricated drafts in
        production were scored while results/ was empty or nearly so. Run
        after the fact against dense results, invented numbers match by
        chance. Observe-only: the verdict is logged and kept for delivery.
        """
        try:
            import json as _json
            from ark.provenance import audit, verdict
            result = audit(tex, self.code_dir)
            ok, detail = verdict(result)
            record = {"iteration": getattr(self, "iteration", None), "time": time.time(),
                      "ok": ok, "detail": detail, "strong": result["strong"],
                      "strong_traced": result["strong_traced"]}
            (self.state_dir / "provenance_at_review.json").write_text(_json.dumps(record))
            if not ok:
                self.log(f"Number provenance before review: {detail}", "WARN")
        except Exception as e:
            self.log(f"Number provenance check skipped: {e}", "WARN")

    def _build_latex_fix_prompt(self, attempt: int, errors: str) -> str:
        if attempt <= 1:
            return f"LaTeX compilation failed. Fix the syntax errors below and ensure it compiles.\n\n{errors}"
        elif attempt == 2:
            return f"LaTeX still fails to compile. Check for mismatched braces, undefined commands, and missing packages. Here are the errors:\n\n{errors}"
        else:
            return f"LaTeX compilation has failed {attempt} times. Take a conservative approach: comment out the problematic section and replace with a minimal working version. The paper must compile.\n\nErrors:\n{errors}"

    def _handle_compile_failure(self, step_num: int, total_steps: int, max_retries: int, errors: str) -> bool:
        self.log_step(f"LaTeX failed after {max_retries} attempts", "error")
        idx, reply = self.ask_user_decision(
            f"LaTeX compilation failed after {max_retries} writer attempts.",
            options=["Skip this iteration", f"Retry with {max_retries} more writer attempts", "I'll fix manually, then continue"],
            timeout=defaults.TIMEOUT_HITL_DECISION, default=0,
            what_happened=f"LaTeX failed to compile {max_retries} times. The writer agent could not recover.",
            background=[f"Iteration {self.iteration}", f"Latest errors: {errors[:300]}"],
            option_details=["Score stays at the last value; skip iteration.", f"Spends another {max_retries} attempts.", "Wait for manual fix."],
            phase="latex_compile",
        )
        if idx == 1:
            for retry in range(1, max_retries + 1):
                self.log_step(f"Extra retry {retry}/{max_retries}...", "progress")
                success, errors = self.compile_latex_with_errors()
                if success: return True
                self.run_agent("writer", f"LaTeX still broken. Comment out broken parts.\n\n{errors}")
        elif idx == 2:
            self.log_step("Waiting for manual fix...", "progress")
            success, _ = self.compile_latex_with_errors()
            if success: return True

        self.log_step("Cannot compile, skipping iteration", "error")
        self.log_step_header(step_num, total_steps, "Compile LaTeX", "end")
        return False

    def _step_review(self, step_num: int, total_steps: int, resume_step: int, paper_state: dict, current_score: float) -> tuple[str, float, list[str]]:
        """Step 2: Reviewer Agent."""
        review_output = ""
        score = current_score
        issue_ids = []

        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Review Paper", "skipped")
            review_file = self.state_dir / "latest_review.md"
            if review_file.exists(): review_output = review_file.read_text()
            score = paper_state.get("current_score", 0)
            issue_ids = self.extract_issue_ids()
            return review_output, score, issue_ids

        self.log_step_header(step_num, total_steps, "Review Paper")
        self._run_citation_verification()

        # Snapshot the *previous* iter's review (currently at
        # latest_review.md) into the per-iter history archive BEFORE
        # the new reviewer overwrites it. This gives the new reviewer
        # access to "what I said last time" so it can do absolute-scale
        # calibration instead of arbitrarily ratcheting bar each iter.
        prior_review_section = self._archive_and_load_prior_review()
        # latest_review.md still holds LAST iteration's review at this point
        # (the archive copies it, it does not move it). Stamp it, so the checks
        # below can tell a file this reviewer wrote from one it inherited.
        review_file = self.state_dir / "latest_review.md"
        prior_stamp = self._file_stamp(review_file)

        # The version this review scores. Edits made after it (plan, execute,
        # finalize) are never reviewed, so delivery compares against it.
        try:
            reviewed = Path(getattr(self, "latex_dir", "") or ".") / "main.tex"
            if getattr(self, "latex_dir", None) and reviewed.exists():
                tex = reviewed.read_text(errors="replace")
                (self.state_dir / "reviewed_main.tex").write_text(tex)
                self._audit_numbers_at_review(tex)
        except OSError:
            pass

        visual_review = self._build_visual_review_section()
        try:
            venue_name = self.config.get('venue', 'top venue')
            review_output = self.run_agent(
                "reviewer",
                f"Please review the current paper {self.config.get('latex_dir', 'paper')}/main.tex "
                f"and the generated {self.config.get('latex_dir', 'paper')}/main.pdf.\n\n"
                f"Review according to {venue_name} standards. Check whether the reported inputs identify "
                "each headline statistic. If two datasets could have the stated inputs but different values, "
                "flag the exact passage and name the missing measurement.\n"
                "Read any previous delivery findings in auto_research/state/delivery_report.yaml. "
                "Check flagged issues against this compiled PDF before carrying them forward.\n"
                f"{visual_review}\n"
                f"{prior_review_section}\n"
                f"Output a detailed review report and save to auto_research/state/latest_review.md",
                timeout=defaults.TIMEOUT_REVIEWER,
            )
        except Exception as e:
            self.log(f"Review phase failed: {e}", "ERROR")
            self.log_step_header(step_num, total_steps, "Review Paper", "end")
            self.save_step_checkpoint(step_num - 1, "Compile LaTeX")
            return "", score, []

        # The reviewer's ANSWER is often just "report saved to latest_review.md"
        # — a pointer, not the review. If the pointed-at file is missing or
        # empty, there is no score anywhere and the run ends 0.0/failed with a
        # perfectly good paper on disk (95241962: contract all-pass, then the
        # reviewer claimed the save and wrote nothing). Same contract as
        # experiments and idea.md: the artifact, not the claim, is the output —
        # give one corrective pass before accepting a scoreless review.
        def _fresh_review() -> bool:
            stamp = self._file_stamp(review_file)
            return stamp is not None and stamp != prior_stamp and stamp[1] > 200

        if not _fresh_review() and len((review_output or "").strip()) < 400:
            self.log("Reviewer produced no report file — one corrective pass",
                     "WARN")
            try:
                review_output = self.run_agent(
                    "reviewer",
                    "Your previous review attempt saved NOTHING: "
                    "auto_research/state/latest_review.md is missing or empty. "
                    "Write the FULL review report to that file now (use the "
                    "file editor's create command, not str_replace), including "
                    "an explicit line 'Overall Score: X/10'. Then print the "
                    "file (`cat auto_research/state/latest_review.md`) to "
                    "confirm it is non-empty before finishing.",
                    timeout=defaults.TIMEOUT_REVIEWER,
                )
            except Exception as e:
                self.log(f"Review corrective pass failed: {e}", "WARN")
        if _fresh_review():
            # The file is the review of record; the chat answer may be a stub.
            review_output = review_file.read_text(errors="replace")
        elif len((review_output or "").strip()) >= 400:
            # The answer IS the review and the file is last iteration's. Write
            # it through, so issue extraction and the next planner read this
            # iteration's review rather than the one before it.
            review_file.write_text(review_output)

        score = self.parse_review_score(review_output)
        if score == 0.0 and review_output and len(review_output.strip()) > 100:
            score = self._retry_score_parsing(review_output) or 0.0

        score_delta = score - current_score
        delta_str = f"+{score_delta:.1f}" if score_delta >= 0 else f"{score_delta:.1f}"
        self.log_step(f"Score: {score}/10 ({delta_str} from last)", "success" if score_delta >= 0 else "warning")
        self.notify_progress("Review", f"Score {score}/10 ({delta_str} from last)", level="done" if score_delta >= 0 else "warn")

        issue_ids = self.extract_issue_ids()
        self.memory.record_issues(issue_ids, self.iteration)
        self._check_repeat_issues()

        if self.iteration == 1 and score < 5.0 and self.telegram.is_configured:
            self._proactive_intervention(score, review_output)

        paper_state["reviews"].append({"iteration": self.iteration, "timestamp": datetime.now().isoformat(), "score": score, "log": str(self.log_file)})
        paper_state["current_score"] = score
        self.save_paper_state(paper_state)
        self.save_step_checkpoint(step_num, "Review Paper")
        self.log_step_header(step_num, total_steps, "Review Paper", "end")
        return review_output, score, issue_ids

    @staticmethod
    def _file_stamp(path):
        """(mtime_ns, size) of a file, or None when it does not exist."""
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _archive_and_load_prior_review(self) -> str:
        """Snapshot the prior iter's review and build the cross-iter
        calibration block to inject into the new reviewer's prompt.

        Without this, every reviewer call is independent — the new
        reviewer has no memory of what the previous reviewer asked
        for, can't tell which issues were addressed, and tends to
        ratchet up severity of remaining nitpicks until a paper that
        actually improved scores worse than its predecessor. The
        block we return:

        1. Names the score the previous iter received.
        2. Hands the new reviewer the prior review report verbatim,
           so it can check ``did the M1/M2 issues the last reviewer
           raised actually get fixed?`` instead of inventing a fresh
           list.
        3. Gives explicit absolute-scale calibration rules so the
           reviewer doesn't escalate minor issues into majors just
           because no real majors remain.

        Side effect: archives the previous ``latest_review.md`` into
        ``auto_research/state/.review_history/iter_<n>.md`` so the
        per-iter audit trail survives across runs.
        """
        review_path = self.state_dir / "latest_review.md"
        if not review_path.exists():
            # First iter — nothing to calibrate against.
            return ""
        try:
            prior_text = review_path.read_text(errors="replace")
        except Exception:
            return ""
        if not prior_text.strip():
            return ""

        # Archive the prior review under .review_history/ keyed by
        # the iteration number that produced it (current iter - 1).
        prior_iter = max(1, getattr(self, "iteration", 1) - 1)
        history_dir = self.state_dir / ".review_history"
        try:
            history_dir.mkdir(parents=True, exist_ok=True)
            archive_path = history_dir / f"iter_{prior_iter}.md"
            if not archive_path.exists():
                archive_path.write_text(prior_text)
        except Exception as e:
            self.log(f"Could not archive prior review: {e}", "WARN")

        # Pull the prior score out of memory.yaml (already maintained
        # by self.memory.record_issues / save_paper_state).
        prior_score: Optional[float] = None
        try:
            mem_path = self.state_dir / "memory.yaml"
            if mem_path.exists():
                import yaml as _yaml
                mem = _yaml.safe_load(mem_path.read_text()) or {}
                history = mem.get("score_history") or []
                if history:
                    last = history[-1]
                    if isinstance(last, dict):
                        prior_score = last.get("score")
        except Exception:
            prior_score = None

        score_line = (
            f"- Prior iteration ({prior_iter}) Total score: **{prior_score:.1f}/10**\n"
            if isinstance(prior_score, (int, float))
            else f"- Prior iteration ({prior_iter}) score: not parsed (assume nearby).\n"
        )

        # Cap prior review payload so the reviewer's prompt doesn't
        # blow up — full review is still on disk if needed.
        prior_excerpt = prior_text
        if len(prior_excerpt) > 8000:
            prior_excerpt = prior_excerpt[:8000] + "\n\n[... truncated; full review at auto_research/state/.review_history/iter_{}.md ...]".format(prior_iter)

        return (
            "\n\n## Cross-Iteration Calibration (MANDATORY)\n\n"
            f"{score_line}"
            f"- Per-iter review archive: `auto_research/state/.review_history/iter_{prior_iter}.md`\n\n"
            "You must use the prior review as the calibration anchor for this round. "
            "Specifically:\n\n"
            "1. **Compare each prior issue to the current paper.** For every M1/M2/m1/... raised "
            "previously, decide one of: `RESOLVED` / `PARTIALLY_RESOLVED` / `STILL_PRESENT` / "
            "`NEW_PROBLEM_INTRODUCED_INSTEAD`. State this in a `## Delta from Prior Iteration` "
            "section in your review.\n"
            "2. **Score must reflect the delta.** A paper that resolved 3 of 4 majors with no new "
            "majors must score *higher* than the prior iter. A paper where remaining issues are "
            "objectively minor must NOT be re-classified as Major just because they are now the "
            "worst remaining. Use the absolute Venue Calibration above — a nitpick is a nitpick "
            "regardless of how few issues remain.\n"
            "3. **Do not re-raise issues that were RESOLVED.** If you cannot articulate a NEW "
            "issue beyond the previous round, the score should rise toward the venue accept bar.\n"
            "4. **New issues require justification.** If you raise an issue that was not in the "
            "prior review, briefly say why it became visible now (e.g. \"newly introduced by "
            "iter-N edit\", \"now visible because previous M1 was resolved\", or \"I missed it "
            "last round, acknowledging\").\n\n"
            "### Prior review (verbatim):\n\n"
            f"```markdown\n{prior_excerpt}\n```\n"
        )

    def _build_visual_review_section(self) -> str:
        if selected_agent_runtime() == "dsh":
            from ark.engines.dsh_runtime import supports_images
            if not supports_images(self._model_for("reviewer")):
                return ("\n\n## Visual Review\n"
                        "This model accepts text only. Review the LaTeX and label "
                        "the rendered page layout as unreviewed.\n")
        page_images = self._maybe_generate_page_images()
        if not page_images: return ""
        
        figure_types = ""
        try:
            from ark.figure_manifest import load_manifest
            manifest = load_manifest(self.figures_dir)
            figures = manifest.get("figures", {})
            ai_figs = [f for f, info in figures.items() if info.get("source") in ("paperbanana", "nano_banana")]
            mpl_figs = [f for f, info in figures.items() if info.get("source") == "matplotlib"]
            if ai_figs or mpl_figs:
                figure_types = "\n\nFigure sources:\n"
                if ai_figs: figure_types += f"- AI concept figures: {', '.join(ai_figs)}\n"
                if mpl_figs: figure_types += f"- Matplotlib plots: {', '.join(mpl_figs)}\n"
        except Exception: pass

        return f"\n\n## Visual Review\n\nPlease read: {chr(10).join(f'- {img}' for img in page_images)}\nKey checks: sizes, layout, density, visual quality.\n{figure_types}"

    def _retry_score_parsing(self, review_output: str) -> Optional[float]:
        self.log("Score parsed as 0 but review exists, retrying reviewer for explicit score...", "WARN")
        retry_output = self.run_agent("reviewer", "The previous review did not output an explicit score. Provide one (Overall Score: X/10).", timeout=defaults.TIMEOUT_SCORE_RETRY)
        return self.parse_review_score(retry_output)

    def _check_repeat_issues(self):
        repeat_issues = self.memory.get_repeat_issues(threshold=3)
        if repeat_issues:
            self.log("Warning: repeating issues detected!", "WARN")
            for issue_id, count in repeat_issues: self.log(f"  - {issue_id}: appeared {count} times", "WARN")

    def _record_intervention_choice(self, options, idx, reply, score):
        """Make an intervention menu pick actually do something: record the
        chosen option as next-iteration guidance. (Free-text replies are already
        injected by ask_user_decision; a bare number used to be inert.)"""
        if reply and not str(reply).strip().isdigit():
            return  # free text already injected by ask_user_decision
        if options and 0 <= idx < len(options):
            choice = options[idx]
            self.log(f"User decision: {choice}", "INFO")
            try:
                self.inject_user_update(f"[Your decision at {score:.1f}/10] {choice}")
            except Exception:
                pass

    def _proactive_intervention(self, score: float, review_output: str):
        question, options = self._build_intervention_options(score, 0, review_output, trigger="First review score is low")
        background = self._build_decision_background(review_output, options, score=score)
        idx, reply = self.ask_user_decision(question, options, timeout=defaults.TIMEOUT_HITL_DECISION, what_happened=f"First review came back at {score}/10.", background=background, option_details=self._build_option_details(options, review_output), phase="first_review")
        self._record_intervention_choice(options, idx, reply, score)
        self._asked_this_iteration = True

    def _check_acceptance(self, score: float, issue_ids: list[str], paper_state: dict) -> tuple[bool, bool, bool]:
        """Check if paper meets acceptance threshold. Returns (stop, post_accept_cleanup, stop_after_cleanup)."""
        if score >= self.paper_accept_threshold:
            cleanup_done = paper_state.get("post_accept_cleanup_done", False)
            if issue_ids and not cleanup_done:
                self.log_section(f"SCORE REACHED {score}/10, Running One Final Issue Cleanup Iteration", "★")
                paper_state["status"] = "accepted_pending_cleanup"
                paper_state["post_accept_cleanup_done"] = True
                paper_state["accepted_score"] = score
                paper_state["accepted_iteration"] = self.iteration
                return False, True, True
            # A positive review skips planning and execution. The same
            # pre-delivery checks still run before recording acceptance.
            return True, False, True
        return False, False, False

    def _step_plan(self, step_num: int, total_steps: int, resume_step: int, review_output: str) -> tuple[dict, str]:
        """Step 3: Plan."""
        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Plan", "skipped")
            if not review_output:
                review_file = self.state_dir / "latest_review.md"
                if review_file.exists(): review_output = review_file.read_text()
            return self._load_action_plan(), ""

        self.log_step_header(step_num, total_steps, "Plan")
        action_plan, planner_output = None, ""
        try:
            if self.memory.stagnation_count >= 5:
                self.log("Stagnation detected, triggering self-repair", "REPAIR")
                _, stagnation_reason = self.memory.is_stagnating()
                self.self_repair(stagnation_reason)
            self._reset_stale_action_plan()
            action_plan, planner_output = self.run_planning_phase(review_output)
        except Exception as e: self.log(f"Plan phase failed: {e}", "ERROR")

        self.save_step_checkpoint(step_num, "Plan")
        self.log_step_header(step_num, total_steps, "Plan", "end")
        n_actions = 0
        if isinstance(action_plan, dict): n_actions = len(action_plan.get("actions") or action_plan.get("issues") or [])
        elif isinstance(action_plan, list): n_actions = len(action_plan)
        self.notify_progress("Plan", f"{n_actions} action(s) queued", level="done")
        return action_plan, planner_output

    def _step_execute(self, step_num: int, total_steps: int, resume_step: int, action_plan: dict, planner_output: str):
        """Step 4: Execute."""
        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Execute", "skipped")
            return

        self.log_step_header(step_num, total_steps, "Execute")
        if 0 < resume_step == step_num - 1 and isinstance(action_plan, dict):
            # Resumed into Execute. An "in_progress" task was cut off by the
            # stop or crash that ended the last run, and only "pending" tasks
            # are scheduled. Nothing is running now, so it is pending again.
            cut = [i for i in action_plan.get("issues") or []
                   if i.get("status") == "in_progress"]
            experiments = {i.get("id") for i in cut if i.get("type") == "EXPERIMENT_REQUIRED"}
            if experiments and not settle_interrupted_experiments(self):
                return
            self._interrupted_review_experiments = experiments
            for i in cut:
                i["status"] = "pending"
            if cut:
                self._save_action_plan(action_plan)
                self.log(f"Resuming Execute: {len(cut)} interrupted task(s) back to pending", "INFO")
        execute_ok = False
        try:
            if action_plan:
                execute_ok = self._run_execute_phase(action_plan, planner_output)
                self._check_human_intervention(stage="Execute")
        except Exception as e: self.log(f"Execute phase failed: {e}", "ERROR")

        if not (self._quota_exhausted or getattr(self, "_terminal_error", None)):
            self.save_step_checkpoint(step_num, "Execute")
        self.log_step_header(step_num, total_steps, "Execute", "end")
        self.notify_progress("Execute", "completed" if execute_ok else "incomplete", level="done" if execute_ok else "warn")

    def _step_validate(self, step_num: int, total_steps: int, resume_step: int):
        """Step 5: Validate (figure quality check)."""
        if step_num <= resume_step:
            self.log_step_header(step_num, total_steps, "Validate", "skipped")
            return
        self.log_step_header(step_num, total_steps, "Validate")
        if self._should_skip_figure_phase(): self.log_step("Figure phase skipped", "info")
        else: self._run_figure_phase()
        self.save_step_checkpoint(step_num, "Validate")
        self.log_step_header(step_num, total_steps, "Validate", "end")
        self.notify_progress("Validate", "figures checked", level="done")

    def _iteration_finalize(self, score: float, current_score: float, paper_state: dict, review_output: str, post_accept_cleanup: bool, stop_after_cleanup: bool) -> bool:
        """Finalize iteration: pre-delivery checks, summary, stagnation check."""
        self.save_paper_state(paper_state)
        self._last_score = score
        
        self.log("", "RAW")
        gap = self.paper_accept_threshold - score
        status = "POST_ACCEPT_CLEANUP" if post_accept_cleanup else ("CONTINUE" if gap > 0 else "CHECKING DELIVERY")
        self.log_summary_box(f"Iteration {self.iteration} Summary", [f"Score: {score}/10", f"Status: {status}"], inside_phase=False)
        self.record_score_to_memory(score)
        
        self.log_step("Pre-delivery checks...", "progress")
        self._ensure_clearpage_before_bibliography()
        self._ensure_float_barrier()
        # Continue can skip the initial figure stage. Recheck existing final
        # artwork before compiling so the PDF includes any approved edit.
        if (self.config.get("figure_generation") == "nano_banana"
                and (self.figures_dir / "concept_figures.json").is_file()):
            self._generate_nano_banana_figures()
        self.compile_latex()
        # Before page fitting: integrating forgotten figures changes the page
        # count, so this must precede _enforce_page_count.
        self._ensure_figures_referenced()
        # Before page fitting and before _fix_overfull: regenerating a figure
        # changes its size, which changes the page count and can create a new
        # overfull box. This is the last actor before delivery — nothing after
        # it reads a figure.
        self._fix_illegible_figures(context="pre-delivery")
        self._fix_overfull(context="pre-delivery")
        self._run_citation_verification()
        self._repair_cited_sources()
        try:
            layout_ok = self._enforce_page_count(context="pre-delivery")
        except QuotaExhaustedError as e:
            return self._handle_quota_exhausted(score, detail=f"Page compression failed: {e.page_count:.1f}/{e.venue_pages} pages")
        # LAST compile-touching gate: page-fitting above may have recompiled via
        # agent-side bare pdflatex (no bibtex), which can strand "?" citations in
        # the delivered PDF (happened in 50350a67). Verify + fix on the final PDF.
        # Hard gate: backfill + prune should always clear "?" markers; if they
        # survive even that, the bibliography is broken beyond auto-repair — do
        # NOT deliver a paper full of "?" as a clean result. Flag it (sticky)
        # so the run ends failed with an actionable reason rather than shipping
        # garbage (the recurring missing-references failure).
        # A page-length edit can reintroduce an old citation or overflow.
        self._repair_cited_sources(recheck_database=False)
        self._fix_overfull(context="final-layout")
        if not self.compile_latex():
            self._run_fatal = "Final manuscript sources failed to compile. See the LaTeX build log."
        elif not self.ensure_resolved_citations(context="pre-delivery"):
            self._run_fatal = ("Bibliography could not be resolved: the paper cites "
                               "works that no academic database could confirm and "
                               "auto-repair (backfill + prune) failed. references.bib "
                               "is incomplete.")
            self.log("Delivery blocked: unresolved citations after backfill+prune", "ERROR")

        report = self._report_delivery_checks()
        problems = report.delivery_blockers(self.config) if report is not None else []
        if layout_ok is False and self.config.get("layout_mode") == "strict":
            from ark.delivery_contract import Finding
            problems.append(Finding("page_budget", "violated", "soft",
                                    f"Page fitting stalled at {self._body_page_count:.2f} "
                                    f"of {self.config.get('venue_pages')} requested body pages."))
        failure = getattr(self, "_run_fatal", None)
        if report is None and not failure:
            failure = "Could not verify the final manuscript. See the delivery-check log."
        if problems and not failure:
            failure = "Final manuscript checks: " + "; ".join(f"{f.check}: {f.detail}" for f in problems)
        if failure:
            paper_state["status"] = "in_progress"
            self.save_paper_state(paper_state)
            self.log(failure, "ERROR")
            if problems and not getattr(self, "_run_fatal", None) and self.iteration < self.max_iterations:
                self.save_checkpoint()
                return True
            self._run_fatal = failure
            # Repeat the unfinished paper iteration on Continue. The separate
            # experiment checkpoint and all existing worker outputs stay valid.
            self.iteration -= 1
            self.save_checkpoint()
            return False

        self.send_iteration_summary(score, current_score, review_output)
        if not post_accept_cleanup and not stop_after_cleanup: self._check_smart_intervention(score, current_score, review_output, True)
        self.cleanup_workspace()
        self.git_commit(f"Iteration {self.iteration}: score {score}/10")
        self.save_checkpoint()

        if stop_after_cleanup:
            paper_state["status"] = "accepted"
            self.save_paper_state(paper_state)
            self.log_section(f"PAPER ACCEPTED  |  Score: {score}/10", "★")
            if not post_accept_cleanup:
                self.send_notification("Paper Accepted", f"{self.project_name.upper()} scored {score}/10 after {self.iteration} iterations")
            return False

        return self._handle_stagnation(score, current_score, review_output)

    def _repair_cited_sources(self, *, recheck_database=True):
        """Repair explicitly unconfirmed citations before fitting the final PDF."""
        if self.config.get("layout_mode") != "strict":
            return
        from ark.citation import unconfirmed_citations
        from ark.orchestrator.state import _atomic_write_yaml
        import hashlib
        missing = unconfirmed_citations(self.latex_dir)
        survey = self.state_dir / "deep_research.md"
        library = self.state_dir / "literature.yaml"
        new_keys = set()
        if recheck_database and survey.is_file() and library.is_file():
            try:
                literature = yaml.safe_load(library.read_text())
            except (OSError, yaml.YAMLError):
                literature = None
            if not isinstance(literature, dict):
                literature = {}
            confirmed = {r.get("bibtex_key") for r in literature.get("references") or []
                         if isinstance(r, dict) and r.get("bibtex_key")}
            pending = [r["bibtex_key"] for r in literature.get("needs_check") or []
                       if isinstance(r, dict) and r.get("bibtex_key")]
            digest = hashlib.sha256(survey.read_bytes()).hexdigest()
            # If most of the initial shortlist failed verification, deleting
            # the affected passages would leave a nominally clean but thin
            # discussion. Make one primary-source discovery pass per survey.
            thin = bool(pending and len(pending) >= len(confirmed)
                        and literature.get("coverage_review_sha256") != digest)
            if missing or thin:
                candidates = missing or pending
                self.log("Checking primary sources for gaps in the literature review", "WARN")
                result = self._bootstrap_citations_from_deep_research(recheck_keys=candidates)
                if result is not None:
                    new_keys = set(result.found_keys) - confirmed
                    if thin:
                        updated = yaml.safe_load(library.read_text()) or {}
                        updated["coverage_review_sha256"] = digest
                        _atomic_write_yaml(library, updated, sort_keys=False)
            missing = unconfirmed_citations(self.latex_dir)
        if missing or new_keys:
            self.run_agent("writer",
                "Repair the supporting literature in the manuscript. Unconfirmed keys still cited: "
                + (", ".join(missing) or "none") + ". Additional verified keys: "
                + (", ".join(sorted(new_keys)) or "none") + ". Read the supporting passages in "
                  "the saved research, original records and managed bibliography. Integrate each "
                  "relevant finding where it supports a specific statement about the research question, "
                  "baseline, benchmark or closest alternative. Remove claims lacking such support. "
                  "Use primary sources that matter to this study. Do not reinstate unconfirmed "
                  "citations to fill space. Leave verification marks and experimental values intact.")

    def _ensure_figures_referenced(self):
        """Delivery gate: generated figures must actually appear in the paper.

        A writer occasionally ships main.tex with ZERO ``\\includegraphics``
        while paper/figures/ holds real, usable figures (44459bb4 delivered 4
        figure files, none referenced). One focused writer pass integrates
        them. Fail-soft: a gate error never blocks delivery.
        """
        try:
            figs = [f for f in sorted(self.figures_dir.glob("*"))
                    if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf") and f.stat().st_size > 5000]
            if not figs:
                return
            main_tex = self.latex_dir / "main.tex"
            if not main_tex.exists():
                return
            has_graphics = bool(re.search(r"\\includegraphics", main_tex.read_text(errors="replace")))
            if has_graphics:
                if self.config.get("figure_generation") != "nano_banana":
                    return
                from ark.delivery_contract import _concept_figure_in_pdf
                from ark.figure_manifest import load_manifest, AI_SOURCES
                manifest = load_manifest(self.figures_dir, infer_existing=False).get("figures", {})
                figs = [f for f in figs if isinstance(manifest.get(f.name), dict)
                        and manifest[f.name].get("source") in AI_SOURCES]
                if not figs or _concept_figure_in_pdf(main_tex.with_suffix(".pdf"), self.figures_dir).status != "violated":
                    return
            names = ", ".join(f.name for f in figs[:8])
            self.log(f"Integrating available figures into the manuscript: {names}", "WARN")
            self.run_agent("writer", (
                f"The compiled paper is missing available figure evidence from paper/figures/: {names}.\n"
                f"Place the requested concept artwork near its explanation in the main body. "
                f"Integrate supporting data figures into main.tex "
                f"using proper figure environments with accurate captions, and "
                f"reference each from the prose (\\label/\\ref). Do NOT invent "
                f"results or alter figure data; skip any figure that does not fit "
                f"the narrative. Ensure the paper still compiles."),
                timeout=defaults.TIMEOUT_PAGE_ADJUSTMENT)
            self.compile_latex()
        except Exception as e:
            self.log(f"figures-referenced gate failed (non-fatal): {e}", "WARN")

    def _handle_terminal_error(self, score: float, rollback_iteration: bool = True) -> bool:
        """Abort the run fast on a non-retryable agent error (bad key / model /
        permission). No quota-style wait — the error won't fix itself."""
        # This iteration didn't complete; roll the counter back so a re-run
        # (once the user fixes the key/model) redoes it instead of skipping it.
        # A dev-phase abort never started a paper iteration, so there is
        # nothing to roll back (doing it anyway saved iteration=-1).
        steps_done = self.get_resume_step() if rollback_iteration else 0
        if rollback_iteration:
            self.iteration -= 1
        detail = getattr(self, "_terminal_error", "") or "non-retryable agent error"
        # STICKY run-fatal marker. `_terminal_error` is attempt-scoped and gets
        # reset by `_iteration_prep()` at the top of every iteration — a later
        # phase's prep wiped the abort evidence and main() reported the run
        # "done" (699d9538, 0bda3eef: credit-exhausted stubs delivered as
        # completed papers). `_run_fatal` is run-scoped and NEVER cleared.
        self._run_fatal = str(detail)[:400]
        self.log("", "RAW")
        self.log_summary_box(
            "Run ABORTED (non-retryable error)",
            [f"Score: {score}/10 (unchanged)", str(detail)[:300],
             "See the error above — bad API key/model, usage or billing limit, "
             "or context overflow. Fix it (or switch provider) and re-run."],
            inside_phase=False,
        )
        self._checkpoint_unfinished_iteration(steps_done)
        return False  # stop the run; do NOT wait

    def _checkpoint_unfinished_iteration(self, steps_done: int):
        """Checkpoint an iteration that stopped part way through.

        With steps already done, their step checkpoint is the resume pointer:
        resume_from_checkpoint() re-enters the iteration after them. Saving
        the rolled-back iteration here instead wiped it, so 954d7885's Continue
        after its balance ran out re-reviewed and re-planned an unchanged paper.
        """
        if not steps_done:
            self.save_checkpoint()
            return
        self.log(f"Checkpoint kept: iteration {self.iteration + 1}, step {steps_done}/5 "
                 "done, a re-run continues from there", "INFO")
        try:
            self._publish_state_docs()
        except Exception as e:
            self.log(f"state doc projection failed (non-fatal): {e}", "WARN")

    def _handle_quota_exhausted(self, score: float, detail: str = "") -> bool:
        steps_done = self.get_resume_step()
        self.iteration -= 1
        self.log("", "RAW")
        self.log_summary_box("Iteration ABORTED (quota exhausted)", [f"Score: {score}/10 (unchanged)", detail or "API quota exhausted"], inside_phase=False)
        self._checkpoint_unfinished_iteration(steps_done)
        wait_time = 1800
        self.log(f"Pausing {wait_time}s waiting for API quota reset...", "ERROR")
        self.send_notification("Quota Exhausted", f"Pausing {wait_time // 60}min before retry")
        RateLimitCountdown(wait_time).run()
        return True

    def _handle_stagnation(self, score: float, current_score: float, review_output: str) -> bool:
        is_stagnating, stagnation_reason = self.memory.is_stagnating()
        if not is_stagnating: return True

        self.log(f"Stagnation detected: {stagnation_reason}", "WARN")
        if self.memory.stagnation_count >= 3:
            self.log("Triggering self-repair...", "REPAIR")
            self.self_repair(stagnation_reason)
        else:
            self.log("Stagnation count low, delegating to Meta-Debugger", "WARN")

        if self.memory.stagnation_count >= 3 and self.telegram.is_configured:
            self._stagnation_intervention(score, current_score, review_output)

        return True

    def _stagnation_intervention(self, score: float, current_score: float, review_output: str):
        review_src = review_output
        if not review_src and (self.state_dir / "latest_review.md").exists():
            review_src = (self.state_dir / "latest_review.md").read_text()
        trigger = f"Stuck {self.memory.stagnation_count} rounds at {score}/10"
        question, options = self._build_intervention_options(score, current_score, review_src or "", trigger=trigger)
        background = self._build_decision_background(review_src or "", options, score=score)
        idx, reply = self.ask_user_decision(question, options, timeout=defaults.TIMEOUT_HITL_DECISION, what_happened=f"Stagnation triggered at {score}/10.", background=background, option_details=self._build_option_details(options, review_src or ""), phase="stagnation_intervention")
        self._record_intervention_choice(options, idx, reply, score)
        self._asked_this_iteration = True

    def check_dependencies(self):
        """Check the selected runtime, model routes and credentials."""
        from ark.llm_lite import provider_key_env

        if selected_agent_runtime() == "dsh":
            from ark.engines.dsh_runtime import dsh_installation_error, route
            error = dsh_installation_error()
            if error:
                self.log(error, "ERROR")
                sys.exit(1)
            if os.environ.get("ARK_AGENT_SANDBOX", "").strip().lower() == "apptainer":
                self.log("Error: Apptainer requires ARK_AGENT_RUNTIME=sdk.", "ERROR")
                sys.exit(1)
            for role, m in [("model", self.model or "")] + list(
                    (self.config.get("agent_models") or {}).items()):
                try:
                    route(m)
                except ValueError as e:
                    self.log(f"Error: ARK_AGENT_RUNTIME=dsh cannot run {role} '{m}': {e}.", "ERROR")
                    sys.exit(1)
        elif not shutil.which("openhands"):
            self.log(
                "Error: 'openhands' command not found. Install with: "
                "uv tool install --python 3.12 openhands",
                "ERROR",
            )
            sys.exit(1)

        # 2. The model must be a LiteLLM string (<provider>/<model>) and that
        #    provider's key must exist. Any OpenHands/LiteLLM provider is allowed
        #    — anthropic/openai/gemini are just the common ones.
        model = self.model or ""
        provider = model.split("/", 1)[0] if "/" in model else ""
        if not provider:
            self.log(
                f"Error: model '{model}' is not a LiteLLM model string. Set `model` "
                f"in config.yaml as <provider>/<model>, e.g. "
                f"anthropic/claude-sonnet-5-5, gemini/gemini-2.5-flash, "
                f"deepseek/deepseek-chat.",
                "ERROR",
            )
            sys.exit(1)
        # Every model the run will call needs its key: the main one and any
        # per-role override (agent_models).
        for role, other in (self.config.get("agent_models") or {}).items():
            p = other.split("/", 1)[0] if "/" in (other or "") else ""
            if not p or not (self.config.get(f"{p}_api_key") or os.environ.get(provider_key_env(p))
                             or (os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY"))):
                self.log(f"Error: agent_models.{role} is '{other}' but no key for it is set "
                         f"({provider_key_env(p) if p else 'not a <provider>/<model> string'}).", "ERROR")
                sys.exit(1)
        cfg_field = f"{provider}_api_key"
        env_var = provider_key_env(provider)
        # A custom gateway (LLM_BASE_URL) is reached with LLM_API_KEY, the same
        # fallback llm_lite and both runtimes use.
        if not (self.config.get(cfg_field) or os.environ.get(env_var)
                or (os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY"))):
            self.log(
                f"Error: model is '{model}' but no key found — set {cfg_field} in "
                f"config.yaml (or {env_var} in the environment).",
                "ERROR",
            )
            sys.exit(1)

        # 3. Deep Research is Gemini-only; warn (don't fail) when no gemini key.
        if not (self.config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")):
            self.log(
                "Note: no gemini_api_key set — Gemini Deep Research will be "
                "skipped (optional feature).",
                "WARN",
            )

        # Paper mode always needs LaTeX tools.
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

        install_cmd = latex_utils.detect_latex_install_command()
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
            question, options, timeout=defaults.TIMEOUT_HITL_DECISION, default=0,
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
                install_cmd, shell=True, capture_output=True, text=True, timeout=defaults.TIMEOUT_LATEX_COMPILE,
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


    # ==================== Research Phase ====================

    def _needs_deep_research(self) -> bool:
        from ark.deep_research import research_report_status

        if self.config.get("skip_deep_research"):
            return False
        status = research_report_status(self.state_dir)
        if status in ("verified", "legacy"):
            # A crash can land between publishing the report and removing the
            # old fallback marker. The completed report already incurred cost.
            (self.state_dir / "research_degraded.txt").unlink(missing_ok=True)
        if status == "missing" or not (self.state_dir / "deep_research.md").is_file():
            return True
        if (status in ("degraded", "invalid_receipt")
                and (self.state_dir / "deep_research_interaction.json").is_file()
                and self.config.get("deep_research_backend", "auto") in ("auto", "gemini")):
            return True
        required = self.config.get("skip_deep_research") is False and (
            self.config.get("layout_mode") == "strict"
            or self.config.get("deep_research_backend") in ("gemini", "openrouter"))
        return required and status in ("degraded", "invalid_receipt")

    def _should_run_research_phase(self) -> bool:
        """Check if the Research Phase should run.

        Returns True if any sub-step still needs to run:
        - idea.md missing (proposal not analyzed yet)
        - deep_research.md missing (Gemini hasn't run yet)
        - project_context.md missing (specialization not done yet)
        """
        # skip_deep_research means exactly that — skip the literature survey.
        # It used to early-return here and skip the WHOLE phase, so idea
        # analysis and project_context.md (specialization) never ran either.
        # The experimenter prompt makes project_context.md mandatory in three
        # places, so every skip_deep_research project — including the webapp's
        # cheap-test preset — sent its experimenter after a file that could not
        # exist. A strong model shrugs past the lie; an obedient one follows
        # the escalation protocol and reports the missing prerequisite instead
        # of experimenting (1c9e7020, first corrective pass). Treat the flag as
        # "the DR sub-step is already done", nothing more.
        idea_done = (self.state_dir / "idea.md").exists()
        dr_done = not self._needs_deep_research()
        ctx_done = (self.state_dir / "project_context.md").exists()

        if idea_done and dr_done and ctx_done:
            return False

        return True

    def _ensure_project_env(self):
        """Provision the per-project conda env + seed the Apptainer sandbox helper.

        Idempotent. Called UNCONDITIONALLY at run() start — NOT only inside the
        research phase. continue/restart skip the research phase, so if the conda
        env was GC'd after completion (v0.5.3 disk reclaim), this is what rebuilds
        it. Without this, a continued/restarted run that touches experiments would
        fail on a missing .conda_env.
        """
        try:
            from website.dashboard.jobs import provision_project_env, project_env_ready
            if not project_env_ready(self.code_dir):
                base_env = self.config.get("base_conda_env", "ark-base")
                self.log_step(f"Provisioning conda environment (cloning {base_env})...", "progress")
                self.notify_progress(
                    "Env setup", f"cloning base env <code>{base_env}</code>...",
                    level="working",
                )
                success, msg = provision_project_env(
                    self.code_dir, base_env,
                    log_fn=lambda m: self.log_step(m, "progress"))
                if success:
                    self.log_step(f"Conda env ready: {msg}", "success")
                    self.notify_progress("Env ready", f"{msg}", level="done")
                else:
                    self.log_step(f"Conda env provisioning failed: {msg}", "error")
                    self.notify_progress("Env setup failed", f"{msg}", level="warn")
                    raise RuntimeError(f"Conda env provisioning failed: {msg}")
            else:
                self.log_step("Conda env already exists", "success")
        except ImportError as e:
            self.log(f"Conda env provisioning skipped (webapp.jobs unavailable): {e}", "WARN")

        # Seed the Apptainer experiment-sandbox helper so experiments run isolated
        # from the host. Best-effort: no-op if apptainer / base image are missing.
        try:
            from ark.sandbox import (write_sandbox_helper, sandbox_available,
                                     sandbox_sif_path, structural_sandbox_requested,
                                     structural_sandbox_status)
            structural, reason = structural_sandbox_status()
            if structural:
                self.log_step(f"Experiment sandbox: STRUCTURAL — the agent itself runs "
                              f"inside Apptainer ({reason})", "success")
            elif structural_sandbox_requested():
                # Asked for and not delivered is the one case that must be loud:
                # otherwise the run looks confined and is not.
                self.log(f"Structural sandbox REQUESTED but unavailable: {reason} — "
                         f"falling back to the advisory sandbox", "WARN")
            if not structural:
                if sandbox_available():
                    if write_sandbox_helper(self.code_dir):
                        self.log_step("Experiment sandbox ready (Apptainer): ./sandbox/run.sh", "success")
                else:
                    self.log(f"Experiment sandbox unavailable (apptainer/image at {sandbox_sif_path()} missing) — experiments will run on host", "WARN")
        except Exception as e:
            self.log(f"Sandbox helper seeding skipped: {e}", "WARN")

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
                f"{self.tg_header('🚤')}\n"
                f"🔬 <b>Research Phase started</b> — analyzing proposal & building foundation...",
                parse_mode="HTML",
            )

        # ── Step 0: Setup (conda env provisioning) ──────────────────────
        # Idempotent — the real work now runs unconditionally at run() start via
        # _ensure_project_env() so continue/restart (which skip this phase) still
        # get the env rebuilt. This call is a no-op when already provisioned.
        self.log_step_header(0, 4, "Setup")
        self._ensure_project_env()
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
- Ask about the foundational studies and closest primary comparisons, including their assumptions.
- Ask how the dataset, methods and evaluation bear on the scientific question.
- Seek competing explanations and the limits of transferring published results to this design.
- Keep operational setup in the project context unless that system is itself being studied.

Output the query as plain text at the END of your response, after a line that says
"DEEP_RESEARCH_QUERY:" — everything after that line is the query.

Be thorough and faithful to the proposal.
""", timeout=defaults.TIMEOUT_INITIALIZER)

            # idea.md is a CONTRACT — the planner and experimenter prompts both
            # name it as required reading. Its existence must not depend on the
            # agent's diligence when we hold the source text ourselves: a local
            # model completed this step without writing the file, nothing
            # checked, and two phases later the experimenter (correctly)
            # refused to proceed over the missing prerequisite (9df9e778).
            # The agent's version is richer when it exists; the verbatim
            # proposal is strictly better than a broken promise when it
            # doesn't.
            # Missing OR empty: an agent that opens the file and writes nothing
            # has kept the letter of the contract and none of it (c59edf94
            # left a 0-byte idea.md; the first version of this check waved it
            # through on existence alone — the same 0-byte lesson as the
            # results files, one hour later).
            if ((not idea_file.exists() or idea_file.stat().st_size == 0)
                    and self._research_idea):
                self.log("Researcher did not write idea.md — seeding it from "
                         "the proposal text verbatim", "WARN")
                idea_file.parent.mkdir(parents=True, exist_ok=True)
                idea_file.write_text(
                    "# Research Idea (verbatim from the launch form; the "
                    "analysis step did not produce its enriched version)\n\n"
                    + self._research_idea)

            self.log_step_header(1, 4, "Analyze Proposal", "end")
        else:
            self.log_step("idea.md exists, skipping proposal analysis", "info")

        # Generate title from idea.md via dedicated LLM call (validated + retry).
        self._update_title_from_idea()

        # ── Step 2: Deep Research ───────────────────────────────────────
        # Honor skip_deep_research HERE too, now that the flag no longer skips
        # the phase wholesale: without this gate a skip project that reaches
        # this step (to build project_context.md) would fall into the key-based
        # backend selection and run a paid survey the user asked not to run.
        dr_file = self.state_dir / "deep_research.md"
        if self.config.get("skip_deep_research", False):
            pass
        elif self._needs_deep_research():
            import os as _os
            from ark.deep_research import (
                run_deep_research, run_deep_research_openrouter, get_gemini_api_key,
            )
            # Backend selection. Default (auto): if an OpenRouter key is present,
            # use Perplexity sonar-deep-research via OpenRouter (one key, cost
            # tracked, returns TITLED citations — Gemini DR only gave bare
            # domains). Else fall back to Gemini Deep Research. `config
            # deep_research_backend` (openrouter|gemini|auto) can force one.
            or_key = _os.environ.get("OPENROUTER_API_KEY", "") or self.config.get("openrouter_api_key", "")
            gem_key = self.config.get("gemini_api_key", "") or get_gemini_api_key()
            if ((self.state_dir / "deep_research_interaction.json").is_file()
                    and self.config.get("deep_research_backend", "auto") in ("auto", "gemini")):
                # Keep polling a submitted Google survey, even when a newly
                # available OpenRouter key would normally win in auto mode.
                use_or, use_gem, or_low = False, bool(gem_key), None
            else:
                use_or, use_gem, or_low = self._deep_research_route(or_key, gem_key)
            label = ("OpenRouter" if use_or else "Gemini" if use_gem
                     else "literature search")
            self.log_step_header(2, 4, f"Deep Research ({label})")

            if use_or or use_gem or or_low is not None:
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
                        "1. Foundational sources, closest prior work and relevant recent comparisons\n"
                        "2. State-of-the-art approaches, benchmarks, and baselines\n"
                        "3. Key technical challenges and open problems\n"
                        "4. Original sources for the methods, dataset and evaluation metrics\n"
                        "5. Evidence that challenges the proposed explanation and its limits.\n"
                        "Keep routine installation, compute and manuscript production out of the literature review."
                    )

                try:
                    if use_or:
                        result = run_deep_research_openrouter(
                            config=self.config,
                            output_dir=self.state_dir,
                            api_key=or_key,
                            custom_query=query,
                        )
                    elif not use_gem:
                        # Spent OpenRouter key and no Gemini key: the direct
                        # literature search below is the plan, not a fallback.
                        result = None
                    else:
                        result = run_deep_research(
                            config=self.config,
                            output_dir=self.state_dir,
                            api_key=gem_key,
                            custom_query=query,
                        )
                    if result:
                        if use_or:
                            # A successful OpenRouter replacement has no Google receipt.
                            (self.state_dir / "deep_research_receipt.json").unlink(missing_ok=True)
                        (self.state_dir / "research_degraded.txt").unlink(missing_ok=True)
                        self.log(f"Deep Research completed ({label}): {result}", "INFO")
                        # Fold the provider-billed DR cost into the ledger as
                        # its own line item (deep_research.py writes the
                        # sidecar from OpenRouter's usage.cost).
                        try:
                            _cf = self.state_dir / "deep_research_cost.usd"
                            if _cf.exists():
                                _dr_cost = float(_cf.read_text().strip() or 0)
                                if _dr_cost > 0:
                                    self._agent_stats.append({
                                        "agent_type": "deep_research",
                                        "elapsed_seconds": 0, "prompt_len": 0,
                                        "output_len": 0, "model": "openrouter/deep-research",
                                        "input_tokens": 0, "output_tokens": 0,
                                        "cache_read_tokens": 0, "cache_creation_tokens": 0,
                                        "cost_usd": _dr_cost, "duration_api_ms": 0,
                                        "timestamp": datetime.now().isoformat(),
                                    })
                                    self._write_cost_report()
                                _cf.unlink(missing_ok=True)
                        except Exception:
                            pass
                        self._send_deep_research_telegram(result)
                    else:
                        if use_or or use_gem:
                            self.log("Deep Research returned no result.", "WARN")
                        if not dr_file.is_file() or not dr_file.stat().st_size:
                            self._fallback_literature_survey(query)
                except Exception as e:
                    self.log(f"Deep Research failed: {e}", "WARN")
                    if not dr_file.is_file() or not dr_file.stat().st_size:
                        self._fallback_literature_survey(query)
            else:
                self.log("No Deep Research key (OpenRouter/Gemini) — skipping", "WARN")

            self.log_step_header(2, 4, f"Deep Research ({label})", "end")
        else:
            self.log_step("Deep research report exists, skipping", "info")

        # ── Step 3: Specialization ──────────────────────────────────────
        # Always sync agent prompt bases from templates first, regardless of
        # whether the project has already been initialized. Edits to
        # `ark/templates/agents/*.prompt` need to reach already-specialized
        # projects on every Continue, not just on first init. The sync
        # preserves any '## Project-Specific Knowledge' addendum that
        # _specialize_agent_prompts appended below the base.
        self._sync_agent_prompt_bases()

        ctx_file = self.state_dir / "project_context.md"
        if not ctx_file.exists():
            self.log_step_header(3, 4, "Specialization")

            # 3.1: Generate project_context.md (web-verified)
            self.log_step("Generating project context (web-verified)...", "progress")
            # Gate B (idea-quality assessment) is folded into this call because it
            # already loads idea.md + deep_research.md — no extra LLM call. When
            # Deep Research did not run (no gemini key / skip_deep_research), the
            # report is absent; degrade gracefully and never block on it.
            dr_exists = (self.state_dir / "deep_research.md").exists()
            dr_note = (
                "Use your Read tool to load BOTH files in full."
                if dr_exists else
                "Use your Read tool to load `auto_research/state/idea.md` in "
                "full. NOTE: no Deep "
                "Research report is available for this run — assess novelty "
                "conservatively from the idea alone and do not over-claim prior art."
            )
            self.run_agent("researcher", f"""
Read the idea summary and deep research report, then generate a verified
project context document.

## Source Material (MANDATORY — Read in full before writing)
- `auto_research/state/idea.md` — the research idea (user-authored)
- `auto_research/state/deep_research.md` — the Gemini Deep Research report{" (MAY BE ABSENT — see note)" if not dr_exists else ""}

{dr_note} Do NOT skim or guess their contents — writing the context document
without consulting them produces hallucinated systems and broken install
instructions.

## Your Task

For EACH external system mentioned in those files, you MUST search the web
to verify:
- What it actually is (do NOT guess from name)
- Official URL and repository
- Correct install command (MUST be project-isolated — never global installs)
- Key CLI commands or API usage for experiments

Write `auto_research/state/project_context.md` with sections:
## External Systems, ## Environment Setup, ## Experiment Guidance, ## Credentials & Access
For Environment Setup, record the experiment worker command and identify which
package versions were actually verified there. Keep pending worker checks explicit.

## Idea Assessment (grounded in the literature above — advisory, NEVER refuse to write)
Append these sections to project_context.md. The goal is to SHARPEN the project,
not to kill it; be honest but constructive.

### Novelty & Prior Art
- Is this idea, or something essentially equivalent, already solved in the
  literature? State exactly ONE verdict token: NOVEL_ENOUGH / PARTIAL_OVERLAP / ESSENTIALLY_SOLVED.
- If PARTIAL_OVERLAP or ESSENTIALLY_SOLVED, cite the closest 1-3 works from the
  report and say in one sentence what each already did.
- If the literature is thin or absent, write "insufficient literature to assess
  novelty" and continue with the rest from the idea alone.

### Our Contribution
- One crisp paragraph: given the prior art above, the specific, defensible delta
  THIS project adds. If you wrote ESSENTIALLY_SOLVED, state the residual delta
  honestly (it may be small, or mainly replication/engineering).

### Narrowed Research Questions
- 2-4 concrete, testable research questions a SMALL team could answer, derived
  from the idea and the gaps the report reveals.

### Scope Recommendation
- If the idea as stated is too broad for a small group, propose ONE narrowed
  version (the single most promising research question above) that is achievable,
  and say what to cut.
""", timeout=defaults.TIMEOUT_INITIALIZER)
            self.log_step("Project context generated", "success")
            self.notify_progress("Project context", "ready", level="done")
            # Gate B: surface the idea assessment to the user (advisory only).
            self._notify_idea_assessment(ctx_file)

            # 3.2: Specialize agent prompts (code-driven, one call per agent)
            # _sync_agent_prompt_bases already ran above (unconditionally),
            # so the per-project prompt files reflect the latest template
            # before specialization is appended.
            self.log_step("Specializing agent prompts...", "progress")
            self.notify_progress(
                "Agent prompts", "specializing for this project...", level="working"
            )
            self._specialize_agent_prompts()

            # 3.3: Select and install skills
            self.log_step("Selecting skills...", "progress")
            self.notify_progress(
                "Skills", "picking from library...", level="working"
            )
            skills_index = self._load_skills_index()
            if skills_index:
                self.run_agent("researcher", f"""
Select skills from the library that will be useful across this project —
implementation, paper writing, reviewing, and any supporting phase.

## Selection Rules
- Select skills for methods/tools/frameworks the project will actually BUILD,
  RUN, or WRITE ABOUT. Implementation skills, training/eval frameworks, writing
  and venue-specific skills, citation/figure/plot skills — all in scope.
- Do NOT select skills just because a topic is MENTIONED as a benchmark or
  baseline. Example: if the project EVALUATES on RL environments but does NOT
  train RL agents, do NOT select RL training skills.
- Typical project picks 3–10 skills. More than 15 is almost always
  over-selection — each extra skill pollutes every downstream agent's context.
  Zero is acceptable if nothing matches.
- When in doubt, leave it out.

## How to Select
Search the project-local catalog below for relevant methods and tools. Each
entry includes an excerpt from the installed SKILL.md. Read only project files.
The path field is a selection ID: copy it verbatim, do not open or search that
directory. Leave out skills whose excerpt does not support a specific use.
After selection, the installer makes the complete skills available in the
project. Builtins and cluster-specific guidance are supplied automatically.

## Source Material (MANDATORY — Read before selecting)
- `auto_research/state/project_context.md` — verified external systems, env setup, experiment guidance
- `auto_research/state/idea.md` — the raw research idea

Read both files to understand what this project will actually build, run, and
write about. Selection must be grounded in those files, not in the catalog.

## Skills Library
{skills_index}

Write `auto_research/state/selected_skills.json` containing a JSON array of
selected skill paths (or an empty array `[]` if nothing matches). Also write
`auto_research/state/selected_skills_rationale.md` with a short rationale per
selected skill (why it matches, which phase will use it).
""", timeout=defaults.TIMEOUT_INITIALIZER)
            n_skills = self._install_selected_skills() if skills_index else 0
            self.log_step("Specialization complete", "success")
            if isinstance(n_skills, int) and n_skills >= 0:
                self.notify_progress(
                    "Skills installed", f"{n_skills} skill(s) loaded", level="done"
                )
            else:
                self.notify_progress("Skills installed", "ready", level="done")

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
        if self.telegram.is_configured:
            self.telegram.send(
                f"{self.tg_header('🚤')}\n"
                f"🏁 <b>Research Phase complete</b> → moving to Dev Phase",
                parse_mode="HTML",
            )

    def _load_skills_index(self) -> str:
        """Create a searchable catalog of available optional skills for this project."""
        skills_root = Path(__file__).parent.parent / "skills"
        index_path = skills_root / "index.json"
        try:
            entries = json.loads(index_path.read_text())
        except (OSError, ValueError):
            return ""

        if not isinstance(entries, list):
            return ""
        available = []
        for entry in entries:
            # Shipped indexes may contain paths from the checkout that built them.
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            parts = Path(entry["path"]).parts
            if "library" not in parts:
                continue
            relative = Path(*parts[parts.index("library") + 1:])
            if not relative.parts or ".." in relative.parts:
                continue
            if relative == Path("hpc/rocs-testbed-slurm"):
                # SlurmComputeBackend supplies this guide when it detects ROCS.
                continue
            directory = skills_root / "library" / relative
            skill_file = directory / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                with skill_file.open(encoding="utf-8", errors="replace") as stream:
                    excerpt = stream.read(1536).strip()
            except OSError:
                continue
            if excerpt:
                available.append({"name": entry.get("name", directory.name),
                                  "path": str(directory), "excerpt": excerpt})

        if not available:
            return ""
        catalog = self.state_dir / "available_skills.jsonl"
        with catalog.open("w") as stream:
            for entry in available:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return f"Search `{catalog}` for installed skill names and excerpts."

    def _install_selected_skills(self) -> int:
        """Copy selected skills to the project directory. Returns count installed."""
        import json
        selected_file = self.state_dir / "selected_skills.json"
        if not selected_file.exists():
            return 0

        try:
            with open(selected_file) as f:
                selected_paths = json.load(f)

            if not isinstance(selected_paths, list):
                return 0

            skills_dest = Path(self.code_dir) / ".claude" / "skills"
            skills_dest.mkdir(parents=True, exist_ok=True)

            # Older projects store their selections without a local catalog.
            catalog = self.state_dir / "available_skills.jsonl"
            offered = None
            if catalog.is_file():
                offered = {json.loads(line)["path"] for line in catalog.read_text().splitlines()}
            installed = []
            for skill_path in selected_paths:
                if offered is not None and skill_path not in offered:
                    continue
                src = Path(skill_path)
                if src.exists() and (src / "SKILL.md").exists():
                    dest = skills_dest / src.name
                    if not dest.exists():
                        import shutil
                        shutil.copytree(src, dest)
                        installed.append(src.name)

            if installed:
                self.log_step(f"Installed {len(installed)} skills: {', '.join(installed)}", "success")
            return len(installed)
        except Exception as e:
            self.log(f"Skills installation failed: {e}", "WARN")
            return 0

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
        """Handle an agent's ``results/needs_human.json`` blocker."""
        needs_file = Path(self.code_dir) / "results" / "needs_human.json"
        if not needs_file.exists():
            return False
        try:
            raw = json.loads(needs_file.read_text())
        except Exception as e:
            self.log(f"needs_human.json unreadable, skipping HITL: {e}", "WARN")
            needs_file.unlink(missing_ok=True)
            return False

        req = _normalise_needs_human(raw)
        self.log(f"Human intervention requested [urgency={req['urgency']}]: {req['summary'] or '(no summary)'}", "WARN")

        if req["urgency"] in _NONBLOCKING_URGENCIES:
            return self._handle_nonblocking_hitl(req, stage, needs_file)

        # A blocker with no summary, no failure detail, no evidence, and no
        # explicit options carries nothing a human could act on — it surfaces as
        # the useless "Agent blocked at <stage>" with an empty body. Never put
        # that in front of the user: HITL prompts must have substance. Log it,
        # tell the chat, and continue automatically.
        has_detail = bool(
            req["summary"] or req["what_failed"]
            or req["evidence"].get("error_output")
            or req["evidence"].get("tested_commands")
            or req["options"]
        )
        if not has_detail:
            self.log("needs_human.json had no actionable detail (no summary / error / "
                     "options) — auto-continuing instead of blocking the human.", "WARN")
            try:
                self._chat("agent",
                           "An agent briefly flagged it needed input but gave no detail to act on — continuing automatically.",
                           kind="notice")
            except Exception:
                pass
            needs_file.unlink(missing_ok=True)
            return False

        # Derive ask_user_decision inputs
        options = [o["title"] or o["id"] for o in req["options"]] or ["Continue without action", "Pause and wait for me"]
        option_details = [o["consequence"] for o in req["options"]] or ["Skip the blocker.", "Hold until guidance."]

        background = self._build_hitl_background(req)
        default_idx = 0
        if req["default_option"] and req["options"]:
            for i, o in enumerate(req["options"]):
                if o["id"] == req["default_option"]:
                    default_idx = i
                    break

        # Build a question with substance: prefer the agent's summary, then the
        # concrete failure, then a stage-qualified line — never a bare "blocked".
        question = (req["summary"] or req["what_failed"]
                    or f"An agent needs your input at {stage or 'this step'}")
        idx, reply = self.ask_user_decision(
            question,
            options, timeout=req["timeout_minutes"] * 60, default=default_idx,
            what_happened=req["summary"] or req["what_failed"], background=background,
            option_details=option_details, phase="needs_human",
        )

        chosen, decision_text = self._resolve_hitl_decision(idx, reply, req)
        
        try: _append_hitl_history(Path(self.code_dir), req, reply, chosen, decision_text, stage)
        except Exception as e: self.log(f"history append failed: {e}", "WARN")
        try: _update_hitl_decisions(Path(self.state_dir), req, chosen, decision_text, stage)
        except Exception as e: self.log(f"decisions update failed: {e}", "WARN")

        needs_file.unlink(missing_ok=True)
        return bool(reply) or chosen is not None

    def _handle_nonblocking_hitl(self, req: dict, stage: str, needs_file: Path) -> bool:
        parts = [f"Auto-acknowledged (urgency={req['urgency']}, not blocking)."]
        if req["fallbacks"]:
            parts.append("Using documented fallbacks:")
            parts.extend(f"  - {fb}" for fb in req["fallbacks"])
        else:
            parts.append("No explicit fallback documented — downstream will proceed without requested input.")
        decision_text = "\n".join(parts)
        self.log(f"HITL non-blocking ack ({req['urgency']}): {req['summary'][:120] or '(no summary)'}", "INFO")
        
        try:
            self.telegram.send_async(
                f"ℹ️ <b>{_html.escape(self.display_name)}</b>: agent logged a <i>{_html.escape(req['urgency'])}</i> need but proceeded with fallback. No action required.\n\n<b>Summary:</b> {_html.escape(req['summary'][:300] or '(no summary)')}",
                parse_mode="HTML", polish=False,
            )
        except Exception as e: self.log(f"HITL async notify failed: {e}", "WARN")
        
        try: _append_hitl_history(Path(self.code_dir), req, None, None, decision_text, stage)
        except Exception: pass
        try: _update_hitl_decisions(Path(self.state_dir), req, None, decision_text, stage)
        except Exception: pass
        
        needs_file.unlink(missing_ok=True)
        return True

    def _build_hitl_background(self, req: dict) -> list[str]:
        background = []
        if req["stage"]: background.append(f"Stage: {req['stage']}")
        if req["what_failed"]: background.append(f"What failed: {req['what_failed'][:400]}")
        for cmd in (req["evidence"].get("tested_commands") or [])[:4]:
            if isinstance(cmd, dict):
                c = cmd.get("cmd") or cmd.get("command") or ""
                rc = cmd.get("exit_code")
                background.append(f"Tested: {str(c)[:120]}" + (f" (exit {rc})" if rc is not None else ""))
            else: background.append(f"Tested: {str(cmd)[:120]}")
        err = req["evidence"].get("error_output")
        if err: background.append(f"Error: {str(err)[:300]}")
        return background

    def _resolve_hitl_decision(self, idx: int, reply: str, req: dict) -> tuple[Optional[dict], str]:
        chosen = None
        decision_text = ""
        if 0 <= idx < len(req["options"]):
            chosen = req["options"][idx]
            decision_text = f"Selected option {chosen['id']}: {chosen['title']}"
            if chosen["consequence"]: decision_text += f" (consequence: {chosen['consequence']})"
            try: self.inject_user_update(decision_text)
            except Exception: pass
            self.log(f"HITL decision: {decision_text[:120]}", "INFO")
        elif reply:
            decision_text = reply.strip()
            self.log(f"HITL free-text reply: {decision_text[:120]}", "INFO")
        else:
            self.log(f"No HITL reply after {req['timeout_minutes']}min — experiments remain blocked.", "WARN")
        return chosen, decision_text

    def _sync_agent_prompt_bases(self):
        """Refresh the base template of each per-project agent prompt,
        preserving any specialization addendum that was appended below.

        Per-project prompts live at ``<project>/agents/<agent>.prompt``.
        They are seeded from ``ark/templates/agents/`` at project creation
        (or webapp restart) and then ``_specialize_agent_prompts`` appends
        a ``## Project-Specific Knowledge`` section. Without this sync,
        any edit to the template in ``ark/`` is invisible to an existing
        project on Continue — the per-project prompt was frozen at the
        version that shipped when the project was specialized.

        Strategy: split each existing prompt at the specialization marker,
        re-read the (now-edited) template, re-apply variable substitutions
        using values extracted from the current prompt, then concat the
        addendum. If no addendum is present yet, skip — the prompt hasn't
        been specialized on this project yet and the seeding code owns it.
        """
        agents_dir = getattr(self, "agents_dir", None)
        if not agents_dir or not agents_dir.exists():
            return
        templates_dir = Path(__file__).parent / "templates" / "agents"
        if not templates_dir.exists():
            return

        MARKER = "## Project-Specific Knowledge"

        # Values to re-substitute come from config (same values the webapp
        # used at seeding time). Fall back to whatever we can infer.
        title = self.config.get("title") or self.config.get("project") or ""
        venue_name = (
            self.config.get("venue")
            or self.config.get("venue_format")
            or "NeurIPS"
        )
        venue_format = self.config.get("venue_format") or "neurips"
        venue_pages = str(self.config.get("venue_pages", 9))
        project_id = self.config.get("project") or getattr(self, "project_id", "")

        try:
            from ark.template_preprocess import render_custom_template_notes
            custom_notes = render_custom_template_notes(
                Path(self.code_dir) / "paper"
            )
        except Exception:
            custom_notes = ""

        refreshed = 0
        for tpl_path in templates_dir.glob("*.prompt"):
            per_path = agents_dir / tpl_path.name
            if not per_path.exists():
                continue
            per = per_path.read_text()
            if MARKER not in per:
                # Prompt hasn't been specialized yet for this project.
                # Let the regular seeding + specialization path handle it.
                continue
            addendum = per[per.index(MARKER):]
            base = tpl_path.read_text()
            for k, v in {
                "{PROJECT_NAME}": project_id,
                "{PAPER_TITLE}": title or project_id,
                "{VENUE_NAME}": venue_name,
                "{VENUE_FORMAT}": venue_format,
                "{VENUE_PAGES}": venue_pages,
                "{LATEX_DIR}": "paper",
                "{FIGURES_DIR}": "paper/figures",
                "{CUSTOM_TEMPLATE_NOTES}": custom_notes,
            }.items():
                base = base.replace(k, v)
            new_content = base.rstrip() + "\n\n" + addendum
            if new_content != per:
                per_path.write_text(new_content)
                refreshed += 1
        if refreshed:
            self.log(
                f"Refreshed {refreshed} agent prompt base(s) from templates",
                "INFO",
            )

    def _specialize_agent_prompts(self):
        """Specialize each agent's prompt with project-specific knowledge.

        For each agent (except researcher itself), calls the researcher
        to generate a '## Project-Specific Knowledge' section, then appends it to
        the agent's prompt file. Verifies the append succeeded.

        The researcher agent has Read access and is instructed to load
        ``auto_research/state/project_context.md`` itself — we no longer
        pre-load or truncate the context into the prompt.
        """
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

## Project Context (MANDATORY — Read before composing the section)
Use Read to load `auto_research/state/project_context.md` in full. It has
the verified external systems, install commands, environment setup, and
experiment guidance for this project. Ground the specialization section
in what that file actually says — do not guess. For Python commands and package
versions, apply the current experiment worker instructions. Carry pending worker
checks forward without presenting them as verified.

## Focus Areas for {agent_name}
{focus}

## Rules
- Output ONLY the "## Project-Specific Knowledge" section content (with the heading)
- Be concise but comprehensive (200-400 words)
- Include specific tool names, commands, URLs, and technical details
- For experimenter: emphasize project-isolated installs and checking existing services
- For writer: include anonymity rules (no author names in title or text for blind review)
- Do NOT repeat generic instructions already in the agent's base prompt
""", timeout=defaults.TIMEOUT_AGENT_SPECIALIZE)

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
            # Title already committed, but main.tex / agent prompts may have
            # drifted (e.g. restart after template preprocess stubs the title
            # to "ARK Pending Title"). Sync is idempotent and cheap.
            self._sync_paper_metadata(current)
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
            # Fill in the empty ``**Paper Title**:`` slot in goal_anchor if the
            # project was created before the title existed. Don't clobber a
            # goal_anchor that already carries a real title.
            goal = cfg.get("goal_anchor") or ""
            if goal:
                cfg["goal_anchor"] = re.sub(
                    r'(\*\*Paper Title\*\*:[ \t]*)(\n|$)',
                    lambda m: f"{m.group(1)}{new_title}{m.group(2)}",
                    goal,
                    count=1,
                )
            config_path.write_text(
                yaml.dump(cfg, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            )
        self._sync_db(title=new_title, name=new_title)
        self.log(f"Title committed: {new_title}", "INFO")
        # The header in every future Telegram message will now show the
        # real title — drop the cache so display_name picks it up. Wrap
        # both calls in fail-soft guards: test harnesses may stub this
        # mixin with ``MagicMock(spec=PipelineMixin)``, which won't carry
        # ``_invalidate_display_name`` / ``notify_progress`` (both live
        # on the Orchestrator itself), and a non-essential notification
        # must never break the pipeline.
        try:
            self._invalidate_display_name()
        except Exception:
            pass
        try:
            self.notify_progress("Title generated", new_title[:80], level="done")
        except Exception:
            pass

        # --- Propagate title to paper/main.tex and agent prompts ---
        self._sync_paper_metadata(new_title)

    def _sync_paper_metadata(self, title: str):
        """Push the canonical title into ``paper/main.tex`` and agent prompts.

        Called after ``_update_title_from_idea`` commits a new title, so the
        LaTeX ``\\title{...}`` and the writer/reviewer prompts all agree with
        ``config.yaml``. Without this sync, templates ship with their own
        placeholder title (e.g. ``Formatting Instructions For NeurIPS 2026``)
        which would otherwise survive the whole pipeline.
        """
        # 1. Rewrite \title{...} in the main LaTeX file.
        main_tex = self.latex_dir / "main.tex"
        if main_tex.exists():
            try:
                src = main_tex.read_text()
                if src and title:
                    new_src = latex_utils.replace_latex_title(src, title)
                    if new_src != src:
                        main_tex.write_text(new_src)
                        self.log(f"Synced paper title in main.tex → {title}", "INFO")
            except Exception as e:
                self.log(f"Failed to sync main.tex title: {e}", "WARN")

        # 2. Re-render agent prompts from templates so {PAPER_TITLE} is current.
        templates_dir = Path(__file__).parent / "templates" / "agents"
        agents_dir = self.agents_dir
        if not (templates_dir.exists() and agents_dir.exists()):
            return
        project_id = self._project_id or self.project_name
        venue_format = self.config.get("venue_format") or "neurips"
        venue_name = (
            self.config.get("venue")
            or self.config.get("venue_name")
            or venue_format
            or "NeurIPS"
        )
        venue_pages = self.config.get("venue_pages", 9)
        latex_dir = self.config.get("latex_dir", "paper")
        figures_dir = self.config.get("figures_dir", f"{latex_dir}/figures")

        # Custom-template notes: empty string for projects without a
        # template_manifest.yaml so the placeholder doesn't leak into the
        # rendered prompt.
        try:
            from ark.template_preprocess import render_custom_template_notes
            custom_notes = render_custom_template_notes(self.latex_dir)
        except Exception as e:
            self.log(f"Failed to render custom template notes: {e}", "WARN")
            custom_notes = ""

        subs = {
            "{PROJECT_NAME}": project_id,
            "{PAPER_TITLE}": title or project_id,
            "{VENUE_NAME}": venue_name,
            "{VENUE_FORMAT}": venue_format,
            "{VENUE_PAGES}": str(venue_pages),
            "{LATEX_DIR}": latex_dir,
            "{FIGURES_DIR}": figures_dir,
            "{CUSTOM_TEMPLATE_NOTES}": custom_notes,
        }
        try:
            for pf in templates_dir.glob("*.prompt"):
                content = pf.read_text()
                for placeholder, value in subs.items():
                    content = content.replace(placeholder, value)
                (agents_dir / pf.name).write_text(content)
            self.log(f"Refreshed {len(list(templates_dir.glob('*.prompt')))} agent prompts with new title", "INFO")
        except Exception as e:
            self.log(f"Failed to refresh agent prompts: {e}", "WARN")

    # ==================== Citation Bootstrapping ====================

    def _merge_template_bibs_into_references(self):
        r"""Merge entries from venue-template .bib files into references.bib.

        Many venues (ACL/EMNLP/NAACL via acl-style-files, NeurIPS via
        the neurips template) ship a starter `.bib` file under a
        non-canonical name — `custom.bib`, `references.bib`,
        `mybib.bib`, etc. The writer agent is told (via
        ``ark/templates/agents/writer.prompt``) to use keys from
        ``references.bib``. ARK's citation system writes auto-fetched
        entries to ``references.bib``. The venue's starter file is
        ignored by both, even though ``\bibliography{custom}`` in the
        rendered main.tex reads it. Result: writer sees an empty
        ``references.bib``, concludes "no keys available", emits zero
        ``\cite{}`` calls, and the final paper renders with an empty
        References section.

        This routine consolidates the situation: read every other
        ``.bib`` file in the LaTeX directory, parse out its
        ``@<type>{<key>, ...}`` entries, and append any entries whose
        key is NOT already present in ``references.bib``. The original
        venue-template ``.bib`` files are left in place so the venue
        template's example ``\bibliography{custom}`` keeps working,
        but every key is now also reachable via ``references.bib``.

        The merge is idempotent (re-running adds no duplicates) and
        non-destructive (only ``references.bib`` is modified).
        """
        import re as _re

        latex_dir = getattr(self, "latex_dir", None)
        if latex_dir is None or not latex_dir.exists():
            return

        refs_path = latex_dir / "references.bib"
        if not refs_path.exists():
            refs_path.write_text("% ARK auto-managed references\n\n")

        existing_text = refs_path.read_text(errors="replace")
        # @<type>{<key>, — capture key
        entry_re = _re.compile(r'@\w+\s*\{\s*([^,\s}]+)', _re.MULTILINE)
        existing_keys = set(entry_re.findall(existing_text))

        # Find sibling .bib files (skip references.bib itself and any
        # *.bib.txt note files like ACL's anthology.bib.txt).
        merged_keys: list[str] = []
        merged_from: list[str] = []
        for sibling in sorted(latex_dir.glob("*.bib")):
            if sibling.name == "references.bib":
                continue
            try:
                text = sibling.read_text(errors="replace")
            except Exception:
                continue

            # Walk top-level entries by tracking brace depth so multi-line
            # entries are captured intact and entry boundaries are correct.
            entries = self._split_top_level_bib_entries(text)
            new_block = []
            for entry in entries:
                m = entry_re.match(entry)
                if not m:
                    continue
                key = m.group(1)
                if key in existing_keys:
                    continue
                new_block.append(entry.strip())
                existing_keys.add(key)
                merged_keys.append(key)
            if new_block:
                merged_from.append(sibling.name)
                existing_text = (
                    existing_text.rstrip() +
                    f"\n\n% Merged from {sibling.name} ({len(new_block)} entries)\n\n" +
                    "\n\n".join(new_block) + "\n"
                )

        if merged_keys:
            refs_path.write_text(existing_text)
            self.log(
                f"Merged {len(merged_keys)} bib entries into references.bib "
                f"from {', '.join(merged_from)}: {', '.join(merged_keys[:8])}"
                + ("..." if len(merged_keys) > 8 else ""),
                "INFO",
            )

    @staticmethod
    def _split_top_level_bib_entries(text: str) -> list[str]:
        """Split a .bib file body into top-level @entry{...} chunks.

        Naive `re.findall(r'@\\w+\\{[^}]*\\}', ...)` misses entries with
        nested braces (e.g., titles like ``{The {ACL} 2026 Special
        Theme}``). Walk the brace structure manually instead.
        """
        from ark.citation import bib_entry_spans

        return [text[start:end] for start, end in bib_entry_spans(text)]

    def _deep_research_route(self, or_key: str, gem_key: str):
        """``(use_openrouter, use_gemini, spent_balance)`` for Deep Research.

        Default (auto): OpenRouter when its key is present, else Gemini.
        ``deep_research_backend`` (openrouter|gemini|auto) can force one. A run
        on a free model can still hold a spent OpenRouter key, and
        sonar-deep-research is paid, so the call would 402 and fall back
        anyway. When the balance is confirmed too low, OpenRouter is skipped:
        Gemini if its key exists, else the caller runs the direct literature
        search. ``spent_balance`` is that balance, or None when not spent.
        """
        backend = (self.config.get("deep_research_backend", "auto") or "auto").lower()
        use_or = bool(or_key) and backend in ("auto", "openrouter")
        spent = None
        if use_or:
            from ark.provider_billing import openrouter_too_low_for_side_tasks
            spent = openrouter_too_low_for_side_tasks(or_key)
            if spent is not None:
                self.log(f"OpenRouter balance is ${spent:.2f}: not running the "
                         f"paid Deep Research call on it", "WARN")
                use_or = False
        use_gem = bool(gem_key) and (
            backend == "gemini"
            or (not use_or and (backend == "auto" or spent is not None)))
        return use_or, use_gem, spent

    def _fallback_literature_survey(self, query: str) -> bool:
        """Build a minimal literature base from academic search when Deep
        Research fails, so a paper is never written on an empty foundation.

        Deep Research failure used to be a WARN that the pipeline walked
        straight past: the writer then produced a full paper with zero
        literature grounding and nobody was told (2026-08-06, and the same
        root shape as the recurring missing-references bug). The provider is
        the flaky part, not the idea of grounding — and we already talk to
        DBLP/CrossRef/arXiv/S2 for citations, so fall back to those.

        The result is deliberately marked as degraded, both in the report
        itself and via ``research_degraded.txt``, which the delivery contract
        reads. A thinner foundation is acceptable; an INVISIBLE one is not.
        """
        try:
            from ark.citation import (looks_like_paper, search_papers,
                                      search_queries_from_topic, title_on_topic,
                                      topic_terms)
        except Exception as e:  # noqa: BLE001
            self.log(f"Literature fallback unavailable: {e}", "WARN")
            return False

        self.log_step("Deep Research unavailable — falling back to a direct "
                      "literature search (DBLP/CrossRef/arXiv/S2)...", "progress")

        # `query` is the Deep Research PROMPT — prose written for an LLM, with
        # markdown headers, LaTeX and full sentences. Sending it (even truncated)
        # to keyword search engines made CrossRef fuzzy-match incidental words
        # and return 12/12 topically unrelated papers on a real run: dentistry,
        # civil engineering, veterinary ophthalmology, plus three CrossRef
        # *figure* records. Ask short, keyword-shaped questions instead, and
        # keep the project's own idea in the topic corpus — its Related Work
        # names real prior papers, the most precise query material there is.
        idea = self.config.get("research_idea") or self.config.get("goal_anchor") or ""
        title = self.config.get("title") or ""
        queries = search_queries_from_topic(f"{query}\n{idea}", title, limit=4)
        if not queries:                      # nothing usable — last resort
            queries = [" ".join((title or query).split()[:8])]
        # The relevance backstop needs a vocabulary to compare against. With a
        # one-line topic ("transformers") it would have almost no terms and
        # would reject everything — turning a thin survey into NO survey, which
        # is the worse failure. A gate that cannot discriminate must not judge.
        terms = topic_terms(f"{title}\n{query}\n{idea}")
        gate_on = len(terms) >= 6
        if not gate_on:
            self.log(f"Literature fallback: topic too thin for a relevance "
                     f"filter ({len(terms)} distinctive terms) — keeping all hits",
                     "WARN")

        papers, seen_titles, dropped = [], set(), 0
        for q in queries:
            try:
                hits = search_papers(q, max_results=8)
            except Exception as e:  # noqa: BLE001
                self.log(f"Literature fallback query {q!r} failed: {e}", "WARN")
                continue
            for p in hits:
                key = (p.title or "").strip().lower()
                if not key or key in seen_titles:
                    continue
                seen_titles.add(key)
                # Indexes carry non-papers: CrossRef figure/table records,
                # Springer reference-work term stubs ("laser spectral width",
                # no author, no year). They keyword-match perfectly and are not
                # citable.
                if not looks_like_paper(p.title, getattr(p, "authors", None),
                                        getattr(p, "year", None)):
                    dropped += 1
                    continue
                # Backstop: a keyword query is far more precise than prose, but
                # CrossRef can still drag in an unrelated field. Require the hit
                # to share distinctive terms with the topic.
                if gate_on and not title_on_topic(p.title, terms):
                    dropped += 1
                    continue
                papers.append(p)
            if len(papers) >= 12:
                break
        papers = papers[:12]
        self.log(f"Literature fallback: {len(queries)} queries "
                 f"({', '.join(repr(q) for q in queries)}), kept {len(papers)} "
                 f"on-topic, dropped {dropped} off-topic", "INFO")

        if not papers:
            # Still no grounding: record it so the run is auditable and the
            # delivery contract can flag the paper rather than pretend.
            (self.state_dir / "research_degraded.txt").write_text(
                "Deep Research failed and the literature-search fallback found "
                f"nothing on-topic ({dropped} off-topic hits discarded). "
                "This paper has no literature grounding.\n")
            self.log("Literature fallback found no on-topic papers — the paper "
                     "will have NO literature grounding", "ERROR")
            return False

        lines = [
            "# Literature Survey (fallback)",
            "",
            "> Deep Research was unavailable for this run, so this survey was "
            "assembled directly from academic search APIs "
            "(DBLP / CrossRef / arXiv / Semantic Scholar). It lists real, "
            "verifiable work but is THINNER than a full Deep Research report: "
            "no synthesis, no methodology section. Treat coverage as partial.",
            "",
            "Search queries used: " + "; ".join(f"`{q}`" for q in queries),
            "",
            "## Related Work",
            "",
        ]
        for i, p in enumerate(papers, 1):
            authors = ", ".join((getattr(p, "authors", None) or [])[:3])
            venue = getattr(p, "venue", "") or ""
            year = getattr(p, "year", "") or ""
            lines.append(f"{i}. **{p.title}**" + (f" — {authors}" if authors else ""))
            if venue or year:
                lines.append(f"   - {venue} {year}".rstrip())
            if getattr(p, "abstract", None):
                lines.append(f"   - {p.abstract[:300]}")
            lines.append("")

        (self.state_dir / "deep_research.md").write_text("\n".join(lines))
        (self.state_dir / "research_degraded.txt").write_text(
            f"Deep Research failed; literature assembled from search APIs "
            f"({len(papers)} on-topic papers kept, {dropped} off-topic hits "
            f"discarded). No synthesis, and coverage is narrower than a full "
            f"survey — verify the related-work framing before trusting it.\n")
        self.log_step(f"Literature fallback: {len(papers)} real on-topic papers "
                      f"found (report is thinner than Deep Research)", "success")
        return True

    def _bootstrap_citations_from_deep_research(self, *, recheck_keys=()):
        r"""Extract paper titles from Deep Research report via LLM, then fetch BibTeX via API.

        1. Merge any venue-template .bib files (e.g. ACL's `custom.bib`)
           into `references.bib` so the writer prompt's "use keys from
           references.bib" rule actually surfaces the template's
           starter citations. Without this merge, ARK and the venue
           template each maintain a separate .bib file and the writer
           sees a near-empty `references.bib` because the entries live
           in `custom.bib` — that has caused multiple final papers to
           ship with `\cite{}` count = 0 and an empty References page.
        2. LLM reads the report and extracts paper titles as JSON list
        3. Each title is searched via DBLP/CrossRef/arXiv/S2
        4. Found papers get official BibTeX written to references.bib
        5. Not-found titles get a keyword retry, then [NEEDS-CHECK] + Telegram notification
        """
        from ark.citation import bootstrap_citations

        # Step 0: merge venue-template .bib files into references.bib.
        # Idempotent: re-running on a file that already has all template
        # entries is a no-op.
        self._merge_template_bibs_into_references()

        deep_research_file = self.state_dir / "deep_research.md"
        if not deep_research_file.exists():
            return

        bib_path = str(self.latex_dir / "references.bib")
        literature_path = str(self.state_dir / "literature.yaml")

        self.log_step("Extracting citations from Deep Research report...", "progress")

        # Step 1: LLM extracts paper titles from the report
        extract_prompt = """Identify the primary papers supporting or challenging this project's claims,
including its closest prior work, methods, dataset and evaluation. Exclude unrelated tools mentioned
in passing. Verify the exact title and authors against the original source linked in the report.
If a claim cannot be traced to its original paper, leave it out of the verified list.

## Source Material (MANDATORY — Read in full before extracting)
- `auto_research/state/deep_research.md` — the report

Use Read to load the file in full. Do NOT work from memory or a partial read —
missing the second half means missing half the citations.

For each paper, return a JSON object with these fields:
- "title": the paper's actual full title
- "authors": first author surname (e.g. "Vaswani"). If the report does NOT name
  an author, leave this as an empty string `""` — NEVER write "Unknown", "TBD",
  "Anonymous", or any other placeholder. Placeholder names get written verbatim
  into the bibliography and a reviewer will treat them as an unfinished manuscript.
- "year": publication year as integer (e.g. 2017). Use 0 if the year is not given.
- "doi": exact DOI for THIS work when the report identifies it. Leave it empty if uncertain. Never copy a related work's DOI from the surrounding claim.
- "query": a search query to find it (title + author + year)
- "context": a 1-2 sentence summary of what the supported finding, its original section/table or official URL when supplied, and the limits of applying it here

Return a JSON array. Example:
[
  {"title": "Attention Is All You Need", "authors": "Vaswani", "year": 2017, "query": "Attention Is All You Need Vaswani 2017", "context": "Introduces the Transformer architecture based solely on attention mechanisms, replacing recurrence and convolutions."},
  {"title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding", "authors": "Devlin", "year": 2019, "query": "BERT Pre-training Deep Bidirectional Transformers Devlin 2019", "context": "Proposes bidirectional pre-training for language representations, achieving SOTA on multiple NLP benchmarks."}
]

Rules:
- "title" must be the paper's actual full title as it would appear on the paper itself
- "context" should summarize what the report says about this paper, NOT what you think the paper is about
- "query" should include the title plus first author surname and year to help search
- If only an abbreviation is given (e.g. "TimeGAN by Yoon et al., 2019"), infer the full title for "title" and construct a rich "query"
- Do NOT include book titles, dataset names, or tool names
- Do NOT invent papers not mentioned in the report
- If no papers are mentioned, return []
"""
        if recheck_keys:
            extract_prompt += (
                "\nResolve the manuscript passages citing these keys: " + ", ".join(recheck_keys)
                + ". The current records and survey may have shortened titles or misattributed an author. "
                  "Open the original paper or publisher metadata. Search by the method and specific finding "
                  "when its quoted title fails. Confirm the full title, lead author, official URL or DOI "
                  "and supporting passage. Include foundational or competing original work needed to "
                  "ground the claim. Cover the original method, benchmark and closest alternatives "
                  "where appropriate. The prior shortlist can be wrong or incomplete. Use the web or public "
                  "scholarly APIs to locate original work absent from the survey, including foundational "
                  "papers predating its recent examples. Distinguish these new sources in the context "
                  "field. Leave uncertain identities unresolved."
            )
        agent_output = self.run_agent("researcher", extract_prompt, timeout=defaults.TIMEOUT_CITATIONS_EXTRACT)

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
            contexts=contexts_list, dois=[p.get("doi", "") for p in papers_info],
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
        self.notify_progress(
            "Citations bootstrapped",
            f"{found}/{total} resolved, {missing} needs-check",
            level="done" if missing == 0 else "warn",
        )

        return result

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

        # Pull a JSON array out of the (possibly fenced/prosey) LLM text.
        arr = extract_json(text, want=list)
        if arr is not None:
            strs = [t for t in arr if isinstance(t, str) and len(t) > 5]
            if strs:
                return strs

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
                                "doi": p.get("doi", ""),
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
        """Run experiments when the project still needs an initial draft."""
        if self.config.get("skip_dev_phase", False):
            return False
        if self.load_paper_state().get("reviews"):
            return False
        if self._load_dev_phase_state()["status"] in ("complete", "completed"):
            return False

        # If paper already has substantial content, skip
        if self._paper_has_substantial_content():
            return False

        return True

    def _load_dev_phase_state(self) -> dict:
        """A missing checkpoint starts a project. An unreadable one must stop it."""
        path = self.state_dir / "dev_phase_state.yaml"
        try:
            with path.open() as stream:
                state = yaml.safe_load(stream)
        except FileNotFoundError:
            return {"iteration": 0, "status": "pending", "experiments": []}
        except yaml.YAMLError as exc:
            raise ValueError(f"Cannot resume experiments from malformed {path.name}") from exc
        # Check the fields present even before step-level resume was introduced.
        if (not isinstance(state, dict) or type(state.get("iteration")) is not int
                or state["iteration"] < 0
                or state.get("status") not in ("pending", "in_progress", "complete", "completed")):
            raise ValueError(f"Cannot resume experiments from incomplete {path.name}")
        return state

    def _save_dev_phase_state(self, state: dict):
        """Commit the new experiment checkpoint before publishing progress."""
        from ark.orchestrator.state import _atomic_write_yaml

        path = self.state_dir / "dev_phase_state.yaml"
        _atomic_write_yaml(path, state, default_flow_style=False, allow_unicode=True)
        # Sync to DB
        dev_status = state.get("status", "pending")
        phase = "dev" if dev_status == "in_progress" else ("review" if dev_status in ("completed", "complete") else "")
        self._sync_db(
            dev_iteration=int(state.get("iteration", 0)),
            dev_status=dev_status,
            phase=phase,
        )

    def _experiment_approval_gate(self):
        """Before running experiments, let the user approve the plan or steer it
        (大实验前把关). Gated by autonomy level — skipped in full_auto."""
        if not self._should_ask("experiment_approval"):
            return
        plan_summary = self._experiment_plan_digest().strip()
        if not plan_summary:
            # Older projects used Markdown plans.
            for name in ("experiment_plan.md", "experiments_plan.md", "plan.md", "action_plan.md"):
                try:
                    plan_summary = (self.state_dir / name).read_text()[:1200].strip()
                except (OSError, UnicodeError):
                    continue
                if plan_summary:
                    break
        if len(plan_summary) > 2400:
            plan_summary = plan_summary[:2399].rstrip() + "…"
        idx, reply = self.ask_user_decision(
            question="Experiment plan ready — approve and run it? (or type any changes)",
            options=["Approve — run the experiments as planned"],
            what_happened="The planner finished the experiment plan; about to execute it.",
            background=[plan_summary or "No readable plan summary is available."],
            timeout=defaults.TIMEOUT_HITL_DECISION, default=0,
            kind="experiment_approval", timeout_action="proceed_default",
            phase="experiment_approval",
        )
        # A free-text reply is an adjustment — persist it so the experimenter
        # (which now carries user_instructions) honors it on this very run.
        if reply and not str(reply).strip().isdigit():
            try:
                self.add_user_instruction(reply, source="experiment_gate")
                self.log("Applied your experiment adjustment.", "INFO")
            except Exception:
                pass

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
        dev_state, start_iter, max_dev_iters = self._begin_dev_phase()
        research_idea = self._research_idea
        self._run_experiment_loop(dev_state, start_iter, max_dev_iters, research_idea)
        if getattr(self, "_terminal_error", None):
            return
        self._start_initial_draft()
        self._generate_all_figures()
        self._write_initial_draft(research_idea)
        self._deliver_dev_phase(dev_state, max_dev_iters)

    def _begin_dev_phase(self):
        max_dev_iters = self.config.get("max_dev_iterations", 3)
        dev_state = self._load_dev_phase_state()
        start_iter = dev_state.get("iteration", 0)
        # `iteration` is saved when an iteration STARTS. A run cut off inside
        # it used to resume at the next one, and past the cap that meant
        # drafting at once: f028f3ba, 5c0e44f1 and b5fedacd wrote papers on
        # experiments that never finished. Re-enter the cut iteration; its
        # finished steps are skipped (_run_experiment_loop).
        if (dev_state.get("status") == "in_progress" and start_iter > 0
                and "evaluate" not in (dev_state.get("steps_done") or [])):
            start_iter -= 1
            self.log(f"Resuming dev iteration {start_iter + 1}: it was cut off "
                     f"before its completeness check", "WARN")
        elif dev_state.get("sufficient"):
            start_iter = max_dev_iters  # the last check already said enough

        self.log("", "RAW")
        self.log_section(f"Dev Phase  |  Building experiments & data  |  max {max_dev_iters} iterations")
        self._send_dev_phase_telegram("start", 0, max_dev_iters)

        return dev_state, start_iter, max_dev_iters

    def _start_initial_draft(self):
        self.log("", "RAW")
        self.log_section("✏️ Writing Initial Paper Draft")
        self._send_dev_phase_telegram("writing", 0, 0)

    def _abort_dev_on_terminal_error(self, during: str) -> bool:
        """Stop the dev phase when an agent hit a non-retryable error.

        The review loop has checked ``_terminal_error`` between steps for a
        while; the dev loop never did, and that asymmetry is expensive. Seen
        live: a misresolved API key made every agent die on a 401, each dead
        agent was still logged "✓ completed", and the loop walked through plan
        → run → analyze → evaluate producing nothing. Downstream then read the
        absent results as "no experiments are running" and went on to write a
        paper about findings that did not exist.

        Failing here is what makes the run's own report trustworthy: no
        experiments means no paper, not a paper with no experiments.
        """
        err = getattr(self, "_terminal_error", None)
        if not err:
            return False
        self.log(f"Dev phase aborted while {during} — {err}", "ERROR")
        self.log("Refusing to draft until this step completes. Continue uses the saved "
                 "experiment checkpoint.", "ERROR")
        return True

    def _run_experiment_loop(self, dev_state: dict, start_iter: int,
                             max_dev_iters: int, research_idea: str):
        """Steps 1-4: Iterative experiment planning, execution, analysis, and evaluation.

        Loops until experiments are sufficient or max iterations reached.

        Each step's agent is instructed to Read source files (deep_research.md,
        experiment_plan.yaml, results/, findings.yaml) directly — we do not
        pre-load or pass truncated content through the call chain.
        """
        findings_summary = self._load_findings_summary()

        for dev_iter in range(start_iter + 1, max_dev_iters + 1):
            experiment = ExperimentIteration(self, dev_state, dev_iter, max_dev_iters,
                                             dev_iter == start_iter + 1, research_idea, findings_summary)
            for name in experiment.remaining_steps():
                if not getattr(experiment, name)():
                    return
            if experiment.evaluate() is not False:
                break
            findings_summary = experiment.findings

    def _plan_experiments(self, dev_iter: int, max_dev_iters: int,
                          research_idea: str, findings_summary: str) -> str:
        """Step 1: Plan experiments using planner agent."""
        self.log_step_header(1, 4, "Plan Experiments")
        venue_pages = int(self.config.get("venue_pages", 9) or 9)
        # Page-aware experiment budget. A 1-page workshop poster doesn't need
        # 8 experiments; a full conference paper does. Cap accordingly so the
        # experimenter agent can finish within its timeout budget.
        if venue_pages <= 2:
            max_exps = 1
            scope_note = ("This is a very short paper ({}p). Any new measurement must fit "
                          "in one focused experiment under 5 minutes.").format(venue_pages)
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
        saved_results = [os.path.relpath(path, self.code_dir)
                         for path in self._experiment_evidence_files(limit=6)]
        prior_controls = self._load_dev_phase_state().get("comparison_controls", []) if dev_iter > 1 else []
        output = self.run_agent("planner", f"""
You are planning experiments for a research project. This is Dev Phase iteration {dev_iter}/{max_dev_iters}.

## Research Idea
{research_idea}

## Source Material (MANDATORY — Read before planning)
- `auto_research/state/deep_research.md` — full Gemini Deep Research report
- `auto_research/state/project_context.md` — verified external systems + install hints

Use Read to load both files in full. Ground your experiment plan — especially
the `required_systems` section — in those files. Do NOT guess from memory, and
do NOT re-derive systems already listed in project_context.md.

## Current Findings
{findings_summary if findings_summary else "No findings summary is available."}
Candidate result files on disk (up to six paths, contents and relevance unverified):
{json.dumps(saved_results, ensure_ascii=False)}
Active comparison controls from earlier iterations:
{json.dumps(prior_controls, ensure_ascii=False)}

## Scope & Budget
{scope_note}
You MUST plan no more than {max_exps} experiments total. Put the cheapest
comparison that could change the central conclusion first. Defer larger
runs until that comparison shows why they are needed.

## Task
Use the existing project environment to inspect files. Your only deliverable is
the plan. Do not create environments, install packages, train models, run experiments
or submit jobs during planning. The experimenter will handle these after approval.

Design a focused experiment plan ({max_exps} experiments max):
1. Identify the external systems, tools, libraries and datasets the experimenter
   will need, based on the research idea and project context. Reuse existing
   dependencies and record install hints only for missing ones.
2. Inspect relevant saved outputs under `results/` and `{self.config.get("scripts_dir", "scripts")}/results/`,
   even if the findings summary is empty. Cite the file and rows, keys or cases
   that already cover each comparison. Identify which observations are missing.
   If the saved observations suffice, specify the reanalysis using those files.
3. For an explanatory claim, name a credible rival, including a confound or a null
   effect when plausible. Select a control that gives the two explanations different
   observable predictions. Specify what changes and what stays matched (for example,
   the same data split, evaluator and training budget).
   When the central comparison depends on a matched attribute, include it under
   `comparison_controls`. Give distinct result pointers for the reference and
   treatment so the experimenter can record each arm's observed value from its
   loaded data or runtime configuration. Choose a field that identifies the
   attribute (for cohort membership, compute a digest of evaluated IDs for each arm).
   Omit this section when matching an attribute is irrelevant to the design.
   To correct an earlier comparison, keep its label and give the replacement fresh
   result paths. Add `replaces` to that new control, copying the exact reference
   and treatment pointers from the active pair above. Preserve the pilot results.
4. Give each experiment's script, parameters, observable and predicted outcomes in
   advance. For exploratory or qualitative work, use a contrasting case or a pattern
   that would change the interpretation. Omit inapplicable hypothesis fields for
   purely descriptive work.
5. State a feasible time, compute or sample limit and what result would stop further
   runs, justify a larger test, or leave the claim unresolved. If both explanations
   fit, specify the narrower claim. Include only this iteration's work in `experiments`.

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

# Optional example for a claim that depends on evaluating the same population:
comparison_controls:
  - label: "same evaluated population"
    reference_source: "results/metrics.json#/reference/cohort"
    treatment_source: "results/metrics.json#/variant/cohort"
    # Only for a corrected comparison with the same label:
    # replaces:
    #   reference_source: "results/pilot.json#/reference/cohort"
    #   treatment_source: "results/pilot.json#/variant/cohort"

experiments:
  - id: "exp1"
    title: "Experiment title"
    description: "What to test"
    hypothesis: "Proposed explanation and its observable prediction under the control"
    alternative: "Most credible rival explanation or interpretation"
    falsifier: "Contrasting outcome expected under the rival that would weaken the proposed explanation"
    existing_evidence: "Source and coverage of saved observations, or what you checked and found missing"
    script: "path/to/script.py"
    parameters: "key params"
    metrics: ["metric1", "metric2"]
    baseline: "Control or contrasting case, including what is held constant"
    stopping_rule: "Budget for the comparison and how its outcome changes the next decision or claim"
```

IMPORTANT: If the research idea describes a specific platform, framework, or system (e.g., "evaluate on OpenClaw", "benchmark on MLPerf"), you MUST list it under required_systems. The experimenter is NOT allowed to re-implement these from scratch — they must install and use the real thing. If you are unsure how to install something, write your best guess for install_hint and the experimenter will search online for the correct method.
Once you have saved experiment_plan.yaml, return immediately for approval.
""", timeout=defaults.TIMEOUT_LIT_REVIEW, pre_draft=True)
        self.log_step_header(1, 4, "Plan Experiments", "end")
        return output

    def _run_experiments(self, dev_iter: int, max_dev_iters: int,
                         resumed: bool = False) -> str:
        """Step 2: Run experiments using experimenter agent + compute backend."""
        self.log_step_header(2, 4, "Run Experiments")
        self._send_dev_phase_telegram("experiments", dev_iter, max_dev_iters)

        resume_note = ""
        session_args = {}
        if selected_agent_runtime() == "dsh":
            session_args["session_checkpoint"] = self.state_dir / f"dev-experiment-{dev_iter}.dsh-session"
        if resumed:
            # The cut attempt may have left jobs queued or running. Let them
            # finish first, so the new session finds their output instead of
            # submitting the same experiments a second time.
            if not settle_interrupted_experiments(self):
                return ""
            resume_note = EXPERIMENT_RESUME_NOTE
            checkpoint = session_args.get("session_checkpoint")
            if checkpoint is not None and checkpoint.is_file():
                session_args["interrupted_session"] = checkpoint.read_text().strip()
                session_args["resume_instructions"] = resume_note.strip()

        compute_ctx = self._compute_backend.setup()

        # The task text tells the agent to put scripts in scripts/ and results
        # in results/, but nothing ever created those directories. A capable
        # model runs mkdir first; a weaker one writes straight to the promised
        # path, takes ENOENT from the file tool, and gives up (seen live on
        # ac5318e8 — 161s of real work ended by exactly this). If we name the
        # directories, we provide them.
        (self.code_dir / "results").mkdir(exist_ok=True)
        (self.code_dir / self.config.get("scripts_dir", "scripts")).mkdir(exist_ok=True)

        remote_work_dir = compute_ctx.get("work_dir", str(self.code_dir))
        local_results = str(self.code_dir / "results")
        remote_results = f"{remote_work_dir}/results"

        sessions = []
        try:
            if resumed and not self._compute_backend.sync_from_backend(remote_results, local_results):
                self._terminal_error = "Could not retrieve prior experiment results. Check the backend before resuming."
                return ""
            self._compute_backend.sync_to_backend(str(self.code_dir), remote_work_dir)
            compute_instructions = self._compute_backend.get_agent_instructions()
            exp_output = self.run_agent("experimenter", f"""
Carry out the approved comparisons for this iteration. Reuse saved observations
that cover an arm, run missing measurements and honor the plan's stopping rule.
{resume_note}
## Experiment Plan (MANDATORY — Read before running)
Use Read to load `auto_research/state/experiment_plan.yaml` in full. It
contains the required_systems and every experiment definition. Do NOT
proceed without having consulted it.
{self._experiment_plan_digest()}
For each comparison_controls entry, record each arm's value at its specified
JSON Pointer from the data or configuration actually used by that arm. Preserve
any mismatch. If the value is unavailable, leave the field absent and explain the gap.
For a control with `replaces`, keep the old raw files and write the corrected
comparison to its new result paths. Distinguish the two runs in the results.

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
- Submit independent new measurements together. For a comparison covered by
  existing observations, compute it directly from those files.
- Each script should save results to results/ directory
- Use clear naming: results/exp1_results.json, results/exp2_results.json, etc.
- Handle errors gracefully (log failures, continue with remaining experiments)
- Keep experiments small enough to finish within the agent budget
""", timeout=defaults.TIMEOUT_EXPERIMENTER, session_out=sessions, **session_args)

            self.log_step("Waiting for all experiments to complete...", "progress")
            waited = time.time()
            if not self._compute_backend.wait_for_completion(max_wait_hours=4):
                if not shutting_down.is_set() and not getattr(self, "_terminal_error", None):
                    self._terminal_error = "Experiment jobs did not settle. Check the scheduler before resuming."
                return ""
            if not self._compute_backend.sync_from_backend(remote_results, local_results):
                if not getattr(self, "_terminal_error", None):
                    self._terminal_error = "Could not retrieve experiment results. Check the backend before resuming."
                return ""
            collected = collect_after_jobs(self, sessions, time.time() - waited)
            if collected:
                exp_output = collected
        finally:
            self._compute_backend.teardown()

        # Check if experimenter requested human intervention
        self._check_human_intervention(stage="Run Experiments")

        # One evidence-driven second chance before giving up. The failure this
        # covers is an agent that stopped one turn short: on a7235ecf the
        # experimenter installed deps, wrote the script, RAN it, watched it
        # crash on a missing import, said "let's fix that and rerun" — and
        # ended its turn there, because a message without a tool call is how an
        # agent declares itself finished. Everything it needed was in place;
        # what it lacked was being told to continue. So tell it, once, with the
        # evidence attached. If the retry also produces nothing, the normal
        # abort stands. Not after a terminal error, though: a bad key or a spent
        # balance kills the retry the same way, and the retry's "wrote nothing"
        # would then bury the real cause.
        if (not self._experiment_evidence_files()
                and not getattr(self, "_terminal_error", None)):
            self.log("Experiments left no usable results — giving the "
                     "experimenter one corrective pass", "WARN")
            results_dir = self.code_dir / "results"
            empties = ([str(f.relative_to(self.code_dir))
                        for f in results_dir.rglob("*")
                        if f.is_file() and f.stat().st_size == 0]
                       if results_dir.exists() else [])
            hint = (f"Empty files from the crashed attempt: {', '.join(empties)}. "
                    if empties else "")
            self._experimenter_corrective_pass(hint)

        self._require_experiment_evidence()

        self.log_step_header(2, 4, "Run Experiments", "end")
        return exp_output

    def _experimenter_corrective_pass(self, hint: str) -> None:
        """Re-run the experimenter once, on a backend set up for this pass.

        The first pass's backend is already torn down here (on SkyPilot the
        cluster is down), so this pass brings its own: set up, push the code,
        run, wait, pull results/ back, tear down. Without the pull, a retry
        that succeeded on a remote backend would still look empty locally.
        """
        compute_ctx = self._compute_backend.setup()
        remote_work_dir = compute_ctx.get("work_dir", str(self.code_dir))
        self._compute_backend.sync_to_backend(str(self.code_dir), remote_work_dir)
        try:
            self.run_agent("experimenter", f"""
The previous attempt left no usable experiment artifact in `results/`.
{hint}Read the existing scheduler stdout, stderr and saved outputs first.
If the measurements already exist, extract them to a structured result file.
Do not resubmit a completed job to recover numbers it already produced.

If measurements are missing, diagnose the failure from its error output and
fix the failed experiment. Save its measured results. Before you finish, print the
result files (`cat results/*.json`) to confirm they are non-empty. Do not
report success without that confirmation.

{self._compute_backend.get_agent_instructions()}
""", timeout=defaults.TIMEOUT_EXPERIMENTER)
            self._compute_backend.wait_for_completion(max_wait_hours=1)
            self._compute_backend.sync_from_backend(
                f"{remote_work_dir}/results", str(self.code_dir / "results"))
        finally:
            self._compute_backend.teardown()

    def _experiment_plan_digest(self, limit: int = 12) -> str:
        """A few lines naming what has to run, inlined into the task.

        The pipeline's convention is that agents Read their source files rather
        than being handed pre-loaded (and possibly truncated) content. That
        holds up with a model that reliably follows a "go read this first"
        instruction; a weaker one answers from the prompt alone and finishes
        its turn having done nothing. Measured on a local 32B across three
        runs: 13k tokens in, ~600 out, zero tool calls, an empty results/ — the
        same model completed the identical work in ten tool calls when the task
        itself said what to run.

        So: seed the ids and one-line descriptions, and keep the instruction to
        read the file for everything else. Costs a strong model a handful of
        tokens it already had; gives a weak one somewhere to start.
        """
        plan = self.state_dir / "experiment_plan.yaml"
        try:
            data = yaml.safe_load(plan.read_text()) or {}
            entries = data.get("experiments") or []
        except Exception:
            return ""
        if not isinstance(entries, list) or not entries:
            return ""
        lines = []
        controls = data.get("comparison_controls")
        if isinstance(controls, list):
            for control in controls[:limit]:
                if not isinstance(control, dict):
                    continue
                fields = [control.get(key) for key in ("label", "reference_source", "treatment_source")]
                if all(isinstance(field, str) and field.strip() for field in fields):
                    label, reference, treatment = fields
                    lines.append(f"- Match to check: {label}\n  Reference: {reference}\n  Treatment: {treatment}")
                    previous = control.get("replaces")
                    if isinstance(previous, dict):
                        old_reference = previous.get("reference_source")
                        old_treatment = previous.get("treatment_source")
                        if isinstance(old_reference, str) and isinstance(old_treatment, str):
                            lines.append(f"  Replaces prior reference: {old_reference}\n"
                                         f"  Replaces prior treatment: {old_treatment}")
        for e in entries[:limit]:
            if not isinstance(e, dict):
                continue
            eid = str(e.get("id") or "?")
            # The planner is free-form about this key; observed "title" and
            # "description" from the same model on consecutive runs.
            what = str(e.get("description") or e.get("title") or e.get("name")
                       or e.get("goal") or "").strip().replace("\n", " ")
            lines.append(f"- {eid}: {what[:160]}" if what else f"- {eid}")
            for key, label in (("hypothesis", "Hypothesis"), ("alternative", "Rival explanation"),
                               ("baseline", "Control or baseline"), ("falsifier", "Would count against it"),
                               ("existing_evidence", "Evidence already checked"), ("metrics", "Metrics"),
                               ("stopping_rule", "Budget and stopping rule")):
                value = e.get(key)
                if isinstance(value, list):
                    value = ", ".join(str(item) for item in value if isinstance(item, (str, int, float)))
                if isinstance(value, str) and value.strip():
                    lines.append(f"  {label}: {' '.join(value.split())[:180]}")
        if not lines:
            return ""
        more = ("\n- … plus more; the file is authoritative"
                if len(entries) > limit else "")
        return ("\nFor orientation, the plan currently defines:\n"
                + "\n".join(lines) + more + "\n")

    def _experiment_evidence_files(self, limit: int | None = None) -> list:
        """Result files with actual content in them.

        Zero-byte files are excluded deliberately: they are the fingerprint of
        a crash, not of a result. Observed on a7235ecf — the script opened
        results/exp1_results.json for writing and died on the very next line
        (a missing `import json`), leaving an empty file that an existence
        check happily accepted as evidence.
        """
        scripts_dir = self.config.get("scripts_dir", "scripts")
        candidates = [self.code_dir / "results",
                      self.code_dir / scripts_dir / "results"]
        files = (f for d in candidates if d.exists()
                for f in d.rglob("*")
                if f.is_file() and f.stat().st_size > 0
                # An escalation report is a request for help, not a result;
                # counting it as evidence sent a run into analysis with nothing
                # to analyse but its own cry for help (9df9e778).
                and not f.name.startswith("needs_human")
                # Likewise the setup reports the task asks for BEFORE any
                # experiment runs: an agent that installed deps and stopped
                # has a non-empty results/ and no results.
                and f.name not in _SETUP_REPORTS
                # Slurm opens these even for OOM and failed setup. Job logs
                # can diagnose failures; only experiment outputs unlock analysis.
                and not re.fullmatch(r"slurm-\d+(?:_\d+)?\.(?:err|out)", f.name))
        return list(islice(files, limit))

    def _require_experiment_evidence(self) -> None:
        """An experiment step that produced nothing did not succeed.

        The agent's own "done" is a claim; a file on disk is evidence. Seen live
        on smoke run 8ad45646: a local model's experimenter had three tool calls
        refused by the environment, wrote nothing, and still reported success —
        after which the compute backend read the missing results directory as
        "nothing is running" and returned done, and the pipeline proceeded to
        analyse and write up results that did not exist.

        Checked only after ``wait_for_completion``, so an asynchronous backend
        has already had its chance to finish and sync. Sets the terminal flag
        rather than raising: the dev loop's own checkpoint then ends the run the
        same way any other non-retryable failure does.
        """
        if self._experiment_evidence_files():
            return
        if getattr(self, "_terminal_error", None):
            return          # already failing, and that error is the real cause
        if os.environ.get("ARK_ALLOW_EMPTY_EXPERIMENTS", "").strip() == "1":
            self.log("Experiments produced no result files — continuing anyway "
                     "(ARK_ALLOW_EMPTY_EXPERIMENTS=1)", "WARN")
            return
        failure = self.code_dir / "results" / "setup_failure.json"
        if failure.is_file() and failure.stat().st_size > 0:
            self._terminal_error = (
                "Experiments never ran: environment setup failed (see "
                "results/setup_failure.json): "
                + failure.read_text(errors="replace")[:240].replace("\n", " "))
        else:
            self._terminal_error = (
                "Experiments produced no usable result files. Check the job logs and "
                "save the measured output under results/ before drafting.")
        self.log(self._terminal_error, "ERROR")

    def _analyze_results(self, open_questions: list[str] | None = None) -> str:
        """Check the analysis against the saved results before spending another iteration."""
        from ark.findings_schema import analysis_evidence_issues, sync_findings_from_json

        self.log_step_header(3, 4, "Analyze Results")
        task = f"""
Analyze ALL experiment results from this dev iteration.

## Source Material (MANDATORY — Read before analyzing)
- `auto_research/state/experiment_plan.yaml` — what the plan claimed would run
- every file under `results/` — actual experiment outputs (use Glob + Read)
- If `auto_research/state/findings.json` exists, read it before updating it. For an older project with only `findings.yaml`, read that instead.

Use Read/Glob to inspect the result files directly. Do NOT rely on a
summary — verify each experiment's outputs against the plan.

## Task
1. Check all result files in results/ directory
2. Verify experiments completed successfully (no errors, valid outputs)
3. Summarize key findings
4. Compare against the measured baselines, identify missing controls and sample sizes.
5. Report negative results and alternative explanations. Distinguish deterministic
   measurements from noisy ones. For a timing measured once, describe the observed
   time and explicitly leave variability and a general speed advantage unresolved.
6. Write auto_research/state/findings.json with ALL findings. For directly copied
   JSON measurements include metric_sources with the original unrounded value
   and its JSON Pointer. For a derived comparison identify the reference and
   treatment, their raw locations (JSON Pointers when available), metric, units,
   and signed formula in construct.
   Check which trials, inputs or splits each arm covers and how they were aggregated.
   If the measurements do not cover the claimed comparison, report the observations
   separately and state what is missing. Citations to text logs, CSV rows, and
   predicted or hypothetical values belong in source/construct, outside the
   per-metric JSON Pointer map.
   Preserve prior coverage, notes and qualitative findings.

Write **auto_research/state/findings.json** (JSON, not YAML). ARK converts
it to findings.yaml for you — do NOT hand-write YAML. Include every finding
(carry prior ones forward). Shape:
```json
{{
  "findings": [
    {{
      "id": "finding1",
      "experiment": "exp1",
      "result": "Key result description",
      "metrics": {{"accuracy": 0.81}},
      "source": "results/eval.json#test",
      "metric_sources": {{"accuracy": "results/eval.json#/test/accuracy"}},
      "construct": "Test-set accuracy over the stated population and split",
      "significance": "Why this matters",
      "supports_claim": "Which paper claim this supports"
    }}
  ]
}}
```
"""
        if open_questions:
            task += ("\nAnswer these questions by reanalyzing the saved measurements. "
                     "Do not submit new experiments. Record any comparison those data cannot support.\n"
                     + "\n".join(f"- {gap}" for gap in open_questions))
        json_path = self.state_dir / "findings.json"
        yaml_path = self.state_dir / "findings.yaml"
        for attempt in range(2):
            source = json_path if json_path.exists() else yaml_path
            before = self._file_stamp(source)
            output = self.run_agent("planner", task, timeout=defaults.TIMEOUT_LIT_REVIEW, pre_draft=True)
            if getattr(self, "_terminal_error", None):
                break
            current = json_path if json_path.exists() else yaml_path
            unchanged = current == source and self._file_stamp(current) == before
            converted, messages = sync_findings_from_json(self.state_dir)
            for message in messages:
                self.log_step(message, "info" if converted else "warning")
            # An invalid JSON submission must not pass against a stale YAML
            # from a previous iteration. YAML-only projects still resume.
            if not converted and json_path.exists():
                issues = ["Write valid findings.json containing the current analysis."]
            else:
                issues = analysis_evidence_issues(yaml_path, self.code_dir, warn=lambda msg: self.log(msg, "WARN"))
            if open_questions and unchanged and not issues:
                issues = ["Record the analysis of the saved results in findings.json: " + "; ".join(open_questions)]
            if not issues:
                break
            detail = "\n".join(issues)
            if attempt:
                self._terminal_error = f"Analysis needs corrected findings before drafting. {detail}"
                break
            self.log_step(f"Checking findings against saved results: {detail}", "warning")
            task = ("Correct auto_research/state/findings.json using the existing raw results. "
                    "Keep valid observations. Correct each cited value or withdraw the claim "
                    "and explain the limitation. metric_sources holds exact observed JSON scalars only. "
                    "For a prediction or a value from a log or CSV, describe its source and calculation "
                    "in construct and omit that entry from metric_sources. Preserve pointers for all "
                    "direct JSON measurements. For a disputed comparison, verify the two raw inputs, "
                    "their population, units, direction and denominator, or narrow the claim. "
                    "Do not submit or rerun experiments.\n" + detail)
        self.log_step_header(3, 4, "Analyze Results", "end")
        return output

    def _evaluate_completeness(self, research_idea: str,
                                findings_summary: str) -> ResearchDecision | None:
        """Decide whether another experiment would change a material paper claim."""
        self.log_step_header(4, 4, "Evaluate Completeness")

        decision_format = """Return one JSON object. Always include sufficient (boolean), recommendation
(proceed_to_writing, reanalyze_existing or need_more_experiments), contradictions,
claim_checks and gaps (arrays). sufficient is true only for proceed_to_writing.
For contradictions give the finding ID and the counterexample or arithmetic in one sentence.
A claim_check takes claim, predicted (the number asserted), and observed_source for a
single raw scalar. For directly comparable raw scalars, use reference_source,
treatment_source and operation: difference (treatment minus reference), ratio
(treatment / reference), or relative_change_pct (100 * difference / reference).
Sources are results/file.json#/field JSON Pointers. A reported reduction has negative
relative_change_pct. For floating-point means, subtracting two stored arm means can
differ in the last digits from the mean of paired differences. Write the derived
claim and predicted value at the precision meaningful for the finding (e.g. a
difference of 0.07544 with decimal_places=5). If the finding instead reports
the full scalar saved for the paired statistic, check it with observed_source.
Keep the original units. decimal_places applies only to an explicitly rounded claim.
If no check fits, use []; give the calculation or missing observation in words.
A clear contradiction is sufficient to return a decision immediately."""
        task = f"""
Evaluate whether we have sufficient experimental data for the paper.
Question: {research_idea}

Findings to check:
{findings_summary}

Read auto_research/state/findings.yaml, the experiment_plan.yaml in the same
directory, and the relevant raw files under results/. Use project_context.md
and setup or credential records to check systems the comparison actually requires.
Inspect the strongest claim first. Match metric, units, workload and denominator.
Try a counterexample to an exact or universal claim. Distinguish a failure of the
hypothesis already reported in the findings from an error still present in the findings.

Choose proceed_to_writing for a supported, scoped result, including a null result.
Choose reanalyze_existing when saved observations can settle the question. Explain
what is wrong and the correction, even if the derivation takes multiple steps and
has no single raw JSON pointer. Choose need_more_experiments when the scoped question
needs a new measurement, naming the smallest missing comparison in gaps. A short
descriptive study need not establish a population-wide effect. Return the verdict
now. Do not modify files, submit jobs or design additional studies.

{decision_format}
"""
        session = []
        decision = None
        for turn in range(2):
            outcome = {}
            reply = self.run_agent(
                "planner", task, pre_draft=True, session_out=session, outcome_out=outcome,
                continue_session=session[-1] if turn else None,
                timeout=defaults.TIMEOUT_SCORE_RETRY if turn else defaults.TIMEOUT_INITIALIZER,
            )
            if getattr(self, "_terminal_error", None):
                break
            if outcome.get("completed") is False:
                self._terminal_error = "The evidence review stopped before completion. Continue to retry it with the saved results."
                break
            try:
                decision = ResearchDecision.from_review(reply, self.code_dir, self.log)
                break
            except ValueError as error:
                if turn or not session or not outcome.get("completed"):
                    self._terminal_error = f"{error} Continue to retry the review with the saved results."
                    break
                self.log_step("The reviewer finished reading the evidence. Asking it to complete its decision.", "info")
                task = (f"Finish the evidence review from this conversation. {error} "
                        "Use the observations already inspected. Retain counterexamples, raw source "
                        "references and limitations. Distinguish a disproved hypothesis already corrected "
                        "in the findings from a contradiction still in the findings. If the evidence is "
                        "insufficient, name the missing comparison. Do not call tools, edit files or "
                        "submit a job.\n" + decision_format)
        self.log_step_header(4, 4, "Evaluate Completeness", "end" if decision else "incomplete")
        return decision

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
For every caption, compare the printed axis/legend labels and the plotting script with the
quantity and units you describe. If the required display is missing, identify which plot needs repair.
{figure_list}

**CRITICAL**: The figures above are already generated. Use \\includegraphics to include them.
- AI concept figures (marked as "AI concept") must NOT be recreated as TikZ or matplotlib.
- Statistical plots (marked as "matplotlib") are already generated from experiment data.
- Use the EXACT filenames listed above in your \\includegraphics commands.
- For multi-column templates, use \\begin{{figure*}} for wide concept figures, \\begin{{figure}} for single plots.

## MANDATORY — every item below is required, NO exceptions:

### 1. All sections must be fully written (zero placeholders)
- Abstract: follow the venue limit. For a short paper, use one compact paragraph with the question, the most informative measured comparison, and its scope
- Introduction: motivation, question, and contributions supported by this study
- Related Work: cite verified sources relevant to the closest competing explanations
- Method: full technical description, equations where appropriate
- Experiments: setup, measured baselines, main results and any ablations actually run
- Analysis/Discussion: explain WHY results are good/bad, failure cases
- Conclusion: answer the question in the space the result warrants. Distinguish what the design identifies from explanations it leaves open
- Acknowledgments: keep the pre-inserted `\\section*{{Acknowledgments}}` block VERBATIM. It is *endmatter* — ARK's post-processing will automatically place it on the references page (after the body `\\clearpage`, before `\\bibliography`), and it does NOT count toward the body page limit. Do NOT rewrite, edit, translate, remove, or wrap it in `\\clearpage` yourself.

### 2. Appendix policy (use `\\appendix` only when content genuinely belongs there)
- Belongs in appendix: full proofs/derivations, extended ablation tables, hyperparameter sweeps, prompt templates, implementation/config details, additional qualitative examples, dataset statistics beyond a summary
- Belongs in body: problem, core method, headline results, primary ablation, key analysis, **and FIGURES** (method/result/concept figures)
- **Figures stay in the body.** Never move a figure to the appendix just to meet the page limit — condense prose or move detailed text/tables there instead. A figure relegated to the appendix to save space usually should not have been generated. Only genuinely supplementary figures (extra examples beyond the main results) may go to the appendix.
- The body-page limit excludes `\\appendix` — prefer appendix over cutting body when supplementary material is worth keeping
- Keep appendix tables near the protocol they support. If wide floats leave a nearly empty
  appendix page, use the venue's supported appendix layout and compile again.
- Do NOT create an empty or single-paragraph appendix just to have one

### 3. Data integrity
- Every performance claim must use actual numbers from findings
- Tabulate measured baseline comparisons where available. Identify any missing control explicitly
- Include denominators or units. State the limits of each comparison

### 4. Page target: {venue_pages} pages of body text
- Fit the evidence to the venue. Leave fine page fitting to the configured layout pass
- Ensure `\\clearpage` before `\\bibliography{{...}}`

### 5. LaTeX mechanics
- Edit {latex_dir}/main.tex directly
- Verify compilation: cd {latex_dir} && pdflatex -interaction=nonstopmode main.tex
- All \\ref and \\cite must resolve

Produce the complete paper. Do not stop until all sections are written and it compiles.
"""

        if (self.code_dir / "auto_research/state/deep_research.md").is_file():
            prompt += ("\nRead auto_research/state/deep_research.md and literature.yaml in that directory, "
                       "plus the source-specific findings in project_context.md and the managed bibliography. For each outside claim that matters to the argument, "
                       "check the original paper and the conditions of its result. Cite the supported claim at "
                       "its point of use. Cover relevant competing approaches and limitations; omit unsupported "
                       "claims even when the survey recommends the citation. Leave uncited records marked "
                       "[NEEDS-CHECK]. In a reproducibility appendix, specify the actual worker entrypoint, "
                       "inputs, outputs, software and how to regenerate the key result. Link it where the "
                       "body first relies on these details.")
        state = self._load_dev_phase_state()
        gaps = state.get("evidence_gaps") or []
        if state.get("comparison_controls"):
            from ark.findings_schema import comparison_control_gaps
            # Continue may resume directly at drafting with a saved positive
            # verdict. Check the cited metadata as it exists at handoff.
            current_gaps = comparison_control_gaps(state["comparison_controls"], self.code_dir, include_replaced=True)
            gaps = list(dict.fromkeys([*current_gaps, *gaps]))
            prompt += ("\nA match in recorded metadata establishes agreement on that field only. "
                       "Support claims about experiment execution and attribution with separate evidence.")
            for control in state["comparison_controls"]:
                if isinstance(control, dict) and isinstance(control.get("replaces"), dict):
                    old = control["replaces"]
                    prompt += (f"\nThe follow-up for '{control['label']}' uses {control['reference_source']} "
                               f"and {control['treatment_source']}. Any conclusions from that pair apply only "
                               f"to its observations. Describe the earlier pair at {old['reference_source']} "
                               f"and {old['treatment_source']} separately.")
        if gaps or state.get("sufficient") is False:
            limitations = "\n".join(f"- {gap}" for gap in gaps)
            prompt += ("\n\n## Scope of the available evidence\n"
                       "The experimental review left the following questions open. Narrow the abstract, "
                       "results and conclusion accordingly. Describe untested comparisons as limitations.\n"
                       + limitations)
        prompt += ("\nRead the full findings.yaml and the cited raw outputs before writing. "
                   "Only report measured comparisons. Explain what a negative result still establishes. "
                   "Keep metric names, units, denominators and aggregation as measured. For each derived "
                   "statistic test whether two datasets with the same recorded observations could have "
                   "different values of that statistic. If so, the observations do not identify it. "
                   "Omit the estimate or state precisely which inputs are missing. "
                   "Identify what each observation represents. Distinct inputs or conditions establish "
                   "coverage. To report repeatability, trace runs within the same condition or use a "
                   "per-condition summary that records the statistic. Give the recorded count, spread "
                   "and unit where relevant. An exact count or agreement between methods does not "
                   "establish zero run-to-run variance. When replication is undocumented, describe the "
                   "observed values and leave repeatability open. Scope timing to the trials recorded. "
                   "Claims about untested sizes or asymptotic behavior require a derivation.")
        self.run_agent("writer", prompt, timeout=defaults.TIMEOUT_WRITER)

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
            question, options, timeout=defaults.TIMEOUT_HITL_DECISION,
            what_happened=trigger,
            background=background,
            option_details=self._build_option_details(options, review_output),
            phase="smart_intervention",
        )
        self._asked_this_iteration = True
        self._record_intervention_choice(options, idx, reply, score)
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
        result_files = sorted(
            f.name for f in results_dir.iterdir()
            if f.suffix in (".json", ".csv", ".txt") and f.stat().st_size > 0
        )
        if not result_files:
            self.log("No data files in results/, skipping", "INFO")
            return

        # Ensure script directory exists
        script_path.parent.mkdir(parents=True, exist_ok=True)

        results_rel = results_dir.relative_to(self.code_dir) if results_dir.is_relative_to(self.code_dir) else results_dir

        self.run_agent("coder", f"""Create a Python plotting script that generates publication-quality
statistical figures from the experiment results in this project.

## Output
Save the script to: {script_rel}
The script must be self-contained and runnable with: python {script_rel}

## Source Material (MANDATORY — Read before writing)
- `{results_rel}/` — raw experiment result files (use Glob + Read on each one)
- `auto_research/state/findings.yaml` — accumulated findings summary
- `auto_research/state/experiment_plan.yaml` — planned comparisons, pairing and stopping rules
- `auto_research/state/paper_requirements.yaml`, when present — requested quantities and displays
- Existing plots in results/ and their generating scripts — reuse the measured series and their
  definitions when preparing publication-sized versions.

Files currently present in `{results_rel}/`:
{chr(10).join(f'- {f}' for f in result_files)}

Read each one to understand its schema before designing plots. Do NOT guess
the data shape — a plot that assumes the wrong columns produces a blank or
mislabeled figure.

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
2. Plot the quantities specified in the paper requirements and experiment plan. Choose two
   complementary views when the evidence supports them. Preserve paired comparisons as paired
   observations. Show censoring and distinguish it from observed endpoints. Use an ablation
   label only for an experiment that actually removes or varies a component.
3. Save each figure as BOTH PDF and PNG to {figures_dir}/
4. Give each file a descriptive name that identifies the measured quantity.

## ❌ HARD RULE: NO FIGURE TITLES INSIDE THE PLOT
LaTeX `\\caption{{}}` is the **single source of truth** for the figure's
title and number. The figure environment auto-numbers as Figure 1, 2, 3, …
based on order of appearance in the .tex file. Any title baked into the
matplotlib output ("Figure 1: My Result", "Phase Diagram", etc.) ends up
*next to* a different LaTeX-numbered caption in the rendered PDF — two
clashing titles per figure, with mis-matched numbers. It looks broken.

Concretely:
- ❌ Do NOT call `plt.title(...)`.
- ❌ Do NOT call `fig.suptitle(...)`.
- ❌ Do NOT call `ax.set_title("Figure N: ...")` or any other prose title
     for the figure as a whole.
- ✅ The ONLY allowed `set_title` use is per-panel sub-labels in
     multi-panel figures, and even then the title must be just the panel
     letter, e.g. `ax1.set_title('(a)', fontweight='bold', loc='left')`.
     No descriptive text — that goes in the LaTeX caption.

When in doubt: omit the title call entirely. The LaTeX `\\caption{{}}` will
provide the title.

## Style Guide (MUST follow):
- Apply ALL rcParams from figure_config.json (font sizes match LaTeX template)
- Wong colorblind-safe palette: ['#0072B2', '#D55E00', '#009E73', '#CC79A7', '#E69F00', '#56B4E9']
- Add hatching patterns for bar charts (colorblind accessibility)
- Use horizontal bars when there are 5+ categories (avoids x-label overlap)
- Abbreviate axis titles and tick labels like a human: short noun phrase or
  standard symbol with units in parens, BOTH axes (e.g. "Acc. (%)" not
  "Accuracy Percentage", "Std. features" not "Standardized Features",
  "Iter." not "Number of Iterations"). Never cram a value into a tick label
  (no "model-a (acc. 0.947)") — annotate the bar or spell it in the caption.
- constrained_layout=True on all figures
- DPI 300, sans-serif fonts exclusively
- Light dashed grid lines behind data
- Error bars with caps where applicable. Keep legends clear of all data, including capped
  points and error bars. Place a legend outside the axes when a panel lacks empty space.
- Use the experimental arm names. Attribute established methods by name.
""", timeout=defaults.TIMEOUT_INITIALIZER)

        if script_path.exists():
            self.log_step(f"Plotting script created: {script_rel}", "success")
        else:
            self.log(f"Coder agent did not create {script_rel}", "WARN")

    def _list_available_figures(self) -> str:
        """List all figures in paper/figures/ with placement, scalability,
        and inclusion status — surfaces `figure_manifest.json` to the writer.

        Four pieces of information per figure:
          - source (matplotlib / paperbanana / nano_banana / manual)
          - placement (single_column vs full_width → figure vs figure*)
          - scalable (whether \\includegraphics resize is safe — no for
            matplotlib vector-with-text, yes for AI bitmaps)
          - referenced-in-main.tex (surface AI figures the writer has
            silently dropped; this is the exact regression that produced
            the 275KB concept-figure-less PDFs).
        """
        if not self.figures_dir.exists():
            return "No figures generated yet."

        from ark.figure_manifest import load_manifest, AI_SOURCES
        manifest = load_manifest(self.figures_dir)
        manifest_figs = manifest.get("figures", {})

        # Read main.tex once so we can cheaply check "is this figure referenced?"
        main_tex_path = self.latex_dir / "main.tex"
        tex_content = ""
        if main_tex_path.exists():
            try:
                tex_content = main_tex_path.read_text()
            except OSError:
                tex_content = ""

        lines = []
        missing_ai_figs = []
        for f in sorted(self.figures_dir.iterdir()):
            if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".pdf"):
                continue
            size_kb = f.stat().st_size // 1024

            info = manifest_figs.get(f.name, {})
            source = info.get("source")
            # Source → is_ai (authoritative from manifest; legacy heuristic
            # if file isn't registered yet)
            if source in AI_SOURCES:
                is_ai = True
            elif source:
                is_ai = False
            else:
                is_ai = size_kb > 150  # legacy heuristic

            # Placement (figure vs figure*)
            placement = info.get("placement")
            if placement is None:
                # Legacy entry or missing — infer from size: AI figures
                # and large matplotlib plots default to full_width.
                placement = "full_width" if size_kb > 150 else "single_column"
            latex_env = r"\begin{figure*}[tb] / \textwidth" if placement == "full_width" \
                        else r"\begin{figure}[!htbp] / \columnwidth"

            # Scalable (\\includegraphics resize safe?)
            scalable = info.get("scalable")
            if scalable is None:
                scalable = is_ai  # matplotlib default false, AI default true
            scale_note = "safe to resize" if scalable \
                         else "DO NOT resize via \\includegraphics (regenerate with smaller figsize instead)"

            # Reference check: does main.tex \\includegraphics this stem?
            stem = f.stem
            is_referenced = bool(tex_content) and (stem in tex_content)

            if is_ai:
                if is_referenced:
                    tag_prefix = "AI concept diagram (already referenced)"
                else:
                    tag_prefix = "AI concept diagram — ⚠ MISSING FROM main.tex — MUST add \\includegraphics"
                    missing_ai_figs.append(f.name)
            else:
                tag_prefix = "matplotlib statistical plot"

            # Legibility: a vector figure whose own labels overprint is
            # unreadable on the page, and no structural check sees it. Say so
            # here, where the agent that can regenerate the plot is looking.
            overprint = ""
            if f.suffix == ".pdf":
                from ark.figure_legibility import describe
                try:
                    what = describe(f)
                except Exception as e:
                    # Not "clean": say the check could not run, so the agent
                    # does not read silence as a pass.
                    what = f"could not be read ({e}), so its labels are UNCHECKED"
                if what:
                    overprint = f"; ⚠ {what} — REGENERATE with room for them"

            labels = ""
            if f.suffix == ".pdf" and not is_ai:
                from ark.figure_legibility import printed_labels
                labels = printed_labels(f)
                if labels:
                    labels = f"; printed labels: {labels}"
            lines.append(
                f"- {f.name} ({size_kb}KB, {tag_prefix}; "
                f"placement={placement} → use {latex_env}; {scale_note}{labels}{overprint})"
            )

            if info.get("representation") == "subject_with_comparison_panel":
                lines.append("  This figure illustrates the study subject. The adjoining labels compare the "
                             "conditions. Explain the full design in its caption and methods prose; "
                             "avoid describing a flowchart. Reviewed caption guidance: "
                             + str(info.get("panel_plan", {}).get("caption", "")))

        if missing_ai_figs:
            lines.append("")
            lines.append(
                "**CRITICAL**: The AI concept figures marked MISSING above exist on "
                "disk but are NOT currently referenced in main.tex. Generating them "
                "cost real compute — DO NOT leave them unused. Add "
                "\\includegraphics for each one using the placement shown."
            )

        return "\n".join(lines) if lines else "No figures generated yet."

    def _send_dev_phase_telegram(self, event: str, current: int, total: int):
        """Send dev phase notifications to Telegram.

        Every message carries the unified ``🚤 ARK Project-<id5> | <title>``
        header so the user can tell which project pinged them even when
        multiple projects share the bot.
        """
        if not self.telegram.is_configured:
            return
        try:
            header = self.tg_header("🚤")
            if event == "start":
                body = f"⚙️ <b>Dev Phase started</b> — up to {total} iterations"
            elif event == "iteration":
                body = f"🔬 <b>Dev {current}/{total}</b> — planning experiments..."
            elif event == "experiments":
                body = f"🧪 <b>Dev {current}/{total}</b> — running experiments..."
            elif event == "writing":
                body = f"✏️ <b>Dev done</b> → writing initial draft..."
            elif event == "complete":
                body = f"✅ <b>Dev Phase complete</b> → moving to review"
            else:
                return
            self.telegram.send(f"{header}\n{body}", parse_mode="HTML")
        except Exception:
            pass

    def _write_cost_report(self):
        """Write per-agent and total cost/stats to cost_report.yaml.

        Called after every agent invocation so the webapp SSE stream can pick
        up live updates within ~2s. Writes atomically (.tmp + os.replace) so
        readers never see a partial file. Aggregates real token & USD fields
        when the claude JSON envelope was parsed; falls back to character
        counts otherwise.

        Merges with any raw_stats already on disk so restarts don't clobber
        cost history — each orchestrator process starts with an empty
        in-memory ``_agent_stats``, but the persisted ledger is the union.
        """
        if not self._agent_stats:
            return

        report_path = self.state_dir / "cost_report.yaml"

        # Merge in any previously persisted raw_stats. Dedup by
        # (timestamp, agent_type); in-memory wins on collision.
        existing_raw = []
        if report_path.exists():
            try:
                prev = yaml.safe_load(report_path.read_text()) or {}
                existing_raw = prev.get("raw_stats") or []
            except Exception:
                existing_raw = []

        in_memory_keys = {
            (s.get("timestamp"), s.get("agent_type"))
            for s in self._agent_stats
        }
        merged_stats = [
            s for s in existing_raw
            if (s.get("timestamp"), s.get("agent_type")) not in in_memory_keys
        ]
        merged_stats.extend(self._agent_stats)
        merged_stats.sort(key=lambda s: s.get("timestamp") or "")

        # Aggregate per agent type. Each bucket carries both legacy char-count
        # fields (for backwards compat with telegram_daemon / older tests) and
        # the new token + cost fields populated from claude JSON output.
        by_type = {}
        for stat in merged_stats:
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
                    "unknown_cost_calls": 0,
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
            if "cost_usd" in stat and stat["cost_usd"] is None:
                b["unknown_cost_calls"] += 1

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
            # Total is a known subtotal when a provider exposes no pricing.
            # Keep unknown costs explicit in raw_stats and mark incompleteness.
            "cost_complete": not any(d["unknown_cost_calls"] for d in by_type.values()),
            "unknown_cost_calls": sum(d["unknown_cost_calls"] for d in by_type.values()),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            # Sessions killed before OpenHands saved their usage are counted
            # by request and priced at the run's average (estimated: true).
            "estimated_cost_usd": round(sum(float(s.get("cost_usd") or 0.0)
                                            for s in merged_stats if s.get("estimated")), 6),
            "per_agent": by_type,
            "raw_stats": merged_stats[-100:],  # Keep last 100 entries
        }

        # Provider-billed truth (baseline sampled at run() start): prior runs'
        # persisted total + this run's key-usage delta. Covers EVERYTHING the
        # provider charged (deep research, figures, utility calls, cache-price
        # drift) — total_cost_usd above is a static-price-table ESTIMATE that
        # undercounted a real user's spend by ~4x (Alessandro, 2026-07-13).
        try:
            if getattr(self, "_provider_base", None) is not None:
                from ark.provider_billing import openrouter_key_usage_usd
                _cur = openrouter_key_usage_usd()
                if _cur is not None:
                    self._provider_last_billed = round(
                        getattr(self, "_provider_prev_runs", 0.0)
                        + max(0.0, _cur - self._provider_base), 6)
            if getattr(self, "_provider_last_billed", None) is not None:
                report["provider_billed_usd"] = self._provider_last_billed
        except Exception:
            pass

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

    def _notify_idea_assessment(self, ctx_file) -> None:
        """Gate B: surface the idea-quality assessment written into
        project_context.md (novelty / contribution / scope).

        Advisory only — never blocks the run. Pings the user with the novelty
        verdict and a nudge to read the scope recommendation; escalates the ping
        to a warning when the literature says the idea is essentially solved, so
        the user hears "this may already be done" early. Fail-soft: any read/parse
        problem is swallowed.
        """
        try:
            if not ctx_file.exists():
                return
            text = ctx_file.read_text(errors="ignore")
        except Exception:  # noqa: BLE001 — advisory, never fatal
            return

        upper = text.upper()
        if "ESSENTIALLY_SOLVED" in upper:
            verdict, level = "essentially solved in prior work", "warn"
        elif "PARTIAL_OVERLAP" in upper:
            verdict, level = "partial overlap with prior work", "info"
        elif "NOVEL_ENOUGH" in upper:
            verdict, level = "looks novel enough", "info"
        else:
            # No machine token (e.g. "insufficient literature") — nothing firm
            # to report; skip the ping rather than guess.
            return

        has_scope = "scope recommendation" in text.lower()
        detail = verdict + (" · see Scope Recommendation in project context" if has_scope else "")
        self.log(f"[Gate B] Idea assessment: {detail}", "INFO")
        self.notify_progress("Idea assessment", detail, level=level)

    def _run_ethical_review(self) -> bool:
        """Pre-launch screening (Gate A) of the submitted research idea.

        Judges ethics (malicious / weaponization / explicit-sexual / anti-human)
        and basic scientific soundness (absurd / pseudoscientific / physically
        impossible). Novelty / scope / contribution are judged later by Gate B,
        after Deep Research. Verdict ladder:

          - reject       → hard block, run fails (clear ethics/soundness breach)
          - human_review → block pending a human's call (borderline ethics)
          - refine       → proceed, but flag a soundness concern to the user
          - proceed      → fine

        A passing/refine/fail-open verdict is cached at
        ``state_dir/ethical_review.json`` so resumed runs skip re-review. A
        block (reject / human_review) is NEVER cached — otherwise a resumed run
        would skip the gate and proceed.

        Returns True if the launch may proceed, False if blocked.
        """
        review_file = self.state_dir / "ethical_review.json"
        if review_file.exists():
            return True

        idea = self._research_idea or ""
        if not idea.strip():
            return True

        from ark.ethical_review import review_idea
        # Use the run's SELECTED model (same verified key as the agents), not a
        # hardcoded Anthropic default. The provider key is resolved from the
        # model prefix inside complete().
        self.log_step("Pre-launch idea review (Gate A)...", "progress")
        result = review_idea(idea, model=self.model)
        verdict = result.get("verdict", "proceed")
        category = result.get("category", "unknown")
        reason = result.get("reason", "")

        if verdict == "reject":
            # Clear ethics/soundness breach — hard block, not overridable.
            self.log_section("Idea review REJECTED — launch blocked")
            self.log(f"Category: {category}\nReason: {reason}", "RAW")
            if getattr(self, "telegram", None) and self.telegram.is_configured:
                self.telegram.send(
                    f"{self.tg_header('⛔')}\n"
                    f"<b>Idea review blocked this project</b>\n"
                    f"Category: <code>{_html.escape(str(category))}</code>\n"
                    f"Reason: {_html.escape(str(reason))}",
                    parse_mode="HTML",
                )
            # Do NOT cache a block — a resumed run must re-evaluate.
            self._sync_db(status="failed", phase="blocked_ethical",
                          error_message=f"Idea rejected ({category}): {reason}")
            # Sticky: without this, main()'s evidence gate re-derives the
            # terminal status and OVERWRITES this specific reason with the
            # generic "no deliverable" message (4c1746b8, 2026-07-16).
            self._run_fatal = f"Idea rejected ({category}): {reason}"
            return False

        if verdict == "human_review":
            # Borderline — ASK the human (webapp + Telegram). "pause" gives this
            # ethics gate an extra grace window, but it still auto-resolves: if
            # no one answers, it continues with the SAFE default (Reject/block).
            # No HITL prompt may block the run forever.
            self.log_section("Idea review — held for human review")
            self.log(f"Category: {category}\nReason: {reason}", "RAW")
            idx, reply = self.ask_user_decision(
                question="This idea was flagged for human review before launch. Approve it?",
                options=["Approve — proceed with the run", "Reject — block this run"],
                what_happened=f"Gate A flagged this idea: {reason}",
                background=[f"Category: {category}"],
                timeout=int(self.config.get("telegram_decision_timeout", defaults.TIMEOUT_HITL_DECISION)),
                default=1, kind="gate_a", timeout_action="pause", phase="gate_a",
            )
            if idx == 0:  # approved
                self.log_step("Idea review: approved by human — proceeding.", "success")
                review_file.parent.mkdir(parents=True, exist_ok=True)
                review_file.write_text(json.dumps(
                    {**result, "verdict": "proceed", "human_approved": True}, indent=2))
                return True
            self.log_section("Idea review — rejected by human")
            self._sync_db(status="failed", phase="blocked_review",
                          error_message=f"Idea rejected by human after review: {reason}")
            self._run_fatal = f"Idea rejected by human after review: {reason}"
            return False

        review_file.parent.mkdir(parents=True, exist_ok=True)
        review_file.write_text(json.dumps(result, indent=2))
        if verdict == "refine":
            # Admissible, but the reviewer flagged a soundness concern. Proceed,
            # but surface it so the user can sharpen the idea.
            self.log(f"⚠ Idea review: proceed with a concern — {reason[:120]}", "WARN")
            self.notify_progress("Idea review", reason[:200], level="warn")
            # The concern must be ENFORCED, not just logged: inject it as a
            # standing user instruction (agents read user_updates.yaml), and
            # tell the user. Previously it vanished into the log while agents
            # e.g. fabricated the very simulated data the concern was about.
            try:
                import yaml as _yaml
                from datetime import datetime as _dt
                _uf = self.state_dir / "user_updates.yaml"
                _data = _yaml.safe_load(_uf.read_text()) if _uf.exists() else {}
                _ups = (_data or {}).get("updates", [])
                _ups.append({"consumed": False, "source": "gate_a",
                             "timestamp": _dt.utcnow().isoformat(),
                             "message": (
                                 f"PRE-LAUNCH REVIEW CONSTRAINT (Gate A): {reason} "
                                 f"— address this honestly in the paper's framing and "
                                 f"methods. Never misrepresent data provenance: any "
                                 f"simulated/synthetic data MUST be labeled as such and "
                                 f"the paper must not claim real-world data collection "
                                 f"that did not happen.")})
                _uf.parent.mkdir(parents=True, exist_ok=True)
                _uf.write_text(_yaml.dump({"updates": _ups}, allow_unicode=True))
                self._chat("agent",
                           f"⚠ Pre-launch review flagged a concern: {reason[:200]} — "
                           f"the paper will address it honestly (e.g. simulated data "
                           f"will be labeled as simulated).", kind="notice")
            except Exception as _e:
                self.log(f"gate-a constraint injection failed (non-fatal): {_e}", "WARN")
        elif result.get("reviewed"):
            self.log_step(f"Idea review passed: {reason[:80]}", "success")
        else:
            # Fail-open: the review did not actually run (no key / API error /
            # unparseable). Don't claim it "passed" — report it as skipped.
            self.log(
                f"⚠ Idea review skipped ({reason[:80]}) — proceeding (fail-open)",
                "WARN",
            )
        return True

    def chat_turn(self, message: str):
        """Out-of-band chat: ONE turn of a PERSISTENT OpenHands conversation with
        full memory + file access (Claude-Code-level). The agent itself decides
        whether to answer, read, edit, or run; its tool steps stream to the chat
        live, and the final reply is posted as the answer. The conversation id is
        kept in the workspace so the next turn resumes with full context."""
        from ark.chat_agent import run_chat_turn, load_conversation_id
        self.start_telegram_listener()
        self._ensure_control_poller()
        self._set_activity("💬 Working on it…")
        model = self.config.get("model_variant") or self.model
        conv = load_conversation_id(self.code_dir)
        _icon = {"command": "$", "edit": "✎", "read": "▸", "thought": "💭",
                 "action": "•", "error": "✗"}

        def on_step(step):
            if step.type == "result":
                return  # bare tool outputs are noisy
            summary = (step.summary or "").strip()
            if not summary:
                return
            self._chat("agent", f"{_icon.get(step.type, '•')} {summary[:300]}", kind="notice")

        try:
            answer, _conv_id, _ok = run_chat_turn(
                workspace=self.code_dir, message=message, model=model,
                conversation_id=conv, on_step=on_step, log=self.log)
        except Exception as e:
            self.log(f"chat_turn failed: {e}", "ERROR")
            self._set_activity("")
            self._chat("agent", f"Hit an error: {str(e)[:200]}", kind="message")
            return
        self._set_activity("")
        # No answer means the agent stopped early (provider error, timeout),
        # not that it finished. "Done." here told 954d7885's owner their audit
        # request ran when DeepSeek had refused the very first call.
        self._chat("agent", answer or (
            "This request did not finish. The agent stopped before it could "
            "reply. Any error it hit is shown above."), kind="message")
        # Budget sync: fold this turn's LLM cost into cost_report.yaml + the DB.
        try:
            from ark.chat_agent import read_conversation_usage
            self._record_chat_cost(read_conversation_usage(self.code_dir, _conv_id), _conv_id)
        except Exception as e:
            self.log(f"chat cost sync skipped: {e}", "WARN")

    def _record_chat_cost(self, usage: dict | None, conversation_id: str | None = None):
        """Fold a chat turn's cost into the project Budget.

        OpenHands persists the conversation's CUMULATIVE cost; we keep the last
        recorded totals in the workspace (.ark_chat_cost.json) and record only
        the delta as a 'chat' agent stat, then regenerate the cost report —
        which also _sync_db's total_cost_usd, so the webapp Budget card updates
        right after the turn.
        """
        # DSH has no price list, so a turn can carry tokens with no cost.
        if not usage:
            return
        import json as _json
        keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
        marker = Path(self.code_dir) / ".ark_chat_cost.json"
        stored = {}
        try:
            stored = _json.loads(marker.read_text())
        except Exception:
            pass
        markers = stored.get("conversations")
        if not isinstance(markers, dict):
            from ark.chat_agent import CONV_FILE
            legacy_file = Path(self.code_dir) / CONV_FILE
            legacy_id = legacy_file.read_text().strip() if legacy_file.is_file() else ""
            markers = {legacy_id: stored} if stored else {}
        key = conversation_id or ""
        prev = markers.get(key, {})
        delta = {k: (usage.get(k) or 0) - (prev.get(k) or 0) for k in keys}
        cost = float(usage.get("known_cost_usd", usage.get("cost_usd")) or 0)
        cost_delta = cost - float(prev.get("known_cost_usd", prev.get("cost_usd")) or 0)
        if cost_delta <= 0 and not any(value > 0 for value in delta.values()):
            return  # nothing new (e.g. turn failed before any LLM call)
        if "unpriced_turns" in usage:
            unpriced = usage["unpriced_turns"] > prev.get("unpriced_turns", 0)
        else:
            unpriced = usage.get("cost_usd") is None
        self._agent_stats.append({
            "agent_type": "chat",
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": 0,
            "prompt_len": 0,
            "output_len": 0,
            "input_tokens": int(delta["input_tokens"]),
            "output_tokens": int(delta["output_tokens"]),
            "cache_read_tokens": int(delta["cache_read_tokens"]),
            "cache_creation_tokens": int(delta["cache_creation_tokens"]),
            "cost_usd": None if unpriced else round(cost_delta, 6),
        })
        try:
            from ark.orchestrator.state import _atomic_write_text
            markers[key] = {k: usage.get(k) or 0 for k in keys}
            markers[key].update(known_cost_usd=cost, unpriced_turns=usage.get("unpriced_turns", 0))
            _atomic_write_text(marker, _json.dumps({"conversations": markers}))
        except Exception:
            pass
        self._write_cost_report()
        spent = ("tokens only, cost unknown" if unpriced else f"${cost_delta:.4f}")
        self.log(f"Chat turn {spent} — added to project budget", "INFO")

    def _apply_context(self) -> str:
        title = self.config.get("title", "") or self.project_name
        return (f"Project: {title}\n"
                f"Paper LaTeX: paper/main.tex (and includes). Figures: paper/figures/. "
                f"Plotting scripts: scripts/. Experiment results: results/.\n"
                f"Read the files you need before editing.")

    def apply_instruction(self, instruction: str, scope: str = "edit"):
        """Apply ONE targeted change WITHOUT the full review→plan→execute→evaluate
        loop — the lightweight re-entry for chat instructions on a finished project.

        scope='edit'       → a single writer (prose/LaTeX) or coder (figures) agent
                             makes just this change, then recompile. Seconds–a minute.
        scope='experiment' → scoped: run the requested experiment for real, regenerate
                             its figure, fold results into the paper. Needs compute,
                             but not a whole iteration.
        """
        self.check_dependencies()
        self.start_telegram_listener()
        self._ensure_control_poller()
        # 'answer' is read-only Q&A (Claude-Code-level: read the real files, reply);
        # it gets its own framing — nothing is changed or recompiled.
        if scope == "answer":
            self._set_activity("Looking into your question…")
            self._chat("agent", "🔍 Let me check the actual files…", kind="notice")
            try:
                self._apply_answer(instruction)
            except Exception as e:
                self.log(f"apply answer failed: {e}", "ERROR")
                self._chat("agent", f"Couldn't look that up: {str(e)[:200]}", kind="message")
            self._set_activity("")
            return
        nice = "experiment" if scope == "experiment" else "edit"
        self._set_activity(f"Applying your {nice}…")
        self._chat("agent", f"On it — applying your {nice} directly (no full iteration).", kind="notice")
        try:
            if scope == "experiment":
                self._apply_experiment(instruction)
            else:
                self._apply_edit(instruction)
        except Exception as e:
            self.log(f"apply_instruction failed: {e}", "ERROR")
            self._chat("agent", f"Hit an error applying that: {str(e)[:200]}", kind="notice")
            return
        self._set_activity("")
        self._chat("agent", "Done — applied your change and recompiled. ✅", kind="milestone")
        # Budget sync: run_agent recorded the stats; write the report so the
        # webapp Budget reflects this apply without waiting for a full iteration.
        try:
            self._write_cost_report()
        except Exception as e:
            self.log(f"apply cost sync skipped: {e}", "WARN")

    def _apply_answer(self, question: str):
        """Read-only investigation: a Claude agent reads the real artifacts (PDF,
        LaTeX, results) and answers accurately — no files are changed."""
        ctx = self._apply_context()
        out = self.run_agent(
            "writer",
            (f"The user asked a question about their FINISHED paper:\n\n\"{question}\"\n\n{ctx}\n\n"
             f"INVESTIGATE for real before answering — read the compiled PDF "
             f"(paper/main.pdf; use pdfinfo / pdftotext as needed), paper/main.tex, and "
             f"results/ files, using whatever shell commands you need. Then give the user a "
             f"direct, accurate, concise answer grounded in what you actually found.\n\n"
             f"This is STRICTLY READ-ONLY: do NOT edit, create, move, or delete any file. "
             f"End your reply with the answer itself (it is shown to the user verbatim)."),
            timeout=defaults.TIMEOUT_PAGE_ADJUSTMENT)
        ans = (out or "").strip()
        self._chat("agent", ans[:3000] if ans else "I couldn't find a clear answer in the files.",
                   kind="message")

    def _report_delivery_checks(self):
        """Persist the audit of the current PDF and return it to the delivery decision."""
        try:
            from ark.delivery_contract import evaluate, write_report
            _dr = evaluate(Path(self.code_dir),
                           venue_pages=int(self.config.get("venue_pages") or 0), config=self.config)
            write_report(Path(self.code_dir), _dr)
            if _dr.violations:
                _summary = "; ".join(f"{f.check}: {f.detail}" for f in _dr.violations)
                self.log(f"Delivery contract: {len(_dr.hard_violations)} hard / "
                         f"{len(_dr.violations)} total violation(s) — {_summary[:300]}", "WARN")
                self._chat("agent",
                           f"⚠ Delivery checks flagged {len(_dr.violations)} issue(s): "
                           f"{_summary[:200]}", kind="notice")
            else:
                skipped = sum(f.status == "skipped" for f in _dr.findings)
                self.log(f"Delivery contract: no violations ({skipped} checks skipped)", "INFO")
            return _dr
        except Exception as _e:
            self.log(f"Delivery contract evaluation failed (non-fatal): {_e}", "WARN")

    def _compile_edited_paper(self):
        self._repair_cited_sources()
        self._ensure_clearpage_before_bibliography()
        if not self.compile_latex():
            raise RuntimeError("The edited paper failed to compile. Check the LaTeX build log.")
        limit = self.config.get("venue_pages")
        if self.config.get("layout_mode") == "strict" and limit and self._body_page_count:
            from ark.latex_geometry import get_geometry
            columns = get_geometry(self.config.get("venue_format", ""), paper_dir=self.latex_dir).get("columns", 1)
            min_pages = int(limit) - (0.15 if columns >= 2 else 0.3)
            if not min_pages <= self._body_page_count <= int(limit):
                if self._enforce_page_count(context="after requested edit") is False:
                    raise RuntimeError(f"Edited paper did not fit the strict layout "
                                       f"({self._body_page_count:.2f}/{limit} body pages).")
        if self.config.get("layout_mode") == "strict":
            self._repair_cited_sources(recheck_database=False)
            self._fix_overfull(context="after requested edit")
            if not self.compile_latex():
                raise RuntimeError("Could not compile the final edited manuscript.")
        report = self._report_delivery_checks()
        if report is None:
            raise RuntimeError("Could not verify the edited manuscript.")
        if problems := report.delivery_blockers(self.config):
            raise RuntimeError("Edited manuscript checks: " + "; ".join(f.detail for f in problems))

    def _apply_edit(self, instruction: str):
        """One focused writer/coder pass + recompile."""
        figure_words = ("figure", "plot", "chart", "diagram", "axis", "axes", "fig ",
                        "fig.", "colour", "color", "图", "图表", "坐标", "曲线", "配色")
        # Captions and figure placement live in the manuscript. A bare mention
        # of a chart used to limit those requests to editing a plotting script.
        text = instruction.lower()
        manuscript_terms = ("caption", "typeset", "page layout", "paper layout", "manuscript",
                            "bibliography", "citation", "figure placement", "on page", "to page",
                            "排版", "图注", "参考文献", "正文", "页首", "页底", "移到第")
        has_figure = any(word in text for word in figure_words)
        agent = "coder" if has_figure and not any(word in text for word in manuscript_terms) else "writer"
        ctx = self._apply_context()
        if agent == "coder":
            task = (f"A user asked for this change to the paper's FIGURES:\n\n"
                    f"\"{instruction}\"\n\n{ctx}\n\n"
                    f"Make ONLY this change — edit the relevant plotting script under "
                    f"scripts/ and respect the figure-integrity skill (never invent data). "
                    f"Do NOT touch unrelated files. Do NOT run the whole pipeline.")
        else:
            task = (f"A user asked for this change to the paper:\n\n"
                    f"\"{instruction}\"\n\n{ctx}\n\n"
                    f"Edit the manuscript and bibliography where required by this request. "
                    f"Use existing figure assets for placement or caption changes. If the "
                    f"request also changes a plot, update its source and regenerate it. "
                    f"Preserve the measured data. Inspect the resulting pages at print size.")
        self.run_agent(agent, task, timeout=defaults.TIMEOUT_PAGE_ADJUSTMENT)
        self._compile_edited_paper()

    def _apply_experiment(self, instruction: str):
        """Scoped experiment: run it for real → regen figures → fold into the paper."""
        ctx = self._apply_context()
        self._set_activity("Running the experiment…")
        self.run_agent("experimenter",
                       (f"A user asked to run/add this experiment:\n\n\"{instruction}\"\n\n{ctx}\n\n"
                        f"Implement and RUN just this experiment. Write real results to "
                        f"results/ as JSON/CSV — never fabricate numbers. Keep it scoped to "
                        f"this request; do not redo the whole experiment suite."),
                       timeout=defaults.TIMEOUT_EXPERIMENTER)
        self._set_activity("Updating figures & paper…")
        try:
            self.generate_figures()
        except Exception as e:
            self.log(f"figure regen after experiment failed: {e}", "WARN")
        self.run_agent("writer",
                       (f"New experiment results were just produced for:\n\n\"{instruction}\"\n\n{ctx}\n\n"
                        f"Update the paper to report these results — numbers strictly from the "
                        f"results/ files (figure-integrity skill). Keep edits focused on this "
                        f"experiment; don't rewrite unrelated sections."),
                       timeout=defaults.TIMEOUT_PAGE_ADJUSTMENT)
        self._compile_edited_paper()

    def run(self, *, dsh_workflow=False):
        """Run the workflow and maintain the worker lifecycle."""
        self.check_dependencies()

        # Provider-billed baseline: sample the OpenRouter key's lifetime usage
        # BEFORE any spend of this run (deep research fires before the first
        # ledger write, so a lazy baseline would miss it). Every later ledger
        # write records provider_billed_usd = prior-runs total + (current -
        # baseline): the provider's invoice truth, covering spend the per-call
        # estimate can't see (DR, figures, utility calls, cache-price drift).
        # Fail-open: no OpenRouter key / API error → estimates only.
        try:
            from ark.provider_billing import openrouter_key_usage_usd
            self._provider_base = openrouter_key_usage_usd()
            # Cross-restart accumulation mirrors the ledger merge: freeze the
            # previous report's billed total as this run's starting point.
            self._provider_prev_runs = 0.0
            _rp = self.state_dir / "cost_report.yaml"
            if _rp.exists():
                _prev = yaml.safe_load(_rp.read_text()) or {}
                self._provider_prev_runs = float(_prev.get("provider_billed_usd") or 0.0)
            self._provider_last_billed = self._provider_prev_runs or None
        except Exception:
            self._provider_base = None

        # Rehydrate any state + result files the prior VM projected but that this
        # (possibly freshly provisioned) VM's disk is missing, then resume.
        self._rehydrate_state_docs()
        self._rehydrate_result_artifacts()

        # Try to resume
        self.resume_from_checkpoint()

        self.log_section(f"{self.project_name.upper()} Started  |  Mode: {self.mode.upper()}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        self.log(f"Max iterations: {self.max_iterations}  |  Max time: {self.max_end_time.strftime('%Y-%m-%d %H:%M')}", "RAW")
        self.log(f"Log: {self.log_file}", "RAW")
        self.log("", "RAW")

        iterations_at_start = self.iteration
        try:
            if dsh_workflow:
                from ark.orchestrator.dsh_workflow import run_workflow
                iterations_at_start = run_workflow(self)
            else:
                iterations_at_start = self._run_python_workflow()
            if iterations_at_start is None:
                return

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
        paper_state = self.load_paper_state()
        final_score = paper_state.get('current_score', 0)
        status = paper_state.get('status', 'unknown')
        self.log_section(f"{self.project_name.upper()} Finished  |  Score: {final_score}/10  |  Status: {status.upper()}")
        if self._should_send_end_summary(status, self.iteration - iterations_at_start):
            self.send_notification(
                f"{self.project_name.upper()} Finished",
                f"Score: {final_score}/10 (target: {self.paper_accept_threshold}/10)\n"
                f"Iterations: {self.iteration} | Status: {status}\n\n"
                f"Reply with a new direction →\nauto-applied on next ark run",
                priority="critical",
            )
        elif status not in ("accepted",):
            self.log("End summary: no new iterations this run — not notifying "
                     "(a rerun without added budget changes nothing to report)", "INFO")
        self.log(f"Total iterations: {self.iteration}", "RAW")

    def _prepare_research_workspace(self):
        if not self._run_ethical_review():
            return False
        self.start_telegram_listener()
        self._send_session_banner()
        # Continue may skip research after the idle environment was reclaimed.
        self._ensure_project_env()
        return True

    def _run_python_workflow(self):
        if not self._prepare_research_workspace():
            return None
        if self._should_run_research_phase():
            self._run_research_phase()
        # The webapp's Continue API stores the cumulative iteration cap.
        target = self._get_max_iteration_target()
        iterations_at_start = self.iteration
        if self._should_run_dev_phase():
            self._run_dev_phase()
            if getattr(self, "_terminal_error", None):
                self._handle_terminal_error(0.0, rollback_iteration=False)
                return iterations_at_start
        while datetime.now() < self.max_end_time and self.iteration < target and not self._stop_requested:
            if not self.run_paper_iteration():
                break
        return iterations_at_start

    @staticmethod
    def _should_send_end_summary(status: str, iterations_this_run: int) -> bool:
        """Whether the end-of-run summary deserves a notification.

        Accepted runs already sent their own ACCEPTED notice. A run that
        performed zero new iterations (a rerun of a project whose cumulative
        budget is spent) has nothing new to say — under an external rerun
        wrapper that notification becomes one email per invocation, forever
        (the 2026-08-20 VIGIL_SEMANTIC inbox flood).
        """
        return status not in ("accepted",) and iterations_this_run > 0

    def _get_max_iteration_target(self) -> int:
        """Calculate the absolute iteration cap.
        
        Treats self.max_iterations as the CUMULATIVE lifetime cap,
        but ensures we never set a target below our current progress.
        """
        return max(self.max_iterations, self.iteration)
