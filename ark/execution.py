"""ExecutionMixin: planner cycle, experiment loop, writing phase, meta-debug, self-repair."""

import json
import os
import re
import subprocess
import time
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import List


class ExecutionMixin:
    """Mixin providing execution logic for the paper improvement pipeline.

    Expects self to have: code_dir, config, log, log_step, log_summary_box,
    run_agent, memory, state_dir, action_plan_file, latest_review_file,
    findings_file, literature_file, latex_dir, figures_dir, hooks,
    _save_action_plan, _load_action_plan, compile_latex,
    _generate_figures_from_results, generate_figures, paper_accept_threshold,
    iteration, project_name, send_notification.
    """

    def _wait_for_local_results(self, max_wait_hours: float) -> bool:
        """Wait for local experiment results. Delegates to compute backend."""
        return self._compute_backend.wait_for_completion(max_wait_hours)

    def wait_for_slurm(self, max_wait_hours: float = 4, job_prefix: str = None) -> bool:
        """Wait for submitted jobs to complete. Delegates to compute backend."""
        return self._compute_backend.wait_for_completion(max_wait_hours)

    def _wait_for_slurm_jobs(self, max_wait_hours: float = 2) -> bool:
        """Wait for experiment jobs (internal shortcut). Delegates to compute backend."""
        return self._compute_backend.wait_for_completion(max_wait_hours)

    def _get_searched_lit_topics(self) -> set:
        """Return set of literature topics already searched (from literature.yaml)."""
        try:
            if self.literature_file.exists():
                data = yaml.safe_load(self.literature_file.read_text()) or {}
                searches = data.get("searches", [])
                if isinstance(searches, list):
                    return {s.get("topic", "").lower().strip()
                            for s in searches if isinstance(s, dict) and s.get("topic")}
        except Exception:
            pass
        return set()

    def run_literature_search(self, topics: list) -> str:
        """Run API-first literature search on given topics.

        1. Extract search queries from topics
        2. Search academic databases (DBLP/CrossRef/arXiv/S2)
        3. Have researcher agent select relevant papers from candidates
        4. Fetch official BibTeX and write to references.bib
        5. Update literature.yaml for writer reference
        """
        from ark.citation import (
            search_papers, extract_search_queries, format_candidates_for_agent,
            parse_agent_selection, fetch_bibtex, append_papers_to_bib,
            update_literature_yaml,
        )

        self.log_step(f"Literature search (API-first): {topics}", "progress")

        bib_path = str(self.latex_dir / "references.bib")
        literature_path = str(self.literature_file)
        paper_title = self.config.get("title", self.project_name)
        research_idea = self.config.get("research_idea", "")

        # Gather candidates from all topics
        all_candidates = []
        for topic in topics:
            topic_prompts = self.config.get("literature_search_prompts", {})
            description = topic_prompts.get(topic, topic)
            queries = extract_search_queries(topic, description)
            self.log_step(f"  Searching: {queries[:3]}", "progress")
            for q in queries[:3]:
                results = search_papers(q, max_results=10)
                all_candidates.extend(results)

        if not all_candidates:
            self.log_step("No papers found from academic databases", "warning")
            return ""

        # Deduplicate
        seen = set()
        unique = []
        for p in all_candidates:
            key = p.doi or p.title.lower()[:60]
            if key not in seen:
                seen.add(key)
                unique.append(p)
        all_candidates = unique[:15]

        self.log_step(f"  Found {len(all_candidates)} candidate papers", "progress")

        # Researcher agent selects relevant papers
        candidates_text = format_candidates_for_agent(all_candidates)
        selection_prompt = f"""
## Paper Background
Title: {paper_title}
Research idea: {research_idea}

## Candidate Papers (from academic databases — all are real, verified papers)

{candidates_text}

## Your Task

Select the papers most relevant to our research from the list above.

Output format:
SELECTED: 1, 5, 11
[1] Reason: ... | Section: Related Work
[5] Reason: ... | Section: Method
[11] Reason: ... | Section: Experiments

Rules:
- ONLY select from the numbered list above
- Do NOT suggest any papers not in the list
- For each selected paper, explain why it is relevant and where to cite it
"""
        agent_output = self.run_agent("researcher", selection_prompt, timeout=900)

        # Parse selection and write BibTeX
        selected = parse_agent_selection(agent_output, all_candidates)
        if not selected:
            self.log_step("Researcher selected no papers", "warning")
            return agent_output

        self.log_step(f"  Researcher selected {len(selected)} papers, fetching BibTeX...", "progress")
        added_keys = append_papers_to_bib(bib_path, selected)
        self.log_step(f"  Added {len(added_keys)} citations to references.bib: {added_keys}", "success")

        # Update literature.yaml
        update_literature_yaml(literature_path, selected, added_keys, agent_output)

        return agent_output

    def run_planner_cycle(self, review_output: str) -> bool:
        """Planner-driven iteration cycle (planning + execution).

        Calls run_planning_phase then _run_execute_phase.
        Returns True if successfully completed.
        """
        action_plan, planner_output = self.run_planning_phase(review_output)
        if action_plan is None:
            return False
        return self._run_execute_phase(action_plan, planner_output)

    def run_planning_phase(self, review_output: str):
        """Run the planning half: literature check + planner agent + validation.

        Returns:
            (action_plan dict, planner_output str) on success, (None, '') on failure.
        """
        # Step 0: Check for literature needs — re-bootstrap from Deep Research if needed
        needs_lit, lit_topics = self._check_needs_literature_search(review_output)
        if needs_lit:
            deep_research_file = self.state_dir / "deep_research.md"
            if deep_research_file.exists():
                self.log_step("Re-reading Deep Research report for additional citations...", "progress")
                self._bootstrap_citations_from_deep_research()
            else:
                self.log_step("No Deep Research report available for literature search", "info")

        # Step 1: Planner analyzes review
        self.log_step("Planner analyzing review...", "progress")
        findings_summary = self._load_findings_summary()

        escalations = self.memory.get_strategy_escalation()
        escalation_prompt = ""
        if escalations:
            escalation_prompt = "\n## Strategy Escalation Suggestions\n\n"
            escalation_prompt += "**Note**: The system intelligently determines whether escalation is needed based on issue type\n"
            escalation_prompt += "- Presentation/layout issues: prefer WRITING_ONLY or FIGURE_CODE_REQUIRED\n"
            escalation_prompt += "- Technical/data issues: may need EXPERIMENT_REQUIRED\n\n"
            for issue_id, info in escalations.items():
                count = info["count"]
                banned = info["banned_methods"]
                required = info["required_escalation"]
                issue_type = info.get("issue_type", "unknown")
                escalation_prompt += f"**{issue_id}** (repeated {count} times, type: {issue_type}):\n"
                if banned:
                    escalation_prompt += f"  - Tried multiple times: {', '.join(banned)}\n"
                if required:
                    escalation_prompt += f"  - Suggestion: **{required}**\n"
                escalation_prompt += "\n"

        # Page count awareness for planner
        page_constraint = ""
        venue_pages = self.config.get("venue_pages")
        page_count = getattr(self, '_body_page_count', 0)
        self._page_over_limit = False
        if venue_pages:
            if page_count and page_count > venue_pages:
                self._page_over_limit = True
            page_constraint = f"""
## PAGE LIMIT: {venue_pages} pages (body text, excluding references and appendix)

Current body pages: {page_count if page_count else 'unknown'}. {"OVER LIMIT — page reduction is a priority." if self._page_over_limit else "Within limit."}
For every writing task, instruct the writer to compile and verify the body page count stays at or under {venue_pages} pages.
"""

        # Append overfull hbox warnings to planner page constraint
        overfull = getattr(self, '_overfull_warnings', [])
        if overfull:
            significant = [w for w in overfull if self._parse_overfull_pt(w) > 3.0]
            if significant:
                page_constraint += "\n## LAYOUT WARNINGS (from LaTeX compilation)\n\n"
                page_constraint += "The following content overflows column/page margins. Include a WRITING_ONLY task to fix these:\n\n"
                for w in significant:
                    page_constraint += f"- `{w}`\n"
                page_constraint += "\n**Fix**: Wrap overflowing tables with `\\resizebox{\\linewidth}{!}{...}` or break long equations.\n"

        # Bottleneck awareness: if Technical Quality is the bottleneck and
        # score is low, tell the planner it must include experiments
        bottleneck_constraint = ""
        bottleneck = self._get_bottleneck()
        current_score = self.memory.scores[-1] if self.memory.scores else 0
        if "Technical" in bottleneck and current_score < 7.5:
            bottleneck_constraint = f"""
## BOTTLENECK: Technical Quality ({current_score}/10)

The reviewer's primary bottleneck is **Technical Quality**. Pure writing changes
(WRITING_ONLY) cannot fix this — you MUST include at least one EXPERIMENT_REQUIRED
task (e.g., add baselines, run ablations, measure new metrics). If you classify
every issue as WRITING_ONLY when the bottleneck is Technical Quality, the score
will not improve.
"""

        planner_output = self.run_agent("planner", f"""
Analyze the following review comments and generate action_plan.yaml.
{escalation_prompt}{page_constraint}{bottleneck_constraint}
## Review Comments Summary
Please read the full review report: auto_research/state/latest_review.md

## Current Findings Summary
{findings_summary}

## Tasks
1. Identify all Major Issues (M1, M2, ...) and Minor Issues (m1, m2, ...)
2. Classify each issue: EXPERIMENT_REQUIRED, FIGURE_CODE_REQUIRED, or WRITING_ONLY
3. Check strategy escalation requirements, ensure banned methods are not used
4. Generate a specific action list for each issue
5. Write results to auto_research/state/action_plan.yaml

Notes:
- EXPERIMENT_REQUIRED: requires running GPU experiments (e.g., adding perplexity measurement, supplementing benchmarks)
- FIGURE_CODE_REQUIRED: requires modifying Python plotting scripts and re-running
- WRITING_ONLY: only requires modifying LaTeX text
""", timeout=1200, prior_context=review_output)

        # Validate action plan
        action_plan = self._load_action_plan()
        valid, validation_msg = self._validate_action_plan(action_plan)
        if not valid:
            self.log(f"Action plan validation failed: {validation_msg}", "WARN")
            self.log("Retrying planner with explicit format instructions...", "INFO")
            self.run_agent("planner", f"""
The previously generated action_plan.yaml has incorrect format: {validation_msg}

Please regenerate action_plan.yaml, strictly following this format:

```yaml
issues:
  - id: "M1"
    type: "EXPERIMENT_REQUIRED"  # or FIGURE_CODE_REQUIRED or WRITING_ONLY
    title: "Issue title"
    description: "Detailed description"
    status: "pending"
    actions:
      - agent: "experimenter"
        task: "What to do"
```

IMPORTANT: Every issue MUST have all fields: id, type, title, description, status, actions.

Please read auto_research/state/latest_review.md and regenerate.
{escalation_prompt}{page_constraint}{bottleneck_constraint}
## Current Findings Summary
{findings_summary}
""", timeout=900, prior_context=review_output)
            action_plan = self._load_action_plan()

        issues = action_plan.get("issues", [])

        if not issues:
            self.log_step("Planner generated no issues, skipping", "warning")
            return None, ''

        # Hard cap: when over page limit, keep only top issues + compression task
        if getattr(self, '_page_over_limit', False) and len(issues) > 6:
            # Keep P0 (compression) + top 5 Major issues
            p0 = [i for i in issues if i.get("id") == "P0"]
            majors = [i for i in issues if i.get("id", "").startswith("M") and i.get("id") != "P0"]
            rest = [i for i in issues if i not in p0 and i not in majors]
            capped = p0 + majors[:5] + rest[:max(0, 6 - len(p0) - min(len(majors), 5))]
            dropped = len(issues) - len(capped)
            if dropped > 0:
                self.log(f"Page over limit: capped plan from {len(issues)} to {len(capped)} issues (dropped {dropped} low-priority)", "WARN")
                action_plan["issues"] = capped
                issues = capped
                self._save_action_plan(action_plan)

        exp_count = sum(1 for i in issues if i.get("type") == "EXPERIMENT_REQUIRED")
        lit_count = sum(1 for i in issues if i.get("type") == "LITERATURE_REQUIRED")
        fig_count = sum(1 for i in issues if i.get("type") == "FIGURE_CODE_REQUIRED")
        write_count = sum(1 for i in issues if i.get("type") == "WRITING_ONLY")
        self.log_step(f"Plan: {len(issues)} issues (exp:{exp_count}, lit:{lit_count}, fig:{fig_count}, write:{write_count})", "success")

        # Record repair methods and check violations
        violations = []
        for issue in issues:
            issue_id = issue.get("id", "")
            issue_type = issue.get("type", "")
            issue_desc = issue.get("title", "") or issue.get("description", "")
            if issue_id and issue_type:
                self.memory.record_repair_method(issue_id, issue_type)
                banned = self.memory.get_banned_methods(issue_id, issue_desc)
                if issue_type in banned:
                    violations.append((issue_id, issue_type, banned, issue_desc))
                    self.log(f"Violation: {issue_id} used a banned method {issue_type}!", "ERROR")

        if violations:
            self.log("Detected possible strategy improvement suggestions", "WARNING")
            tech_violations = []
            for v in violations:
                issue_id, issue_type, banned, issue_desc = v
                pres_type = self.memory.classify_issue_type(issue_desc)
                if pres_type == "presentation":
                    self.log(f"  {issue_id}: presentation issue, {issue_type} may be appropriate, continuing", "INFO")
                else:
                    self.log(f"  {issue_id}: technical issue, suggest switching to {[m for m in ['FIGURE_CODE_REQUIRED', 'EXPERIMENT_REQUIRED'] if m not in banned]}", "WARNING")
                    tech_violations.append((issue_id, issue_type, banned, issue_desc))

            # Ask user when technical issues are repeating failed strategies
            user_provided_fix = False
            if tech_violations and self.telegram.is_configured:
                viol_list = "\n".join(f"- {vid}: tried `{vtype}` again" for vid, vtype, _, _ in tech_violations)
                reply = self.ask_telegram_user(
                    f"*{self.project_name}* repeating failed strategies:\n\n"
                    f"{viol_list}\n\n"
                    f"Suggest a different approach? (auto-fixes in 15min)",
                    timeout=900,
                )
                self._asked_this_iteration = True
                if reply and reply.strip():
                    user_provided_fix = True
                elif reply is None and self.telegram.is_configured:
                    self.telegram.send(f"⏰ *{self.project_name}*: 15min timeout — auto-fixing {len(tech_violations)} violation(s), continuing.")

            # Auto-fix technical violations if user didn't provide guidance
            if tech_violations and not user_provided_fix:
                self.log("Auto-fixing banned method violations for technical issues", "INFO")
                for vid, vtype, banned, vdesc in tech_violations:
                    for issue in issues:
                        if issue.get("id") != vid:
                            continue
                        # Pick best non-banned upgrade type
                        if "EXPERIMENT_REQUIRED" not in banned:
                            upgrade_type = "EXPERIMENT_REQUIRED"
                            upgrade_agent = "experimenter"
                        elif "FIGURE_CODE_REQUIRED" not in banned:
                            upgrade_type = "FIGURE_CODE_REQUIRED"
                            upgrade_agent = "writer"
                        else:
                            upgrade_type = "EXPERIMENT_REQUIRED"
                            upgrade_agent = "experimenter"

                        old_type = issue["type"]
                        issue["type"] = upgrade_type
                        # Rewrite actions with correct agent
                        actions = issue.get("actions", [])
                        for action in actions:
                            action["agent"] = upgrade_agent
                        if not actions:
                            issue["actions"] = [{"agent": upgrade_agent, "task": issue.get("description", issue.get("title", ""))}]
                        self.memory.record_repair_method(vid, upgrade_type)
                        self.log(f"  Auto-fixed {vid}: {old_type} -> {upgrade_type} (agent: {upgrade_agent})", "INFO")
                        break

                self._save_action_plan(action_plan)
                exp_count = sum(1 for i in issues if i.get("type") == "EXPERIMENT_REQUIRED")
                fig_count = sum(1 for i in issues if i.get("type") == "FIGURE_CODE_REQUIRED")
                write_count = sum(1 for i in issues if i.get("type") == "WRITING_ONLY")
                self.log_step(f"Plan after auto-fix: {len(issues)} issues (exp:{exp_count}, fig:{fig_count}, write:{write_count})", "success")

        return action_plan, planner_output

    def _run_execute_phase(self, action_plan: dict, planner_output: str = "") -> bool:
        """Execute the action plan: literature, experiments, writing."""
        issues = action_plan.get("issues", [])

        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not hasattr(self, '_action_plan_lock'):
            self._action_plan_lock = threading.Lock()

        def _run_literature_issue(issue):
            """Handle a literature issue by re-reading Deep Research report.

            Does NOT do independent keyword searches. All citations come from
            the Deep Research report (bootstrapped in Round 1). If reviewer
            asks for more literature, we re-extract from the same report and
            search API for any papers not yet in references.bib.
            """
            from ark.citation import (
                bootstrap_citations, append_papers_to_bib,
            )

            if self._quota_exhausted:
                return

            self.log_step(f"Literature: {issue.get('id')} - {issue.get('title', '')[:30]}", "progress")
            issue["status"] = "in_progress"
            with self._action_plan_lock:
                self._save_action_plan(action_plan)

            # Re-read Deep Research report and re-bootstrap any missing citations
            deep_research_file = self.state_dir / "deep_research.md"
            if deep_research_file.exists():
                self.log_step("  Re-reading Deep Research report for additional citations...", "progress")
                self._bootstrap_citations_from_deep_research()
            else:
                self.log_step("  No Deep Research report available, skipping", "warning")

            issue["status"] = "completed"
            with self._action_plan_lock:
                self._save_action_plan(action_plan)

        def _run_experiment_issue(issue):
            """Execute a single experiment issue."""
            if self._quota_exhausted:
                return
            self.log_step(f"Experiment: {issue.get('id')} - {issue.get('title', '')[:30]}", "progress")
            try:
                self._run_experiment_task(issue, action_plan)
            except Exception as e:
                self.log(f"Experiment task {issue.get('id')} failed: {e}", "ERROR")
                issue["status"] = "failed"
                issue["failure_reason"] = str(e)
                with self._action_plan_lock:
                    self._save_action_plan(action_plan)

        lit_issues = [i for i in issues if i.get("type") == "LITERATURE_REQUIRED" and i.get("status", "pending") == "pending"]
        exp_issues_pending = [i for i in issues if i.get("type") == "EXPERIMENT_REQUIRED" and i.get("status", "pending") == "pending"]

        if lit_issues or exp_issues_pending:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = []
                if lit_issues:
                    self.log_step(f"Queueing {len(lit_issues)} literature tasks (parallel)", "progress")
                    for issue in lit_issues:
                        futures.append(executor.submit(_run_literature_issue, issue))
                if exp_issues_pending:
                    self.log_step(f"Queueing {len(exp_issues_pending)} experiment tasks (parallel)", "progress")
                    for issue in exp_issues_pending:
                        futures.append(executor.submit(_run_experiment_issue, issue))
                # Wait for all to complete
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        self.log(f"Parallel task error: {e}", "ERROR")

        # Step 3: Check all experiments done
        all_experiments_done = all(
            issue.get("status") in ["completed", "failed", "skipped"]
            for issue in issues
            if issue.get("type") == "EXPERIMENT_REQUIRED"
        )

        if all_experiments_done:
            self.log_step("Experiments done, starting writing phase...", "progress")
            writing_ok = self._run_writing_phase(action_plan, prior_context=planner_output)
            return writing_ok

        self.log_step("Some experiments incomplete", "warning")
        return False

    def _run_experiment_task(self, issue: dict, action_plan: dict):
        """Execute a single experiment issue. No retry loop — next iteration handles retries."""
        lock = getattr(self, '_action_plan_lock', None)
        issue["status"] = "in_progress"
        if lock:
            with lock:
                self._save_action_plan(action_plan)
        else:
            self._save_action_plan(action_plan)

        exp_task = issue.get("description", issue.get("title", "Unknown task"))
        actions = issue.get("actions", [])
        if actions:
            for action in actions:
                if action.get("agent") == "experimenter":
                    exp_task = action.get("task", exp_task)
                    break

        # 1. Setup compute
        compute_ctx = self._compute_backend.setup()
        compute_instructions = self._compute_backend.get_agent_instructions()

        # Check for referenced file paths that don't exist
        file_existence_warning = ""
        path_pattern = re.compile(r'(?:references/|data/|experiments/|code/)[\w/.-]+')
        referenced_paths = path_pattern.findall(exp_task + " " + issue.get("description", ""))
        missing_paths = []
        for ref_path in set(referenced_paths):
            full_path = self.code_dir / ref_path
            if not full_path.exists():
                missing_paths.append(ref_path)
        if missing_paths:
            file_existence_warning = (
                "\n\n## WARNING: Referenced paths do not exist!\n"
                "The following paths mentioned in the task do NOT exist on disk:\n"
                + "\n".join(f"- {p} (NOT FOUND at {self.code_dir / p})" for p in missing_paths)
                + "\n\n**Do NOT fabricate synthetic data to substitute for missing real data.** "
                "Instead, report that the required data files are missing and skip "
                "any analysis that depends on them. Output a clear error message "
                "explaining what files are needed.\n"
            )
            self.log(f"WARNING: Experiment references missing paths: {missing_paths}", "WARN")

        try:
            # 2. Experimenter runs the experiment (includes result analysis)
            exp_output = self.run_agent("experimenter", f"""
Task: {exp_task}

Issue context:
- ID: {issue.get('id')}
- Title: {issue.get('title')}
- Description: {issue.get('description', 'N/A')}
{file_existence_warning}
{compute_instructions}

After running the experiment:
1. Check whether the experiment completed successfully (no OOM, no errors)
2. Check whether result files were generated
3. Evaluate whether the data supports the paper's arguments
4. Update auto_research/state/findings.yaml with new findings
""", timeout=1800)

            # 3. Wait for jobs
            self.log_step("Waiting for experiment completion...", "info")
            self._compute_backend.wait_for_completion(max_wait_hours=2)

            # 4. Collect results
            self._compute_backend.collect_results()

        finally:
            self._compute_backend.teardown()

        # 5. Mark done — reviewer in next iteration decides if more work needed
        issue["status"] = "completed"
        if lock:
            with lock:
                self._save_action_plan(action_plan)
        else:
            self._save_action_plan(action_plan)
        self.log_step(f"Issue {issue.get('id')} completed", "success")
        self._generate_figures_from_results()

    def _get_page_constraint_warning(self) -> str:
        """Return a page constraint warning string for the writer."""
        venue_pages = self.config.get("venue_pages")
        if not venue_pages:
            return ""

        result = f"""
## PAGE LIMIT: {venue_pages} pages (body text only, excluding references and appendix)

After making all changes, you MUST verify the page count:
1. Compile with `pdflatex -interaction=nonstopmode main.tex` (run twice for stable layout)
2. Check the body page count (pages before the References section)
3. If over {venue_pages} pages: move less essential subsections to `\\appendix`, condense verbose text
4. If under {venue_pages} pages: you have room — do NOT pad with filler
5. Repeat compile-and-check until body pages = {venue_pages} or fewer
"""

        # Append overfull hbox warnings
        overfull = getattr(self, '_overfull_warnings', [])
        if overfull:
            significant = [w for w in overfull if self._parse_overfull_pt(w) > 3.0]
            if significant:
                result += "\n## LAYOUT WARNINGS (from LaTeX compilation)\n\n"
                result += "The following content overflows column/page margins. **You must fix these:**\n\n"
                for w in significant:
                    result += f"- `{w}`\n"
                result += "\n**Fix**: Wrap overflowing tables with `\\resizebox{\\linewidth}{!}{...}` or break long equations.\n"

        return result

    @staticmethod
    def _parse_overfull_pt(warning: str) -> float:
        """Extract the pt value from an Overfull hbox warning."""
        m = re.search(r'([\d.]+)pt', warning)
        return float(m.group(1)) if m else 0.0

    def _run_writing_phase(self, action_plan: dict, prior_context: str = ""):
        """Execute writing phase for all writing tasks."""
        issues = action_plan.get("issues", [])
        issues = [i for i in issues if i is not None and isinstance(i, dict)]

        latex_dir_name = self.config.get("latex_dir", "Latex")
        writing_tasks = []

        for issue in issues:
            if issue.get("type") == "WRITING_ONLY":
                writing_tasks.append({
                    "id": issue.get("id") or "?",
                    "title": issue.get("title") or "",
                    "description": issue.get("description") or "",
                    "actions": issue.get("actions") or [],
                })
                issue["status"] = "in_progress"
            elif issue.get("type") == "EXPERIMENT_REQUIRED" and issue.get("status") == "completed":
                for action in issue.get("actions", []):
                    if action.get("agent") == "writer":
                        writing_tasks.append({
                            "id": issue.get("id"),
                            "title": f"Integrate experiment results for {issue.get('title')}",
                            "description": action.get("task", ""),
                            "actions": [action],
                            "_from_experiment": True,
                        })
            elif issue.get("type") == "LITERATURE_REQUIRED" and issue.get("status") == "completed":
                for action in issue.get("actions", []):
                    if action.get("agent") == "writer":
                        writing_tasks.append({
                            "id": issue.get("id"),
                            "title": f"Update {issue.get('title')} based on literature survey",
                            "description": action.get("task", ""),
                            "actions": [action],
                        })
                        self.log(f"  Added literature writing task: {issue.get('id')} - write literature content into LaTeX", "INFO")

        if not writing_tasks:
            self.log_step("No writing tasks to execute", "info")
            return True

        writing_tasks = [t for t in writing_tasks if t is not None]
        task_list = []
        for i, task in enumerate(writing_tasks, 1):
            task_list.append(f"""
### Task {i}: {task.get('id') or '?'} - {task.get('title') or ''}
{task.get('description') or ''}
""")

        # Classify: FIGURE_CODE_REQUIRED vs WRITING_ONLY
        figure_tasks = [i for i in issues if i.get("type") == "FIGURE_CODE_REQUIRED"]

        # Phase 0.5: Nano Banana concept figures (if enabled)
        if figure_tasks and self.config.get("figure_generation") == "nano_banana":
            concept_tasks = []
            matplotlib_tasks = []
            for task in figure_tasks:
                title = (task.get("title") or "").lower()
                desc = (task.get("description") or "").lower()
                is_concept = any(kw in title + " " + desc for kw in [
                    "concept", "architecture", "overview", "mechanism",
                    "illustration", "diagram", "workflow", "pipeline",
                ])
                if is_concept:
                    concept_tasks.append(task)
                else:
                    matplotlib_tasks.append(task)

            if concept_tasks:
                self.log_step(f"Routing {len(concept_tasks)} concept figure tasks to Nano Banana", "progress")
                self._generate_nano_banana_figures()
                for task in concept_tasks:
                    task["status"] = "completed"
                self._save_action_plan(action_plan)

            # Only keep matplotlib tasks for the standard figure code path
            figure_tasks = matplotlib_tasks

        # Phase 1: FIGURE_CODE_REQUIRED tasks
        if figure_tasks:
            self.log_step(f"Processing {len(figure_tasks)} FIGURE_CODE_REQUIRED tasks (modifying Python code)", "progress")

            figure_instructions = []
            for task in figure_tasks:
                task_id = task.get("id", "")
                task_title = task.get("title", "")
                task_desc = task.get("description", "")
                actions = task.get("actions", [])

                target_file = self.config.get("create_figures_script", "scripts/create_paper_figures.py")
                target_function = ""
                modification = task_desc

                for action in actions:
                    if "target_file" in action:
                        target_file = action["target_file"]
                    if "target_function" in action:
                        target_function = action["target_function"]
                    if "modification" in action:
                        modification = action["modification"]

                figure_instructions.append(f"""
### {task_id}: {task_title}

**CRITICAL**: You must modify Python code, not LaTeX!

**Target file**: {target_file}
**Target function**: {target_function if target_function else "See task description"}
**Modification needed**: {modification}

**Detailed task**:
{task_desc}

**Action steps**:
1. Use the Read tool to read {target_file}
2. Find the corresponding function (e.g., fig3_palu_distribution)
3. Use the Edit tool to modify matplotlib parameters (figsize, fontsize, tight_layout, etc.)
4. Confirm that the modified code has correct syntax
5. Do not run the script (the system will run it automatically)
""")

            self.run_agent("writer", f"""
## CRITICAL TASK: Modify Python plotting scripts (not LaTeX!)

You received {len(figure_tasks)} FIGURE_CODE_REQUIRED tasks.
These tasks require modifying **Python code**, not LaTeX files!

{''.join(figure_instructions)}

## Tool usage requirements
- Must use the Read tool to read {target_file}
- Must use the Edit tool to modify code
- Do not use Bash to run scripts (the system will run them automatically)

## Function name reference
- Figure 1: fig1_overview
- Figure 2: fig2_sdpa_latency
- Figure 3: fig3_palu_distribution
- Figure 4: fig4_root_cause
- Figure 5: fig5_repair_tradeoff
- Figure 6: fig6_e2e_performance

## Verification
After modifications, ensure:
1. Python syntax is correct (no indentation errors, brackets match)
2. matplotlib parameters are reasonable (figsize, fontsize, etc.)
3. File paths are correct ({latex_dir_name}/figures/*.pdf)
""", timeout=1800, prior_context=prior_context)

            if self._quota_exhausted:
                self.log("Aborting writing phase: API quota exhausted", "ERROR")
                return False

            # Verify Python script was modified
            self.log_step("Verifying Python code changes...", "progress")
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", target_file],
                    capture_output=True, text=True, cwd=self.code_dir
                )
                if target_file in result.stdout:
                    self.log_step("Python code modified successfully", "success")
                else:
                    self.log_step("WARNING: FIGURE_CODE_REQUIRED task but Python not modified!", "error")
                    self.log("This indicates Writer agent did not correctly execute the task.", "WARN")
            except Exception as e:
                self.log(f"Verification failed: {e}", "WARN")

            # Regenerate figures
            self.log_step("Regenerating figures with modified code...", "progress")
            try:
                result = subprocess.run(
                    ["python", target_file],
                    capture_output=True, text=True, timeout=120,
                    cwd=self.code_dir
                )
                if result.returncode == 0:
                    self.log_step("Figures regenerated successfully", "success")
                    self.compile_latex()
                else:
                    self.log_step(f"Figure generation failed: {result.stderr[:200]}", "error")
            except Exception as e:
                self.log_step(f"Figure generation error: {e}", "error")

        # Phase 2: Writing tasks (split by priority)
        high_priority_tasks = []
        batch_tasks = []

        for task in writing_tasks:
            if task is None:
                continue
            # Only experiment-derived tasks need individual calls (different context)
            if task.get("_from_experiment"):
                high_priority_tasks.append(task)
            else:
                batch_tasks.append(task)

        self.log_step(f"Writing tasks: {len(high_priority_tasks)} high-priority (individual), {len(batch_tasks)} batch", "progress")

        # Phase 2A: Individual high-priority tasks
        for task in high_priority_tasks:
            task_id = (task.get('id') or '?') if isinstance(task, dict) else '?'
            task_title = (task.get('title') or '') if isinstance(task, dict) else ''
            task_desc = (task.get('description') or '') if isinstance(task, dict) else ''
            self.log_step(f"Writing (individual): {task_id} - {task_title[:40]}", "progress")

            literature_context = self._get_literature_context_for_task(task)

            page_warning = self._get_page_constraint_warning()
            self.run_agent("writer", f"""
You have only one task to complete. Please complete it carefully and thoroughly.
{page_warning}
## Task: {task_id} - {task_title}
{task_desc}
{literature_context}

## Requirements
1. Carefully read the relevant sections in {latex_dir_name}/main.tex
2. Make substantial modifications (not minor tweaks)
3. Ensure LaTeX syntax is correct after modifications
4. Keep the paper's core contributions unchanged
5. Reference the high-quality paper style in paper_example/ directory

## Reference Files
- auto_research/state/latest_review.md - Review report
- auto_research/state/literature.yaml - Literature survey results
- {latex_dir_name}/references.bib - References library
""", timeout=1800, prior_context=prior_context)

            if self._quota_exhausted:
                self.log("Aborting writing phase: API quota exhausted", "ERROR")
                return False

            # Verify changes
            try:
                diff_output = subprocess.run(
                    ["git", "diff", "--stat", f"{latex_dir_name}/main.tex"],
                    capture_output=True, text=True, cwd=self.code_dir
                ).stdout
                if diff_output.strip():
                    self.log_step(f"  ✓ {task_id}: changes detected", "success")
                else:
                    self.log_step(f"  ✗ {task_id}: No changes detected! Task may have failed.", "error")
            except Exception as e:
                self.log(f"  Verification error: {e}", "WARN")

        # Phase 2B: Batch normal tasks
        if batch_tasks:
            self.log_step(f"Processing {len(batch_tasks)} batch writing tasks", "progress")

            task_list = []
            for idx, task in enumerate(batch_tasks, 1):
                task_list.append(f"""
### Task {idx}: {task.get('id', '')} - {task.get('title', '')}
{task.get('description', '')}
""")

            page_warning = self._get_page_constraint_warning()
            self.run_agent("writer", f"""
Please update the paper {latex_dir_name}/main.tex according to the following review revision tasks.
{page_warning}
## Revision Task List

{''.join(task_list)}

## Notes
1. Ensure LaTeX syntax is correct after each modification
2. Keep the paper's core contributions unchanged
3. Reference the high-quality paper style in paper_example/ directory

## Reference Files
- auto_research/state/latest_review.md - Review report
- auto_research/state/action_plan.yaml - Complete task list
""", timeout=3600, prior_context=prior_context)

        # Verify total modifications
        total_added = 0
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--numstat", f"{latex_dir_name}/main.tex"],
                capture_output=True, text=True, cwd=self.code_dir
            )
            if diff_result.stdout.strip():
                for line in diff_result.stdout.strip().split("\n"):
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[0].isdigit():
                        total_added += int(parts[0])

            if total_added < 5:
                self.log(f"Writing phase only added {total_added} lines, modifications may be insufficient", "WARN")
            else:
                self.log(f"Writing phase added {total_added} lines total", "INFO")
        except Exception as e:
            self.log(f"Writing quality verification failed: {e}", "WARN")

        # Only mark tasks completed if meaningful work was done
        if total_added >= 5:
            for issue in issues:
                if issue.get("type") in ["WRITING_ONLY", "FIGURE_CODE_REQUIRED"]:
                    issue["status"] = "completed"
            self._save_action_plan(action_plan)
            self.log_step("Writing phase completed", "success")

            # Post-writing page count check: if still over limit, one compression pass
            venue_pages = self.config.get("venue_pages")
            if venue_pages and not self._quota_exhausted:
                self.compile_latex()
                page_count = getattr(self, '_body_page_count', 0)
                if page_count and page_count > venue_pages:
                    self.log(f"Post-writing compression needed: {page_count} body pages (limit {venue_pages})", "WARN")
                    self.run_agent("writer", f"""
## PAGE COMPRESSION — venue limit is {venue_pages} body pages

The paper body currently exceeds the limit. Reduce it to {venue_pages} pages or fewer.
Move less essential subsections to \\appendix, condense verbose text, merge overlapping sections.
Do NOT add new content — only compress.

After each round of changes:
1. Compile with `pdflatex -interaction=nonstopmode main.tex` (run twice)
2. Count body pages (before References section)
3. If still over {venue_pages}, compress more
4. Stop when body pages <= {venue_pages}
""", timeout=1800)

                    if not self._quota_exhausted:
                        self.compile_latex()
                        final_count = getattr(self, '_body_page_count', 0)
                        if final_count and final_count > venue_pages:
                            self.log(f"WARNING: Paper still at {final_count} body pages after compression (limit {venue_pages})", "ERROR")
                        elif final_count:
                            self.log(f"Page count OK: {final_count}/{venue_pages} body pages", "INFO")
                elif page_count:
                    self.log(f"Page count OK: {page_count}/{venue_pages} body pages", "INFO")

            return True
        else:
            reason = "API quota exhausted" if self._quota_exhausted else f"only {total_added} lines added"
            self.log(f"Writing phase failed ({reason}), tasks NOT marked completed", "ERROR")
            self._save_action_plan(action_plan)
            return False

    def _get_literature_context_for_task(self, task: dict) -> str:
        """Read literature.yaml and extract context relevant to the current task."""
        if not self.literature_file.exists():
            return ""

        try:
            lit_data = yaml.safe_load(self.literature_file.read_text()) or {}

            entries = lit_data.get("entries", [])
            if not entries:
                entries = lit_data.get("papers", [])
            if not entries:
                entries = lit_data.get("references", [])
            if not entries and isinstance(lit_data, list):
                entries = lit_data

            if not entries:
                for key, val in lit_data.items():
                    if isinstance(val, list) and len(val) > 0:
                        entries = val
                        break

            if not entries:
                return ""

            lines = ["\n## Available Literature Resources (from Literature Agent)\n"]
            for entry in entries[:20]:
                if isinstance(entry, dict):
                    title = entry.get("title", "")
                    bibtex_key = entry.get("bibtex_key", entry.get("key", entry.get("cite_key", "")))
                    abstract = entry.get("abstract", entry.get("summary", ""))
                    venue = entry.get("venue", "")
                    year = entry.get("year", "")
                    if title:
                        cite_str = f" (cite: \\cite{{{bibtex_key}}})" if bibtex_key else ""
                        venue_str = f" [{venue} {year}]" if venue else ""
                        lines.append(f"- **{title}**{cite_str}{venue_str}")
                        if abstract:
                            lines.append(f"  Abstract: {abstract[:400]}")
                elif isinstance(entry, str):
                    lines.append(f"- {entry}")

            # Add [NEEDS-CHECK] citations
            needs_check = lit_data.get("needs_check", [])
            if needs_check:
                lines.append("\n## [NEEDS-CHECK] Citations (unverified — mark in text)")
                for nc in needs_check:
                    if isinstance(nc, dict):
                        nc_title = nc.get("title", "")
                        nc_key = nc.get("bibtex_key", "")
                        if nc_title and nc_key:
                            lines.append(f"- **{nc_title}** (cite: \\cite{{{nc_key}}}) — **[NEEDS-CHECK]**")

            if len(lines) > 1:
                lines.append(f"\nPlease cite these references in Related Work and other appropriate sections.")
                lines.append(f"BibTeX entries are in {self.config.get('latex_dir', 'Latex')}/references.bib, please use \\cite{{}} to cite.")
                lines.append(f"\nDo NOT remove existing \\cite{{}} commands. When revising, only add or modify — never delete existing citations.")
                lines.append(f"\n[NEEDS-CHECK] papers are treated as normal citations. Use them where appropriate based on content relevance.")
                return "\n".join(lines)
            return ""
        except Exception:
            return ""

    def _get_bottleneck(self) -> str:
        """Extract bottleneck dimension from latest_review.md score table.

        Parses the | Dimension | Score | table and returns the dimension
        with the lowest score. Falls back to 'Unknown' on parse failure.
        """
        if not self.latest_review_file.exists():
            return "Unknown"

        try:
            content = self.latest_review_file.read_text()
            # Parse score table rows: | Dimension | X.X/10 | ... |
            row_pattern = re.compile(
                r"\|\s*([^|*]+?)\s*\|\s*(\d+(?:\.\d+)?)\s*/\s*10\s*\|"
            )
            dimensions = []
            for match in row_pattern.finditer(content):
                dim_name = match.group(1).strip()
                score = float(match.group(2))
                # Skip header, total, and separator rows
                if dim_name.lower() in ("dimension", "total", "---", "--------"):
                    continue
                dimensions.append((dim_name, score))

            if dimensions:
                # Return the dimension with the lowest score
                bottleneck = min(dimensions, key=lambda x: x[1])
                return bottleneck[0]
        except Exception as e:
            self.log(f"Failed to extract bottleneck: {e}", "WARN")

        return "Unknown"

    # ==================== Meta-Debugger ====================

    def run_meta_debugger(self, trigger_reason: str) -> str:
        """Run Meta-Debugger for system diagnosis and repair.

        Returns:
            "CONTINUE" | "CONTINUE_WITH_FIX" | "PAUSE"
        """
        self.log(f"Triggering Meta-Debugger: {trigger_reason}", "META")

        diagnosis_ctx = self.memory.get_diagnosis_context()
        health_status, health_reasons = self.memory.get_health_status()

        ctx_summary = f"""
## Trigger Reason
{trigger_reason}

## System Health Status
Status: **{health_status}**
{"Reasons: " + ", ".join(health_reasons) if health_reasons else "No anomalies"}

## Score Trend
- Current: {diagnosis_ctx['scores']['current']}/10
- Best: {diagnosis_ctx['scores']['best']}/10
- Trend: {diagnosis_ctx['scores']['trend']}
- Recent: {' -> '.join(f"{s:.1f}" for s in diagnosis_ctx['scores']['recent'][-5:])}

## Stagnation Status
- Stagnation count: {diagnosis_ctx['stagnation']['count']}
- Is stagnating: {diagnosis_ctx['stagnation']['is_stagnating']}
- Reason: {diagnosis_ctx['stagnation']['reason']}

## Issue Repetition
- High repeat (7+ times): {diagnosis_ctx['issues']['high_repeat']}
- Medium repeat (3+ times): {diagnosis_ctx['issues']['repeat_issues'][:5]}

## Experiment Idle Runs
- Idle run count: {diagnosis_ctx['experiment_empty_count']}
"""

        diagnosis_output = self.run_agent("meta_debugger", f"""
{ctx_summary}

Please perform a complete system diagnosis:

1. **Read key state files**:
   - auto_research/state/memory.yaml
   - auto_research/state/action_plan.yaml
   - auto_research/state/latest_review.md

2. **Analyze recent execution logs** (check auto_research/logs/ directory)

3. **Check execution consistency**:
   - Run `git diff scripts/create_paper_figures.py` to check FIGURE_CODE tasks
   - Run `git status` to check which files were modified

4. **Identify problem patterns**:
   - Are there cases of "correct plan but failed execution"?
   - Is the system stuck in a "method loop"?
   - Are strategy escalation rules being violated?

5. **Generate diagnosis report** to auto_research/state/meta_diagnosis.md

6. **If state issues are found, fix them directly** (ONLY these file types):
   - Reset erroneous accumulations in memory.yaml
   - Fix malformed action_plan.yaml
   - Edit agent prompt files (*.prompt) to improve instructions

**FORBIDDEN — do NOT modify**:
   - Any Python source code (.py files)
   - Any shell scripts (.sh files)
   - Any configuration outside auto_research/state/

Modifying .py files risks breaking the pipeline. If you find a Python bug,
describe it in meta_diagnosis.md — a human will fix it.

**Important**: Diagnosis must find root causes, not just symptoms. Fixes must be specific, not just suggestions.
""", timeout=1800)

        # Safety: revert any .py files the agent may have modified
        self._revert_py_modifications()

        diagnosis_file = self.state_dir / "meta_diagnosis.md"
        if diagnosis_file.exists():
            try:
                diagnosis_file.read_text()
                self.log("Meta-Debugger completed diagnosis, continuing iteration", "WARNING")
                self.memory.load()
                return "CONTINUE_WITH_FIX"
            except Exception as e:
                self.log(f"Failed to read diagnosis report: {e}", "ERROR")

        return "CONTINUE"

    def _revert_py_modifications(self):
        """Revert any .py file changes made by meta-debugger.

        Meta-debugger should only modify state files (.yaml, .md, .prompt).
        If it touched .py files, revert them with git checkout.
        """
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, timeout=10,
                cwd=self.code_dir,
            )
            if result.returncode != 0:
                return

            changed = result.stdout.strip().split("\n") if result.stdout.strip() else []
            py_files = [f for f in changed if f.endswith(".py")]
            if py_files:
                self.log(f"Meta-debugger modified .py files (forbidden): {py_files}", "WARN")
                subprocess.run(
                    ["git", "checkout", "--"] + py_files,
                    capture_output=True, timeout=10,
                    cwd=self.code_dir,
                )
                self.log(f"Reverted {len(py_files)} .py file(s)", "WARN")
        except Exception as e:
            self.log(f"Failed to check/revert .py modifications: {e}", "WARN")

    def check_and_trigger_meta_debug(self) -> str:
        """Check if Meta-Debugger should be triggered, and run it if so."""
        should_trigger, reason = self.memory.should_trigger_meta_debug()
        if should_trigger:
            return self.run_meta_debugger(reason)
        return "CONTINUE"

    def self_repair(self, stagnation_reason: str) -> bool:
        """Self-repair: re-plan strategy when stagnating.

        Includes meta-reflection check: if last repair was ineffective,
        escalate strategy instead of repeating.
        """
        self.log("Starting self-repair process...", "REPAIR")

        current_score = self.memory.scores[-1] if self.memory.scores else 0.0
        stagnation_count = self.memory.stagnation_count
        bottleneck = self._get_bottleneck()

        last_repair_effective, ineffective_reason = self.memory.was_last_repair_effective()
        repeat_issues = self.memory.get_repeat_issues(threshold=3)

        self.log(f"Bottleneck dimension: {bottleneck}", "REPAIR")
        if not last_repair_effective:
            self.log(f"Meta-reflection warning: last repair was ineffective - {ineffective_reason}", "REPAIR")
        if repeat_issues:
            self.log(f"Repeated Issues: {[r[0] for r in repeat_issues]}", "REPAIR")

        meta_reflection = ""
        if not last_repair_effective or repeat_issues:
            meta_reflection = f"""
### Meta-Reflection Warning - Must Change Approach!

**Last repair was ineffective**: {ineffective_reason}

**Recurring Issues** (these problems have been attempted multiple times but remain unresolved):
"""
            for issue_id, count in repeat_issues:
                meta_reflection += f"- **{issue_id}**: appeared {count} times\n"

            meta_reflection += """
**Mandatory Requirements**:
1. Do not use the same repair method as before
2. Analyze why previous repairs were ineffective
3. Adopt a completely different strategy:
   - If previously editing text -> now run experiments
   - If previously adjusting formatting -> now modify Python code to regenerate figures
   - If figure issues keep recurring -> must use FIGURE_CODE_REQUIRED (modify plotting scripts and re-run)
"""

        repair_prompt = f"""
## Self-Repair Mode - Bottleneck Breakthrough

**Stagnation reason**: {stagnation_reason}
**Current score**: {current_score}/10
**Target score**: {self.paper_accept_threshold}/10
**Consecutive stagnation count**: {stagnation_count}
**Bottleneck dimension**: {bottleneck}
{meta_reflection}

### Bottleneck Analysis

Determine repair strategy based on bottleneck dimension:

| Bottleneck | Strategy |
|------|------|
| Technical Quality | **Must add experiment tasks** (pure writing cannot break through) |
| Presentation | Focus on improving figures (use FIGURE_CODE_REQUIRED) |
| Innovation | Re-frame contribution |
| Writing Quality | Rewrite key sections |

### Required Steps

1. Read auto_research/state/action_plan.yaml, analyze current task structure
2. Read auto_research/state/latest_review.md, understand the reviewer's specific suggestions
3. Check repeated Issues, analyze why previous repairs were ineffective

### Mandatory Requirements

- If bottleneck is **Technical Quality** and < 7.5:
  - Must add EXPERIMENT_REQUIRED tasks

- If bottleneck is **Presentation** or figure-related Issues keep recurring:
  - Must use FIGURE_CODE_REQUIRED (not WRITING_ONLY!)
  - Must modify {self.config.get("create_figures_script", "scripts/create_paper_figures.py")}
  - Must run scripts to regenerate figures

- If all tasks are WRITING_ONLY:
  - This is a danger sign, must add experiment or FIGURE_CODE tasks

**Important**: This is an automatically triggered repair process; you need to make autonomous decisions, do not wait for human instructions.

**Output**: Update auto_research/state/action_plan.yaml, ensure there are breakthrough tasks targeting the {bottleneck} bottleneck.
"""

        self.run_agent("planner", repair_prompt)

        self.memory.mark_repair_attempt(self.iteration)

        if last_repair_effective:
            self.memory.stagnation_count = max(0, stagnation_count - 3)
            self.log(f"Repair effective, stagnation count reduced by 3: {stagnation_count} -> {self.memory.stagnation_count}", "REPAIR")
        else:
            self.log(f"Last repair was ineffective, stagnation count unchanged: {stagnation_count}", "REPAIR")

        self.memory.save()

        self.log("Self-repair completed", "REPAIR")
        return True
