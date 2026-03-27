"""DevMixin: development iteration loop, test runner, code reviewer, mode switching."""

import os
import re
import subprocess
import time
import yaml
from datetime import datetime
from pathlib import Path


class DevMixin:
    """Mixin providing the development iteration loop.

    Expects self to have: iteration, max_iterations, mode, model,
    project_name, code_dir, config, log, log_section, log_step_header, log_step,
    log_summary_box, run_agent, memory, state_dir, save_checkpoint,
    send_notification, ask_telegram_user, git_commit, check_user_updates,
    dev_state_file, code_review_threshold, _agent_stats.
    """

    # ========== Dev State I/O ==========

    def load_dev_state(self) -> dict:
        """Load development state from dev_state.yaml."""
        if self.dev_state_file.exists():
            try:
                with open(self.dev_state_file) as f:
                    state = yaml.safe_load(f) or {}
                tasks = state.get("tasks", [])
                state["tasks"] = [t for t in tasks if t is not None and isinstance(t, dict)]
                return state
            except Exception as e:
                self.log(f"Failed to load dev state: {e}", "WARN")
        return {
            "spec_loaded": False,
            "spec": "",
            "current_phase": "planning",
            "tasks": [],
            "test_history": [],
            "code_review_scores": [],
            "last_test_results": {},
        }

    def save_dev_state(self, state: dict):
        """Save development state to dev_state.yaml."""
        try:
            with open(self.dev_state_file, "w") as f:
                yaml.dump(state, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            self.log(f"Failed to save dev state: {e}", "WARN")

    def _load_spec(self) -> str:
        """Load project spec from config or PDF file."""
        dev_state = self.load_dev_state()
        if dev_state.get("spec"):
            return dev_state["spec"]

        spec = self.config.get("goal_anchor", "")
        if spec:
            return spec

        spec_pdf = self.config.get("spec_pdf", "")
        if spec_pdf:
            pdf_path = Path(spec_pdf)
            if not pdf_path.is_absolute():
                pdf_path = self.code_dir / pdf_path
            if pdf_path.exists():
                try:
                    import fitz
                    doc = fitz.open(str(pdf_path))
                    text = ""
                    for page in doc:
                        text += page.get_text()
                    doc.close()
                    return text[:10000]
                except Exception as e:
                    self.log(f"Failed to extract PDF spec: {e}", "WARN")

        return ""

    # ========== Dev Iteration ==========

    def run_dev_iteration(self) -> bool:
        """Execute one development iteration. Returns whether to continue."""
        self.iteration += 1
        self._iteration_start = datetime.now()

        # Load persistent user instructions (always active, never consumed)
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

        dev_state = self.load_dev_state()
        tasks = dev_state.get("tasks", [])
        total_tasks = len(tasks)
        completed_tasks = len([t for t in tasks if t.get("status") == "completed"])
        pending_tasks = len([t for t in tasks if t.get("status") in ("pending", None)])

        self.log("", "RAW")
        self.log_section(
            f"DEV ITERATION {self.iteration}/{self.max_iterations}  |  "
            f"Tasks: {completed_tasks}/{total_tasks}  |  "
            f"Pending: {pending_tasks}"
        )

        total_steps = 6
        step_num = 0

        # Phase 1: Load Spec (first iteration only)
        if self.iteration == 1 and not dev_state.get("spec_loaded"):
            step_num += 1
            self.log_step_header(step_num, total_steps, "Load Spec")
            spec = self._load_spec()
            if spec:
                dev_state["spec"] = spec[:10000]
                dev_state["spec_loaded"] = True
                self.save_dev_state(dev_state)
                self.log_step(f"Spec loaded ({len(spec)} chars)", "success")
            else:
                self.log_step("No spec found, dev_planner will work from context", "warning")
                dev_state["spec_loaded"] = True
                self.save_dev_state(dev_state)
            self.log_step_header(step_num, total_steps, "Load Spec", "end")

        # Phase 2: Dev Planning
        step_num += 1
        self.log_step_header(step_num, total_steps, "Dev Planning")
        try:
            dev_state = self._run_dev_planning_phase(dev_state)
        except Exception as e:
            self.log(f"Dev planning failed: {e}", "ERROR")
        self.log_step_header(step_num, total_steps, "Dev Planning", "end")

        # Phase 3: Coding (smart grouping)
        step_num += 1
        self.log_step_header(step_num, total_steps, "Coding")
        try:
            dev_state = self._run_coding_phase(dev_state)
        except Exception as e:
            self.log(f"Coding phase failed: {e}", "ERROR")
        self.log_step_header(step_num, total_steps, "Coding", "end")

        # Phase 4: Testing
        step_num += 1
        self.log_step_header(step_num, total_steps, "Testing")
        try:
            dev_state = self._run_test_phase(dev_state)
        except Exception as e:
            self.log(f"Test phase failed: {e}", "ERROR")
            dev_state["last_test_results"] = {
                "passed": 0, "failed": 0, "errors": 1,
                "raw_output": str(e),
            }
        self.log_step_header(step_num, total_steps, "Testing", "end")

        # Phase 5: Debug (conditional)
        test_results = dev_state.get("last_test_results", {})
        if test_results.get("failed", 0) > 0 or test_results.get("errors", 0) > 0:
            step_num += 1
            self.log_step_header(step_num, total_steps, "Debug")
            try:
                dev_state = self._run_debug_phase(dev_state)
            except Exception as e:
                self.log(f"Debug phase failed: {e}", "ERROR")
            self.log_step_header(step_num, total_steps, "Debug", "end")

        # Phase 6: Code Review
        step_num += 1
        self.log_step_header(step_num, total_steps, "Code Review")
        try:
            dev_state = self._run_code_review_phase(dev_state)
        except Exception as e:
            self.log(f"Code review phase failed: {e}", "ERROR")
        self.log_step_header(step_num, total_steps, "Code Review", "end")

        # Save state
        self.save_dev_state(dev_state)

        # Iteration summary
        tasks = dev_state.get("tasks", [])
        completed = len([t for t in tasks if t.get("status") == "completed"])
        total = len(tasks)
        review_scores = dev_state.get("code_review_scores", [])
        latest_review_score = review_scores[-1]["score"] if review_scores else 0
        test_res = dev_state.get("last_test_results", {})

        self.log("", "RAW")
        self.log_summary_box(f"Dev Iteration {self.iteration} Summary", [
            f"Tasks: {completed}/{total} completed",
            f"Tests: {test_res.get('passed', 0)} passed, {test_res.get('failed', 0)} failed, {test_res.get('errors', 0)} errors",
            f"Code Review: {latest_review_score}/10 (threshold: {self.code_review_threshold}/10)",
        ], inside_phase=False)

        # Send dev summary to Telegram
        self._send_dev_iteration_summary(dev_state)

        # Git commit
        self.git_commit(f"Dev iteration {self.iteration}: {completed}/{total} tasks, review {latest_review_score}/10")

        # Save checkpoint
        self.save_checkpoint()

        # Decision: switch to paper mode or continue dev
        if self._should_switch_to_paper(dev_state):
            self._switch_to_paper_mode()
            return True  # Continue running, now in paper mode

        # Check if all tasks done and tests pass
        all_done = all(t.get("status") == "completed" for t in tasks) if tasks else False
        tests_pass = test_res.get("failed", 0) == 0 and test_res.get("errors", 0) == 0

        if all_done and tests_pass and total > 0:
            self.log_section("DEV COMPLETE -- All tasks done, tests passing")
            self.send_notification(
                "Dev Complete",
                f"All {total} dev tasks completed, tests passing. "
                f"Code review score: {latest_review_score}/10"
            )
            reply = self.ask_telegram_user(
                f"*{self.project_name.upper()} Dev Phase Complete*\n\n"
                f"All {total} tasks done, tests passing.\n"
                f"Code review: {latest_review_score}/10\n\n"
                f"Reply 'paper' to switch to paper mode, or provide new dev tasks.",
                timeout=3600,
            )
            if reply and "paper" in reply.lower():
                self._switch_to_paper_mode()
                return True
            elif reply:
                if hasattr(self.memory, 'goal_anchor'):
                    self.memory.goal_anchor += f"\n\n## New Tasks from User\n\n{reply}"
                return True
            return False

        return True

    # ========== Dev Phases ==========

    def _run_dev_planning_phase(self, dev_state: dict) -> dict:
        """Phase 2: Dev planner breaks work into coding tasks."""
        tasks = dev_state.get("tasks", [])
        pending = [t for t in tasks if t.get("status") in ("pending", None)]
        in_progress = [t for t in tasks if t.get("status") == "in_progress"]
        completed = [t for t in tasks if t.get("status") == "completed"]

        spec_summary = dev_state.get("spec", "")[:3000]
        test_results = dev_state.get("last_test_results", {})
        review_scores = dev_state.get("code_review_scores", [])

        prompt = f"""Analyze the current development state and plan coding tasks.

## Project Spec
{spec_summary if spec_summary else '(No spec loaded -- read project files to understand the codebase.)'}

## Current Task Status
- Completed: {len(completed)} tasks
- In Progress: {len(in_progress)} tasks
- Pending: {len(pending)} tasks

## Current Tasks
{yaml.dump(tasks, default_flow_style=False, allow_unicode=True) if tasks else '(No tasks yet -- create initial task breakdown.)'}

## Latest Test Results
{yaml.dump(test_results, default_flow_style=False, allow_unicode=True) if test_results else '(No tests run yet.)'}

## Code Review Feedback
{yaml.dump(review_scores[-1], default_flow_style=False, allow_unicode=True) if review_scores else '(No reviews yet.)'}

## Instructions
1. Read auto_research/state/dev_state.yaml for full context
2. If no tasks exist, create initial task breakdown from the spec
3. If tasks exist, update them based on test results and review feedback
4. Mark new tasks as "pending", keep completed tasks unchanged
5. Each task must have: id, title, description, status, priority, depends_on
6. Save updated state to auto_research/state/dev_state.yaml
"""

        self._last_planner_output = self.run_agent("planner", prompt, timeout=1200)

        # Reload dev_state (agent may have modified it)
        dev_state = self.load_dev_state()

        tasks = dev_state.get("tasks", [])
        pending = [t for t in tasks if t.get("status") in ("pending", None)]
        self.log_step(f"Plan: {len(tasks)} total tasks, {len(pending)} pending", "success")

        return dev_state

    def _run_coding_phase(self, dev_state: dict) -> dict:
        """Phase 3: Execute coding tasks using smart grouping."""
        tasks = dev_state.get("tasks", [])
        pending = [t for t in tasks if t.get("status") in ("pending", None)]

        if not pending:
            self.log_step("No pending tasks to code", "info")
            return dev_state

        # Smart grouping: topological sort by depends_on
        groups = self._group_tasks_by_dependency(pending)

        for group_idx, group in enumerate(groups, 1):
            self.log_step(f"Coding group {group_idx}/{len(groups)}: {len(group)} tasks", "progress")

            task_descriptions = []
            for task in group:
                task_id = task.get("id", "?")
                title = task.get("title", "Untitled")
                desc = task.get("description", "")
                task_descriptions.append(f"### {task_id}: {title}\n{desc}")

            # Mark tasks as in_progress
            for task in group:
                task["status"] = "in_progress"
            self.save_dev_state(dev_state)

            prompt = f"""Implement the following coding tasks:

{chr(10).join(task_descriptions)}

## Instructions
1. Read relevant existing code to understand patterns and conventions
2. Implement each task, writing clean, well-documented code
3. Write corresponding tests for new functionality
4. After implementation, update auto_research/state/dev_state.yaml:
   - Set each completed task's status to "completed"
   - Add files_modified list to each task
5. Ensure all code compiles/imports correctly

## Task IDs to complete: {', '.join(t.get('id', '?') for t in group)}
"""

            self.run_agent("coder", prompt, timeout=1800,
                          prior_context=getattr(self, '_last_planner_output', ''))

            # Reload state (agent may have updated it)
            dev_state = self.load_dev_state()
            tasks = dev_state.get("tasks", [])

            completed_in_group = sum(
                1 for t in group
                if any(tt.get("id") == t.get("id") and tt.get("status") == "completed"
                       for tt in tasks)
            )
            self.log_step(f"Group {group_idx}: {completed_in_group}/{len(group)} completed", "success")

        return dev_state

    def _run_test_phase(self, dev_state: dict) -> dict:
        """Phase 4: Run tests and capture results."""
        test_results = self._run_tests()
        dev_state["last_test_results"] = test_results

        test_history = dev_state.get("test_history", [])
        test_history.append({
            "iteration": self.iteration,
            "passed": test_results.get("passed", 0),
            "failed": test_results.get("failed", 0),
            "errors": test_results.get("errors", 0),
            "timestamp": datetime.now().isoformat(),
        })
        dev_state["test_history"] = test_history[-20:]

        # Save test results for agent context
        test_results_file = self.state_dir / "test_results.yaml"
        try:
            with open(test_results_file, "w") as f:
                yaml.dump(test_results, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            self.log(f"Failed to save test results: {e}", "WARN")

        passed = test_results.get("passed", 0)
        failed = test_results.get("failed", 0)
        errors = test_results.get("errors", 0)
        status = "success" if failed == 0 and errors == 0 else "error"
        self.log_step(f"Tests: {passed} passed, {failed} failed, {errors} errors", status)

        return dev_state

    def _run_debug_phase(self, dev_state: dict) -> dict:
        """Phase 5: If tests fail, debugger agent analyzes and fixes."""
        test_results = dev_state.get("last_test_results", {})
        raw_output = test_results.get("raw_output", "No output captured")

        if len(raw_output) > 5000:
            raw_output = raw_output[:2500] + "\n...\n" + raw_output[-2500:]

        prompt = f"""Tests are failing. Analyze and fix the issues.

## Test Results
- Passed: {test_results.get('passed', 0)}
- Failed: {test_results.get('failed', 0)}
- Errors: {test_results.get('errors', 0)}

## Test Output
```
{raw_output}
```

## Instructions
1. Read auto_research/state/test_results.yaml for detailed results
2. Analyze each failure to identify root cause
3. Fix the source code (not the tests, unless tests are incorrect)
4. Verify imports and dependencies are correct
5. Update auto_research/state/dev_state.yaml task status if needed
"""

        self.run_agent("coder", prompt, timeout=1800)

        # Re-run tests after debug
        self.log_step("Re-running tests after debug fixes...", "progress")
        new_results = self._run_tests()
        dev_state["last_test_results"] = new_results

        test_results_file = self.state_dir / "test_results.yaml"
        try:
            with open(test_results_file, "w") as f:
                yaml.dump(new_results, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            self.log(f"Failed to save test results: {e}", "WARN")

        new_failed = new_results.get("failed", 0)
        new_errors = new_results.get("errors", 0)
        if new_failed == 0 and new_errors == 0:
            self.log_step("All tests passing after debug", "success")
        else:
            self.log_step(f"Still {new_failed} failures, {new_errors} errors after debug", "warning")

        return dev_state

    def _run_code_review_phase(self, dev_state: dict) -> dict:
        """Phase 6: Code reviewer agent evaluates quality."""
        tasks = dev_state.get("tasks", [])
        completed = [t for t in tasks if t.get("status") == "completed"]
        test_results = dev_state.get("last_test_results", {})

        prompt = f"""Review the current codebase for quality, correctness, and completeness.

## Project Status
- Tasks completed: {len(completed)}/{len(tasks)}
- Tests: {test_results.get('passed', 0)} passed, {test_results.get('failed', 0)} failed

## Instructions
1. Read the project source code and tests
2. Evaluate across these dimensions:
   - Code Quality (correctness, error handling, edge cases)
   - Test Coverage (are features adequately tested?)
   - Documentation (docstrings, comments)
   - Architecture (clean abstractions, no code duplication)
3. Provide an overall score (X/10)
4. List specific issues with suggestions
5. Save review to auto_research/state/code_review.md

## Output Format (in code_review.md)
```
# Code Review -- Iteration {self.iteration}

## Score: X/10

## Issues
### C1. [Issue title]
[Description and fix suggestion]

## Strengths
- [strength 1]

## Summary
[Overall assessment]
```
"""

        output = self.run_agent("reviewer", prompt, timeout=1200)

        score = self._parse_code_review_score(output)

        review_scores = dev_state.get("code_review_scores", [])
        review_scores.append({
            "iteration": self.iteration,
            "score": score,
            "timestamp": datetime.now().isoformat(),
        })
        dev_state["code_review_scores"] = review_scores[-20:]

        self.log_step(
            f"Code review score: {score}/10 (threshold: {self.code_review_threshold}/10)",
            "success" if score >= self.code_review_threshold else "warning",
        )

        return dev_state

    # ========== Mode Switching ==========

    def _should_switch_to_paper(self, dev_state: dict) -> bool:
        """Check if dev is done and should switch to paper mode.

        Dual condition: all tasks completed + review score >= threshold + tests passing.
        """
        tasks = dev_state.get("tasks", [])
        if not tasks:
            return False

        all_done = all(t.get("status") == "completed" for t in tasks)
        if not all_done:
            return False

        review_scores = dev_state.get("code_review_scores", [])
        if not review_scores:
            return False
        latest_score = review_scores[-1].get("score", 0)
        if latest_score < self.code_review_threshold:
            return False

        # Tests all passing (dual condition)
        test_results = dev_state.get("last_test_results", {})
        if test_results.get("failed", 0) > 0 or test_results.get("errors", 0) > 0:
            return False

        if self.config.get("auto_switch_to_paper", False):
            self.log("Auto-switching to paper mode (all conditions met)", "INFO")
            return True

        reply = self.ask_telegram_user(
            f"*{self.project_name.upper()} Dev Phase Ready*\n\n"
            f"All tasks completed, tests passing, code review {latest_score}/10.\n\n"
            f"Switch to paper mode? Reply 'yes' to switch, or provide new dev tasks.",
            timeout=1800,
        )
        if reply and any(w in reply.lower() for w in ("yes", "paper", "switch", "ok")):
            return True

        return False

    def _switch_to_paper_mode(self):
        """Transition from dev mode to paper mode."""
        self.log_section("MODE SWITCH: Dev -> Paper")
        self.mode = "paper"
        self.iteration = 0

        dev_state = self.load_dev_state()
        dev_state["paper_switch_at"] = datetime.now().isoformat()
        self.save_dev_state(dev_state)

        self.send_notification(
            "Mode Switch: Dev -> Paper",
            "Development phase complete. Switching to paper review/improvement mode.",
        )
        self.log("Switched to paper mode. Next iteration will run paper pipeline.", "INFO")

    # ========== Test Runner ==========

    def _run_tests(self) -> dict:
        """Run the project's test suite. Returns {passed, failed, errors, output}."""
        test_cmd = self.config.get("test_command", "pytest -v")
        test_timeout = self.config.get("test_timeout", 600)
        conda_env = self.config.get("conda_env", "")

        if conda_env:
            cmd = (
                f"source ~/.bashrc 2>/dev/null; "
                f"mamba activate {conda_env} 2>/dev/null || conda activate {conda_env} 2>/dev/null; "
                f"{test_cmd}"
            )
        else:
            cmd = test_cmd

        try:
            result = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True, text=True, timeout=test_timeout,
                cwd=str(self.code_dir),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            output = (result.stdout + "\n" + result.stderr).strip()
            test_results = self._parse_test_results(output)
            test_results["returncode"] = result.returncode
            test_results["raw_output"] = output[-3000:]
        except subprocess.TimeoutExpired:
            test_results = {
                "passed": 0, "failed": 0, "errors": 1,
                "returncode": -1,
                "raw_output": f"Test suite timed out ({test_timeout}s)",
            }
        except FileNotFoundError:
            test_results = {
                "passed": 0, "failed": 0, "errors": 0,
                "returncode": 0,
                "raw_output": "Test command not found or no tests directory. Skipping.",
            }
        except Exception as e:
            test_results = {
                "passed": 0, "failed": 0, "errors": 1,
                "returncode": -1,
                "raw_output": str(e)[-3000:],
            }

        return test_results

    def _parse_test_results(self, output: str) -> dict:
        """Parse test runner output into structured results."""
        results = {"passed": 0, "failed": 0, "errors": 0}

        # pytest format: "X passed, Y failed, Z error"
        match = re.search(r'(\d+)\s+passed', output)
        if match:
            results["passed"] = int(match.group(1))

        match = re.search(r'(\d+)\s+failed', output)
        if match:
            results["failed"] = int(match.group(1))

        match = re.search(r'(\d+)\s+error', output)
        if match:
            results["errors"] = int(match.group(1))

        # unittest format: "Ran X tests"
        if results["passed"] == 0 and results["failed"] == 0:
            match = re.search(r'Ran\s+(\d+)\s+tests?', output)
            if match:
                total = int(match.group(1))
                if "OK" in output:
                    results["passed"] = total
                else:
                    fail_match = re.search(r'failures=(\d+)', output)
                    err_match = re.search(r'errors=(\d+)', output)
                    results["failed"] = int(fail_match.group(1)) if fail_match else 0
                    results["errors"] = int(err_match.group(1)) if err_match else 0
                    results["passed"] = total - results["failed"] - results["errors"]

        return results

    # ========== Helper Methods ==========

    def _group_tasks_by_dependency(self, tasks: list) -> list:
        """Group tasks by dependency using topological sort.

        Returns list of groups where tasks in each group can run together.
        """
        if not tasks:
            return []

        task_map = {t.get("id"): t for t in tasks}
        pending_ids = set(task_map.keys())
        groups = []
        resolved = set()

        max_iters = len(tasks) + 1
        for _ in range(max_iters):
            if not pending_ids:
                break

            ready = set()
            for tid in pending_ids:
                task = task_map[tid]
                deps = set(task.get("depends_on", []) or [])
                if deps.issubset(resolved | (set(task_map.keys()) - pending_ids)):
                    ready.add(tid)

            if not ready:
                # Circular dependency fallback
                ready = pending_ids.copy()

            group = [task_map[tid] for tid in ready]
            groups.append(group)
            resolved.update(ready)
            pending_ids -= ready

        return groups

    def _parse_code_review_score(self, output: str) -> float:
        """Parse code review score from agent output."""
        patterns = [
            r'(?:Score|Overall\s+Score|Code\s+Review\s+Score)[:\s]+(\d+(?:\.\d+)?)\s*/\s*10',
            r'(\d+(?:\.\d+)?)\s*/\s*10',
            r'Score[:\s]+(\d+(?:\.\d+)?)',
        ]

        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                score = float(match.group(1))
                if 0 <= score <= 10:
                    return score

        # Try reading from code_review.md
        review_file = self.state_dir / "code_review.md"
        if review_file.exists():
            try:
                content = review_file.read_text()
                for pattern in patterns:
                    match = re.search(pattern, content, re.IGNORECASE)
                    if match:
                        score = float(match.group(1))
                        if 0 <= score <= 10:
                            return score
            except Exception:
                pass

        self.log("Could not parse code review score, defaulting to 5.0", "WARN")
        return 5.0

    def _send_dev_iteration_summary(self, dev_state: dict):
        """Send dev iteration summary to Telegram."""
        tasks = dev_state.get("tasks", [])
        completed = len([t for t in tasks if t.get("status") == "completed"])
        total = len(tasks)
        test_res = dev_state.get("last_test_results", {})
        review_scores = dev_state.get("code_review_scores", [])
        latest_review = review_scores[-1]["score"] if review_scores else 0

        elapsed = datetime.now() - getattr(self, '_iteration_start', datetime.now())
        elapsed_min = int(elapsed.total_seconds() / 60)

        msg = (
            f"*{self.project_name.upper()} Dev Iteration {self.iteration}*\n\n"
            f"Tasks: {completed}/{total} completed\n"
            f"Tests: {test_res.get('passed', 0)} passed, "
            f"{test_res.get('failed', 0)} failed, "
            f"{test_res.get('errors', 0)} errors\n"
            f"Code Review: {latest_review}/10\n"
            f"Elapsed: {elapsed_min}min"
        )

        self.send_notification("Dev Iteration Summary", msg)
