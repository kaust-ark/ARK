"""AgentMixin: agent execution, output parsing, rate limit handling."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import yaml
from datetime import datetime, timedelta
from pathlib import Path


def _parse_claude_json(stdout: str) -> dict | None:
    """Parse output of `claude --output-format json`. Returns None on any failure.

    Tolerates trailing whitespace and the rare case where stdout has leading
    non-JSON debug output by scanning for the final result-shaped object.
    Never raises — callers fall back to treating stdout as plain text.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last-resort: locate the final result envelope
        marker = '{"type":"result"'
        start = text.rfind(marker)
        if start == -1:
            return None
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            return None


def _extract_usage(parsed: dict) -> dict:
    """Pull token/cost fields out of parsed claude JSON. Zero-default so callers
    don't need null checks. Always returns a complete dict shape."""
    parsed = parsed or {}
    u = parsed.get("usage") or {}
    model_usage = parsed.get("modelUsage") or {}
    model = next(iter(model_usage), "")
    return {
        "model": model,
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0),
        "cost_usd": float(parsed.get("total_cost_usd") or 0.0),
        "duration_api_ms": int(parsed.get("duration_api_ms") or 0),
    }


def _parse_gemini_json(stdout: str) -> dict | None:
    """Parse output of `gemini -o json`. Returns None on failure."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        # gemini -o json usually outputs the JSON object directly,
        # but may have leading "Loaded cached credentials" etc.
        if "{" in text:
            text = text[text.find("{"):]
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _calculate_gemini_cost(model_id: str, input_tok: int, output_tok: int) -> float:
    """
    Calculate estimated cost for Gemini models (April 2026 pricing).
    """
    input_tok = int(input_tok or 0)
    output_tok = int(output_tok or 0)
    model_lower = (model_id or "").lower()

    # Pricing per 1M tokens
    if "3.1-pro" in model_lower:
        in_rate = 2.00
        out_rate = 12.00
    elif "3.1-flash" in model_lower:
        in_rate = 0.50
        out_rate = 3.00
    else:
        # Default to pro
        in_rate = 2.00
        out_rate = 12.00

    return (input_tok / 1_000_000 * in_rate) + (output_tok / 1_000_000 * out_rate)


def _extract_gemini_usage(parsed: dict) -> dict:
    """Aggregate token usage info from Gemini CLI's nested stats schema."""
    parsed = parsed or {}
    stats = parsed.get("stats", {})
    models = stats.get("models") or stats.get("model") or {}

    total_in = 0
    total_out = 0
    total_cached = 0
    total_thoughts = 0
    total_latency = 0
    main_model = ""

    for mid, info in models.items():
        t = info.get("tokens", {})
        total_in += int(t.get("input") or 0)
        total_out += int(t.get("candidates") or 0)
        total_cached += int(t.get("cached") or 0)
        total_thoughts += int(t.get("thoughts") or 0)
        
        api = info.get("api", {})
        total_latency += int(api.get("totalLatencyMs") or 0)
        
        # Heuristic for the "main" model being used for the response
        if "roles" in info and "main" in info["roles"]:
            main_model = mid
    
    if not main_model and models:
        main_model = next(iter(models))

    cost_usd = _calculate_gemini_cost(main_model, total_in, total_out)

    return {
        "model": main_model,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_tokens": total_cached,
        "cache_creation_tokens": 0, # Gemini schema doesn't distinguish creation
        "cost_usd": cost_usd,
        "duration_api_ms": total_latency,
    }


def _fmt_tok(n: int) -> str:
    """Format a token count as compact human-readable (e.g. 12.3k, 1.2M)."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)

from ark.ui import (
    ElapsedTimer, RateLimitCountdown, agent_styled, styled, Style, Icons,
)


# ── Blocking-command watchdog ─────────────────────────────
# Patterns that indicate a child process will block forever.
# Matched against the full command line of descendant processes.
_BLOCKING_PATTERNS = re.compile(
    r"(?:^|\s)(?:"
    r"tail\s+(?:.*\s)?(?:-[fF]|--follow)"  # tail -f / tail -F / tail --follow
    r"|watch\s"                              # watch <cmd>
    r"|top(?:\s|$)"                          # top
    r"|htop(?:\s|$)"                         # htop
    r"|less(?:\s|$)"                         # less
    r"|more(?:\s|$)"                         # more
    r"|vi(?:m)?(?:\s|$)"                     # vi / vim
    r"|nano(?:\s|$)"                         # nano
    r"|emacs(?:\s|$)"                        # emacs
    r")"
)


def _get_descendant_pids(parent_pid: int) -> list:
    """Get all descendant PIDs of a process (children, grandchildren, etc.)."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(parent_pid)],
            capture_output=True, text=True, timeout=5,
        )
        children = [int(p) for p in result.stdout.strip().split() if p.isdigit()]
        all_descendants = list(children)
        for child in children:
            all_descendants.extend(_get_descendant_pids(child))
        return all_descendants
    except Exception:
        return []


def _kill_blocking_descendants(parent_pid: int, log_fn=None) -> int:
    """Find and kill any blocking descendant processes. Returns count killed."""
    killed = 0
    for pid in _get_descendant_pids(parent_pid):
        try:
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if not cmdline_path.exists():
                continue
            cmdline = cmdline_path.read_bytes().replace(b'\x00', b' ').decode(errors='replace')
            if _BLOCKING_PATTERNS.search(cmdline):
                os.kill(pid, signal.SIGTERM)
                killed += 1
                if log_fn:
                    log_fn(f"  Watchdog killed blocking process (PID {pid}): {cmdline[:80]}", "WARN")
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return killed


class _BlockingCommandWatchdog:
    """Background thread that periodically scans for and kills blocking child processes."""

    def __init__(self, parent_pid: int, log_fn=None, interval: int = 30):
        self._parent_pid = parent_pid
        self._log_fn = log_fn
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        # Wait a bit before first check — give the agent time to start
        if self._stop.wait(timeout=self._interval):
            return
        while not self._stop.wait(timeout=self._interval):
            _kill_blocking_descendants(self._parent_pid, self._log_fn)

# Per-agent context profiles: controls what context each agent type receives.
# memory: iteration history, score trends, escalation suggestions
# deep_research: Gemini Deep Research background report (up to 8KB)
# prior_context: output from the previous agent in the pipeline chain
# context_files: generic file references (research_state, findings, etc.)
# Note: project-specific knowledge is now written directly into agent prompt files
# during the Specialization step (Research Phase Step 3). The runtime injection of
# project_context has been replaced by Template-Specialization architecture.
# `user_instructions`: inject auto_research/state/user_instructions.yaml as its own
# section, independent of memory. Only researcher gets this — it acts as the
# "compiler" that derives user intent into config.yaml / project_context.md /
# customized agent prompts. Downstream agents pick up the derived artifacts via
# their normal channels (prior_context, context_files). Ongoing instructions
# added mid-run are still surfaced to planner/reviewer via memory.goal_anchor.
AGENT_CONTEXT_PROFILES = {
    "researcher":     {"memory": False, "deep_research": False, "prior_context": False, "context_files": True,  "user_instructions": True},
    "reviewer":       {"memory": True,  "deep_research": False, "prior_context": False, "context_files": False, "user_instructions": False},
    "planner":        {"memory": True,  "deep_research": False, "prior_context": True,  "context_files": False, "user_instructions": False},
    "writer":         {"memory": False, "deep_research": True,  "prior_context": True,  "context_files": False, "user_instructions": False},
    "experimenter":   {"memory": False, "deep_research": True,  "prior_context": False, "context_files": True,  "user_instructions": False},
    "coder":          {"memory": False, "deep_research": False, "prior_context": True,  "context_files": False, "user_instructions": False},
}

# Default profile for unknown agent types (conservative: include everything)
_DEFAULT_PROFILE = {"memory": True, "deep_research": True, "prior_context": True, "context_files": True, "user_instructions": False}


class AgentMixin:
    """Mixin providing agent execution capabilities.

    Expects self to have: agents_dir, code_dir, model, log, log_step,
    log_summary_box, save_checkpoint, send_notification, memory,
    action_plan_file, latest_review_file, config, _rate_limit_notified,
    _agent_empty_count, _agent_stats.
    """

    def _build_path_boundary(self) -> str:
        """Build a path restriction directive for agent system prompts."""
        return (
            f"CRITICAL PATH RESTRICTION: You are working on project '{self.project_name}'. "
            f"You MUST only read and write files within: {self.code_dir}\n"
            f"NEVER access, read, modify, or reference files outside this directory. "
            f"If a task requires files outside this path, report it and stop."
        )

    def _get_ark_model(self) -> str | None:
        """
        Return the ARK model name to pass to ``claude --model``.

        Reads ``model_variant`` from the project config (e.g.
        ``"claude-sonnet-4-6"``) — that's the actual CLI model name.
        Falls back to ``model`` only if it already looks like a real model
        identifier (contains a dash); a bare ``"claude"`` is the *backend*
        type, not a CLI model name, and would make the CLI exit 1 with
        "There's an issue with the selected model".

        Returns None to let the Claude CLI use its built-in default.
        """
        variant = self.config.get("model_variant")
        if variant:
            return variant
        legacy = self.config.get("model")
        if legacy and "-" in legacy:
            return legacy
        return None

    def _kill_process_tree(self, pid: int):
        """Kill a process and all its descendants (including the process itself).

        Uses SIGKILL on the entire process group (since agents run with
        start_new_session=True, pid == pgid) to ensure no orphans survive.
        """
        # First try to kill the entire process group
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        # Also kill individual descendants in case pgid differs
        descendants = _get_descendant_pids(pid)
        for child_pid in reversed(descendants):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        # Kill the process itself
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _cleanup_cli_state(self):
        """Clean up Claude CLI state after abnormal termination (e.g. SIGHUP).

        Removes lock files and stale state that can cause subsequent calls to fail silently.
        """
        import glob as globmod
        from pathlib import Path
        claude_dir = Path.home() / ".claude"
        cleaned = []
        # Remove common lock/state files that may be left behind
        for pattern in ["*.lock", "tmp/*", ".session*"]:
            for f in claude_dir.glob(pattern):
                try:
                    if f.is_file():
                        f.unlink()
                        cleaned.append(str(f))
                except OSError:
                    pass
        # Kill any orphaned claude processes
        try:
            import subprocess as sp
            result = sp.run(["pkill", "-f", "claude.*--no-session-persistence"],
                          capture_output=True, timeout=5)
        except Exception:
            pass
        if cleaned:
            self.log(f"  Cleaned up {len(cleaned)} files: {cleaned}", "INFO")
        # Brief pause to let things settle
        time.sleep(5)

    def _parse_rate_limit_wait(self, error_msg: str) -> int:
        """Parse wait time (seconds) from rate limit error message.

        Supports formats:
        - "retry after 60 seconds"
        - "retry after 2026-01-25T14:30:00"
        - "reset at 1706188200" (Unix timestamp)
        - "wait 5 minutes"

        Returns:
            Wait seconds; defaults to 300 (5 min) on parse failure.
        """
        error_lower = error_msg.lower()

        # Format 1: "retry after X seconds" or "wait X seconds"
        match = re.search(r"(?:retry after|wait)\s+(\d+)\s*(?:seconds?|s)", error_lower)
        if match:
            return int(match.group(1))

        # Format 2: "X minutes"
        match = re.search(r"(?:retry after|wait)\s+(\d+)\s*(?:minutes?|m)", error_lower)
        if match:
            return int(match.group(1)) * 60

        # Format 3: ISO timestamp "2026-01-25T14:30:00"
        match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", error_msg)
        if match:
            try:
                reset_time = datetime.fromisoformat(match.group(1))
                wait_seconds = (reset_time - datetime.now()).total_seconds()
                return max(int(wait_seconds), 60)
            except ValueError:
                pass

        # Format 4: Unix timestamp
        match = re.search(r"reset.*?(\d{10})", error_lower)
        if match:
            try:
                reset_time = datetime.fromtimestamp(int(match.group(1)))
                wait_seconds = (reset_time - datetime.now()).total_seconds()
                return max(int(wait_seconds), 60)
            except (ValueError, OSError):
                pass

        # Default: 5 minutes
        return 300

    def _summarize_agent_output(self, agent_type: str, output: str) -> list:
        """Generate summary lines for agent output."""
        if not output or len(output) < 50:
            return []

        summary_lines = []

        if agent_type == "reviewer":
            if self.latest_review_file.exists():
                content = self.latest_review_file.read_text()

                score_match = re.search(r"\*\*Total\*\*.*?\*\*(\d+\.?\d*)/10\*\*", content, re.DOTALL)
                if score_match:
                    summary_lines.append(f"Score: {score_match.group(1)}/10")

                rating_match = re.search(r"\*\*Rating:\s*([^*\n]+)", content)
                if rating_match:
                    summary_lines.append(f"Rating: {rating_match.group(1).strip()}")

                dimensions = [
                    ("Technical Quality", "Tech"),
                    ("Paper Presentation", "Pres"),
                    ("Innovation", "Innov"),
                    ("Writing Quality", "Write"),
                ]
                dim_scores = []
                for eng, abbr in dimensions:
                    dim_match = re.search(rf"\|\s*{eng}\s*\|[^|]*\|\s*(\d+)/10", content)
                    if dim_match:
                        dim_scores.append(f"{abbr}:{dim_match.group(1)}")
                if dim_scores:
                    summary_lines.append(" | ".join(dim_scores))

                major_issues = re.findall(r"### M\d+\.\s*([^\n]+)", content)
                if major_issues:
                    summary_lines.append(f"Major Issues ({len(major_issues)}):")
                    for issue in major_issues[:3]:
                        summary_lines.append(f"  • {issue.strip()[:40]}")

                minor_issues = re.findall(r"### m\d+\.\s*([^\n]+)", content)
                if minor_issues:
                    summary_lines.append(f"Minor Issues: {len(minor_issues)}")

        elif agent_type == "writer":
            if "main.tex" in output.lower():
                summary_lines.append(f"Modified: {self.config.get('latex_dir', 'Latex')}/main.tex")
            fig_changes = re.findall(r"fig\d+|figure\s*\d+", output, re.IGNORECASE)
            if fig_changes:
                summary_lines.append(f"Figures touched: {len(set(fig_changes))}")

        elif agent_type == "experimenter":
            slurm_jobs = re.findall(r"sbatch|srun|slurm", output, re.IGNORECASE)
            if slurm_jobs:
                summary_lines.append(f"Slurm jobs submitted: {len(slurm_jobs)}")
            cloud_ops = re.findall(r"ssh\s|rsync\s|aws\s|gcloud\s|az\s", output, re.IGNORECASE)
            if cloud_ops:
                summary_lines.append(f"Cloud operations: {len(cloud_ops)}")
            local_runs = re.findall(r"python\s+\S+\.py|nohup\s|bash\s+\S+\.sh", output)
            if local_runs:
                summary_lines.append(f"Local scripts: {len(local_runs)}")

        elif agent_type == "planner":
            if self.action_plan_file.exists():
                try:
                    with open(self.action_plan_file) as f:
                        plan = yaml.safe_load(f) or {}
                    issues = plan.get("issues", [])
                    if issues:
                        exp_count = sum(1 for i in issues if i.get("type") == "EXPERIMENT_REQUIRED")
                        write_count = sum(1 for i in issues if i.get("type") == "WRITING_ONLY")
                        summary_lines.append(f"Issues: {len(issues)} total")
                        summary_lines.append(f"  Experiments: {exp_count}, Writing: {write_count}")
                except Exception:
                    pass

        return summary_lines

    def run_agent(self, agent_type: str, task: str, timeout: int = 1800,
                  prior_context: str = "") -> str:
        """Run an agent of the specified type, returning its output.

        Args:
            agent_type: Agent type (matches prompt file name).
            task: The task description for the agent.
            timeout: Max execution time in seconds.
            prior_context: Output from the previous agent in the pipeline chain.
                           Only included if the agent's context profile allows it.
        """
        import json

        prompt_file = self.agents_dir / f"{agent_type}.prompt"
        if not prompt_file.exists():
            raise FileNotFoundError(f"Agent prompt not found: {prompt_file}")

        base_prompt = prompt_file.read_text()

        # Look up context profile for this agent type
        profile = AGENT_CONTEXT_PROFILES.get(agent_type, _DEFAULT_PROFILE)

        # Build context sections based on profile
        context_sections = []

        # Memory / iteration history
        if profile["memory"]:
            history_context = self.memory.get_context_for_agent(agent_type)
            if history_context:
                context_sections.append(f"## Iteration History (Memory)\n\n{history_context}")

        # User Instructions — user intent captured at launch / restart / continue.
        # Only agents marked `user_instructions: True` (currently researcher) see this
        # directly; researcher derives the content into config.yaml, project_context.md,
        # and customized agent prompts so downstream agents pick it up naturally.
        if profile.get("user_instructions"):
            ui_file = self.state_dir / "user_instructions.yaml"
            if ui_file.exists():
                try:
                    ui_data = yaml.safe_load(ui_file.read_text()) or {}
                    entries = ui_data.get("instructions", []) or []
                    messages = [e.get("message", "").strip() for e in entries if e.get("message")]
                    if messages:
                        rendered = "\n".join(f"- {m}" for m in messages)
                        context_sections.append(
                            "## User Instructions (from launch form / webapp)\n\n"
                            "The user submitted the following guidance. You are the project's\n"
                            "compiler — do NOT copy these instructions verbatim into downstream\n"
                            "artifacts. Instead, interpret intent and derive each instruction\n"
                            "into the appropriate destination so downstream agents pick it up\n"
                            "through their normal channels:\n\n"
                            "- API keys / tokens → write to `config.yaml`\n"
                            "- Experiment constraints (N, baselines, success criteria) →\n"
                            "  bake into `project_context.md` → `## Experimental Protocol`\n"
                            "- Style / claim discipline (e.g. \"don't over-claim\") →\n"
                            "  append to the target agent's `## Project-Specific Knowledge`\n"
                            "- Preferred libraries / skills → record rationale in\n"
                            "  `selected_skills_rationale.md`\n"
                            "- Fallback rules → add to Protocol `Failure contingency`\n\n"
                            "If an instruction cannot be safely derived (e.g. it contradicts\n"
                            "the idea or a safety rule), surface it in `needs_human.json`\n"
                            "rather than silently ignoring it.\n\n"
                            f"{rendered}"
                        )
                except Exception as e:
                    self.log(f"Could not read user_instructions.yaml: {e}", "WARN")

        # Prior context from previous agent
        if profile["prior_context"] and prior_context:
            # Truncate if very long
            pc = prior_context if len(prior_context) <= 6000 else (
                prior_context[:3000] + "\n\n... (truncated) ...\n\n" + prior_context[-3000:]
            )
            context_sections.append(f"## Prior Agent Output\n\n{pc}")

        # Deep Research report — the file path is surfaced in the Context
        # Files section below; we do NOT inject the report body here. Agents
        # have Read and can load the full report on demand, avoiding the
        # signal loss that came from truncating an 8000-char slice into every
        # agent's system prompt.

        # Context file references
        if profile["context_files"] or profile["deep_research"]:
            dr_line = ""
            if profile["deep_research"]:
                dr_line = (
                    "- auto_research/state/deep_research.md - Gemini Deep Research "
                    "background report (Read in full when researching related work,\n"
                    "  baselines, or technical background — do not skim)\n"
                )
            context_sections.append(
                "## Context Files\n\n"
                "Please read the following files for context (if they exist):\n"
                "- auto_research/state/research_state.yaml - Current research state\n"
                "- auto_research/state/findings.yaml - Existing findings\n"
                f"{dr_line}"
                "- auto_research/state/project_context.md - Project requirements and setup\n"
                "- report.md - Research report\n"
                "- results/ directory - Experiment results"
            )

        # Assemble full prompt
        context_block = "\n\n".join(context_sections) if context_sections else ""
        full_prompt = f"""{base_prompt}

---

## CRITICAL RULES — Shell Commands

NEVER run blocking or interactive commands. They will hang the pipeline forever.
Banned commands: `tail -f`, `tail --follow`, `watch`, `top`, `htop`, `less`, `more`,
`vim`, `nano`, `python` (interactive REPL without script), `cat` (on pipes/devices),
`sleep` > 30s, `read`, any command that waits for stdin or runs indefinitely.

Instead:
- To check file contents: use `cat file` or `head -n 50 file` (NOT `tail -f`)
- To monitor jobs: use `squeue` once (NOT `watch squeue`)
- To wait for results: just exit — the system handles waiting automatically
- To run scripts: use `python script.py` (NOT interactive `python`)

## Current Task

{task}

{context_block}

Execute the task and update the corresponding files.
"""

        # Brief task description for logging
        task_brief = task.split("\n")[0][:50].strip()
        if len(task.split("\n")[0]) > 50:
            task_brief += "..."
        self.log_step(f"{Icons.for_agent(agent_type)} Agent [{agent_type}] → {task_brief}", "progress")

        start_time = time.time()
        timer = ElapsedTimer(agent_type)

        MAX_RETRIES = 2
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                cmd = []
                # Strip CLAUDECODE to prevent nested-session detection.
                # Strip GEMINI_API_KEY / GOOGLE_API_KEY so the Gemini CLI uses                                            
                # OAuth credentials from ~/.gemini/oauth_creds.json rather than                                    
                # the API key (which is only for Deep Research via Python API).                                           
                _strip = {"CLAUDECODE", "GEMINI_API_KEY", "GOOGLE_API_KEY"} 
                env = {k: v for k, v in os.environ.items() if k not in _strip}
                if self.model == "gemini":
                    boundary = self._build_path_boundary()
                    cmd = [
                        "gemini",
                        "-p", f"[SYSTEM RULE] {boundary}\n\n{full_prompt}",
                        "--approval-mode", "auto_edit",
                        "-o", "json",
                    ]
                    # Respect model_variant if set
                    ark_model = self._get_ark_model()
                    if ark_model:
                        cmd.extend(["-m", ark_model])
                    else:
                        cmd.extend(["-m", "auto"])
                elif self.model == "claude":
                    cmd = [
                        "claude", "-p", full_prompt,
                        "--permission-mode", "bypassPermissions",
                        "--no-session-persistence",
                        "--output-format", "json",
                        "--append-system-prompt", self._build_path_boundary(),
                    ]
                    ark_model = self._get_ark_model()
                    if ark_model:
                        cmd.extend(["--model", ark_model])
                elif self.model == "codex":
                    boundary = self._build_path_boundary()
                    cmd = [
                        "codex", "exec",
                        "--dangerously-bypass-approvals-and-sandbox",
                        "-C", str(self.code_dir),
                        f"[SYSTEM RULE] {boundary}\n\n{full_prompt}",
                    ]
                else:
                    self.log(f"Unsupported model backend: {self.model}", "ERROR")
                    return ""

                ark_model = self._get_ark_model()
                self.log(f"Backend model: {self.model} | Model: {ark_model or 'default'}", "INFO")

                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,  # Don't hold terminal pty fd
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=self.code_dir,
                    env=env,
                    start_new_session=True,  # isolate from parent's SIGHUP
                )

                # Start watchdog to kill blocking child processes (tail -f, watch, etc.)
                watchdog = _BlockingCommandWatchdog(process.pid, log_fn=self.log)
                watchdog.start()

                timer.start()
                result = ""
                stdout = ""
                stderr = ""
                usage_record = None  # populated when claude returns parseable JSON

                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                    # claude --output-format json: parse the envelope, extract `result`
                    # field for downstream and `usage` for cost tracking. Fall back to
                    # raw stdout on parse failure so the existing empty-run / failure
                    # paths still trigger normally.
                    if self.model == "claude":
                        parsed = _parse_claude_json(stdout)
                        if parsed is not None:
                            result = parsed.get("result", "") or ""
                            usage_record = _extract_usage(parsed)
                        else:
                            result = stdout
                    elif self.model == "gemini":
                        parsed = _parse_gemini_json(stdout)
                        if parsed is not None:
                            result = parsed.get("response", "") or ""
                            usage_record = _extract_gemini_usage(parsed)
                        else:
                            result = stdout
                    else:
                        result = stdout

                    if stderr:
                        stderr_lower = stderr.lower()
                        if "rate limit" in stderr_lower or "rate_limit" in stderr_lower:
                            wait_seconds = self._parse_rate_limit_wait(stderr)
                            wait_minutes = wait_seconds / 60
                            self.log(f"Rate limit detected: need to wait {wait_minutes:.1f} minutes", "WARN")

                            if not self._rate_limit_notified:
                                self._rate_limit_notified = True
                                self.save_checkpoint()
                                self.send_notification(
                                    f"Rate Limit",
                                    f"{agent_type} rate-limited, waiting {wait_minutes:.0f}min\n"
                                    f"Recovery: ~{(datetime.now() + timedelta(seconds=wait_seconds)).strftime('%H:%M')}",
                                    priority="critical"
                                )

                            self.log(f"Waiting {wait_minutes:.1f} minutes before auto-recovery...", "INFO")
                            RateLimitCountdown(wait_seconds + 10).run()
                            self._rate_limit_notified = False
                        elif "set an auth method" in stderr_lower:
                            self.log(f"  [{agent_type}] Auth Error: Authentication not configured. Please export GEMINI_API_KEY='...'", "ERROR")
                        elif "error" in stderr_lower and "api" in stderr_lower:
                            self.log(f"  [{agent_type}] API Error: {stderr[:200]}", "AGENT")

                    if process.returncode not in (0, None):
                        self.log(f"  [{agent_type}] CLI exited with code {process.returncode}", "WARN")
                        if stderr:
                            self.log(f"  [{agent_type}] stderr: {stderr[:300]}", "WARN")
                        # Signal kill (129=SIGHUP, 137=SIGKILL, etc.) — clean up CLI state
                        if process.returncode >= 128:
                            sig = process.returncode - 128
                            self.log(f"  [{agent_type}] Killed by signal {sig}, cleaning up CLI state...", "WARN")
                            self._cleanup_cli_state()

                except subprocess.TimeoutExpired:
                    watchdog.stop()
                    # Kill entire process tree (agent + all its children)
                    self._kill_process_tree(process.pid)
                    timer.stop()
                    self.log(f"Agent {agent_type} timed out ({timeout}s)", "WARN")
                    # Capture whatever stdout/stderr is available.
                    # Close pipes first to avoid blocking on dead processes.
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except Exception:
                        # If communicate still blocks, force-close pipes
                        for pipe in (process.stdout, process.stderr):
                            if pipe:
                                try:
                                    pipe.close()
                                except Exception:
                                    pass
                        stdout, stderr = "", ""
                    stdout = stdout or ""
                    stderr = stderr or ""
                    # JSON envelope is usually missing on timeout (truncated mid-stream).
                    # Try once; on failure fall back to raw text and let empty-run handle it.
                    if self.model == "claude":
                        parsed = _parse_claude_json(stdout)
                        if parsed is not None:
                            result = parsed.get("result", "") or ""
                            usage_record = _extract_usage(parsed)
                        else:
                            result = stdout
                    elif self.model == "gemini":
                        parsed = _parse_gemini_json(stdout)
                        if parsed is not None:
                            result = parsed.get("response", "") or ""
                            usage_record = _extract_gemini_usage(parsed)
                        else:
                            result = stdout
                    else:
                        result = stdout

                watchdog.stop()
                timer.stop()
                elapsed = int(time.time() - start_time)

                # Empty-run detection with auto-retry.
                # "Empty" means the agent didn't do its job — that's a property
                # of the *outcome*, not the *length* or *speed* of the response.
                # A good title is 60 chars; a good yes/no is 3 chars; a good
                # "find this file" is one line. Length is not a quality signal.
                #
                # The only honest signals for "this run was broken":
                #   - process exited non-zero (claude code crashed / errored)
                #   - process produced literally no output
                stripped = result.strip()
                is_empty = (
                    process.returncode != 0
                    or not stripped
                )
                if is_empty:
                    self.log(f"Agent [{agent_type}] empty-run detected (attempt {attempt}/{MAX_RETRIES}): ran only {elapsed}s, output only {len(result.strip())} chars", "WARN")
                    self.log(f"  returncode: {process.returncode}", "WARN")
                    if stderr:
                        self.log(f"  stderr: {stderr[:500]}", "WARN")
                    else:
                        self.log(f"  stderr: (empty)", "WARN")
                    if result.strip():
                        self.log(f"  stdout: {result.strip()[:200]}", "WARN")
                    self.log(f"  prompt length: {len(full_prompt)} chars", "WARN")

                    # Detect quota exhaustion specifically
                    combined_output = (result.strip() + " " + (stderr or "")).lower()
                    if any(phrase in combined_output for phrase in [
                        "out of extra usage", "out of usage",
                        "you've hit your limit", "hit your limit",
                        "usage limit", "resets ",
                    ]):
                        self._quota_exhausted = True
                        self.log(f"API quota exhausted detected — flagging for phase abort", "ERROR")
                        self._agent_empty_count += 1
                        break  # no point retrying, quota won't recover soon

                    # Wall-clock timeout that produced nothing: skip retry. Rerunning
                    # the same prompt for another full timeout window is cargo-cult —
                    # if the first run hung long enough to hit the cap with zero
                    # output, the second almost always does the same. This is the
                    # path that wasted 2 hours on safeclaw-v2 E3.
                    #
                    # (elapsed is computed as int(time.time() - start_time); allow
                    # a small slack below `timeout` to absorb timer jitter.)
                    hit_wall_clock = elapsed >= max(timeout - 5, 0)
                    if hit_wall_clock:
                        self.log(
                            f"  Empty-run coincided with wall-clock timeout ({elapsed}s ≈ {timeout}s) — "
                            f"skipping retry; rerunning the same prompt is unlikely to help",
                            "WARN",
                        )
                        self._agent_empty_count += 1
                        break

                    if attempt < MAX_RETRIES:
                        timer.stop()
                        backoff = 30 * attempt
                        self.log(f"  Empty-run retry: waiting {backoff}s before retry (attempt {attempt + 1}/{MAX_RETRIES})...", "INFO")
                        time.sleep(backoff)
                        start_time = time.time()  # reset for next attempt
                        continue  # retry
                    else:
                        # All retries exhausted
                        self.log(f"  Possible cause: API rate limit / token exhaustion / connection failure", "WARN")
                        self._agent_empty_count += 1
                        if self._agent_empty_count >= 3:
                            self._quota_exhausted = True  # flag for pipeline-level abort
                            wait_time = 300
                            self.log(f"Consecutive {self._agent_empty_count} agent empty-runs, pausing {wait_time}s waiting for API recovery", "ERROR")
                            self.send_notification(
                                "Rate Limit",
                                f"Agent empty {self._agent_empty_count}x in a row, likely rate-limited\nPausing 5min...",
                                priority="critical"
                            )
                            RateLimitCountdown(wait_time).run()
                            self._agent_empty_count = 0
                else:
                    self._agent_empty_count = 0

                break  # success or final attempt done, exit retry loop

            except Exception as e:
                if 'watchdog' in dir():
                    watchdog.stop()
                timer.stop()
                elapsed = int(time.time() - start_time)
                self.log(f"Agent {agent_type} error (attempt {attempt}/{MAX_RETRIES}): {e}", "ERROR")
                if attempt < MAX_RETRIES:
                    self.log(f"  Retrying...", "INFO")
                    time.sleep(30)
                    start_time = time.time()
                    continue
                self.send_notification("Agent Error Failed", f"{agent_type}: {e}", priority="critical")
                err_stat = {
                    "agent_type": agent_type,
                    "elapsed_seconds": elapsed,
                    "prompt_len": 0,
                    "output_len": 0,
                    "timestamp": datetime.now().isoformat(),
                    "error": str(e),
                    # Zero-default cost fields so aggregation never sees missing keys
                    "model": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cost_usd": 0.0,
                    "duration_api_ms": 0,
                }
                self._agent_stats.append(err_stat)
                try:
                    self._write_cost_report()
                except Exception:
                    pass
                return ""

        timer.stop()
        self.log_step(f"{Icons.for_agent(agent_type)} {agent_styled(agent_type, f'[{agent_type}]')} completed ({elapsed}s)", "success")

        # One-line cost summary (only when claude returned parseable usage)
        if usage_record:
            in_tok = usage_record["input_tokens"]
            out_tok = usage_record["output_tokens"]
            cr = usage_record["cache_read_tokens"]
            cc = usage_record["cache_creation_tokens"]
            cached_in = cr + cc
            total_in = in_tok + cached_in
            hit_pct = int(100 * cr / total_in) if total_in else 0
            self.log_step(
                f"  💰 ${usage_record['cost_usd']:.4f}  "
                f"in:{_fmt_tok(in_tok)}  out:{_fmt_tok(out_tok)}  "
                f"cache:{_fmt_tok(cached_in)}({hit_pct}% hit)",
                "info"
            )

        # Agent summary
        summary_items = self._summarize_agent_output(agent_type, result)
        if summary_items:
            self.log_summary_box(f"{agent_type.upper()} Summary", summary_items)

        # Cost tracking — extend with real token/cost when claude JSON was parsed
        stat = {
            "agent_type": agent_type,
            "elapsed_seconds": elapsed,
            "prompt_len": len(full_prompt),
            "output_len": len(result) if result else 0,
            "timestamp": datetime.now().isoformat(),
            # Zero-defaults so cost_report aggregation never sees missing keys
            "model": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cost_usd": 0.0,
            "duration_api_ms": 0,
        }
        if usage_record:
            stat.update(usage_record)
        self._agent_stats.append(stat)

        # Live cost report — written after every agent so the webapp SSE stream
        # can pick up updates within ~2s. Failures here must never break the run.
        try:
            self._write_cost_report()
        except Exception as exc:
            self.log(f"  cost report write failed: {exc}", "WARN")

        return result
