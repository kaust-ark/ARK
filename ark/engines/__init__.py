"""ark.engines: modular package for agent orchestration, runtimes, and parsing."""
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

from .cli import get_cli_for_model


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
    # user_instructions = True wherever the user might steer the work, so HITL
    # steer/adjust messages actually reach the agent doing it (not just the
    # researcher). Reviewer stays False — it evaluates, it shouldn't be steered.
    "researcher":     {"memory": False, "deep_research": False, "prior_context": False, "context_files": True,  "user_instructions": True},
    "reviewer":       {"memory": True,  "deep_research": False, "prior_context": False, "context_files": False, "user_instructions": False},
    "planner":        {"memory": True,  "deep_research": False, "prior_context": True,  "context_files": False, "user_instructions": True},
    "writer":         {"memory": False, "deep_research": True,  "prior_context": True,  "context_files": False, "user_instructions": True},
    "experimenter":   {"memory": False, "deep_research": True,  "prior_context": False, "context_files": True,  "user_instructions": True},
    "coder":          {"memory": False, "deep_research": False, "prior_context": True,  "context_files": False, "user_instructions": True},
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
                summary_lines.append(f"Modified: {self.config.get('latex_dir', 'paper')}/main.tex")
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

        # Credentials guidance — only when the intervention gate is active (the
        # ark-request-secret command exists only then).
        _mgr0 = getattr(self, "_intervention", None)
        if _mgr0 is not None and getattr(_mgr0, "enabled", False):
            full_prompt += (
                "\n\n## CRITICAL RULES — Credentials\n"
                "If you need an API key / token / password you do not already have, do NOT "
                "hardcode, fabricate, or silently skip. Request it from the human:\n"
                "    export NAME=$(ark-request-secret NAME \"why you need it\")\n"
                "The value is supplied over Telegram and injected into your shell env only. "
                "Never `echo` a secret or write it into a file.\n"
            )

        # Experiment sandbox — route agent-generated experiment execution through
        # Apptainer (isolated from the host). No-op unless the base image exists.
        if agent_type in ("experimenter", "coder"):
            try:
                from ark.sandbox import experimenter_directive
                full_prompt += experimenter_directive()
            except Exception:
                pass
            # Shared-env protection — ALWAYS on, sandbox or not. A project's
            # agent once ran `conda activate ark-base && pip install ...`,
            # downgrading sqlalchemy for the whole platform (every user's
            # status/score sync silently died for 5 days).
            full_prompt += (
                "\n\n## CRITICAL RULES — Python environments\n"
                "Install packages ONLY into this project's own environment: "
                "`.conda_env/bin/pip install <pkg>`. NEVER `conda activate` or "
                "install into any shared/named conda env (ark-base, base, or "
                "anything outside this project directory) — those are shared "
                "platform infrastructure and changing them breaks other users' "
                "runs. If `.conda_env` is missing, report it instead of "
                "falling back to a shared env.\n"
            )

        # Brief task description for logging
        task_brief = task.split("\n")[0][:50].strip()
        if len(task.split("\n")[0]) > 50:
            task_brief += "..."
        self.log_step(f"{Icons.for_agent(agent_type)} Agent [{agent_type}] → {task_brief}", "progress")

        start_time = time.time()
        timer = ElapsedTimer(agent_type)

        # 1 initial + 3 retries. OpenRouter endpoints drop streams under load
        # (peer closed / incomplete chunked read) — a single retry proved too
        # thin on flaky evenings (observed 10 drops in one run).
        MAX_RETRIES = 4
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # self.model is the full LiteLLM string (e.g.
                # "gemini/gemini-3.5-flash"), already resolved from --model →
                # config → default. Do NOT pass the legacy config `model_variant`
                # as an override: OpenHandsCLI prefers the variant, which would
                # silently revert a --model override back to the config model.
                self.log(f"Model: {self.model}", "INFO")

                cli_runner = get_cli_for_model(self.model)
                timer.start()

                # Live step log + intervention sandbox. No-op unless the
                # orchestrator attached an InterventionManager as self._intervention.
                _on_event = None
                _exec_env = None
                _mgr = getattr(self, "_intervention", None)
                if _mgr is not None:
                    try:
                        _on_event = _mgr.event_handler(agent_type, self.log_step)
                        _exec_env = _mgr.sandbox_env(
                            cli_runner.build_env(self.code_dir), agent_type)
                    except Exception as e:
                        self.log(f"  [intervention] setup skipped: {e}", "WARN")

                returncode, stdout, stderr, elapsed, timeout_expired = cli_runner.execute(
                    prompt=full_prompt,
                    path_boundary=self._build_path_boundary(),
                    code_dir=self.code_dir,
                    timeout=timeout,
                    log_fn=self.log,
                    on_event=_on_event,
                    env=_exec_env,
                )
                timer.stop()

                if timeout_expired:
                    self.log(f"Agent {agent_type} timed out ({timeout}s)", "WARN")
                
                result = ""
                usage_record = None
                oh_error_code = None
                oh_error_detail = None

                # OpenHands `--json` output: the CLI runner extracts the final agent
                # message, token/cost (from openhands' persisted state), and any
                # ConversationErrorEvent. NOTE: openhands exits 0 even on
                # auth/quota/model failure — failures surface through the event
                # stream, never the return code.
                if hasattr(cli_runner, "parse_output"):
                    parsed = cli_runner.parse_output(stdout)
                    result = parsed.get("result", "") or ""
                    usage_record = parsed.get("usage")
                    oh_error_code = parsed.get("error_code")
                    oh_error_detail = parsed.get("error_detail")
                    # Tool calls the environment REJECTED. Not fatal — the agent
                    # is allowed to fail and recover — but they must be visible,
                    # because the failure mode they precede is silent: the agent
                    # summarises the rejected call as if it had worked, and every
                    # later phase builds on a file that was never written.
                    # Observed end-to-end on a local 32B: write to "/local_ok.txt"
                    # → "Permission denied" → "The file has been created."
                    # Weaker models do this far more, and free/local models are
                    # exactly where we now run. Logged rather than raised: a
                    # rejected call is often a legitimate probe.
                    for line in (parsed.get("failed_tools") or [])[-3:]:
                        self.log(f"  [{agent_type}] tool call rejected — {line}",
                                 "WARN")
                    sandbox_note = parsed.get("sandbox")
                    if sandbox_note:
                        # WARN when the sandbox fell back to the host, INFO when
                        # it held. Either way it is now on the record per call.
                        held = "FAILED" not in sandbox_note.upper()
                        self.log(f"  [{agent_type}] {sandbox_note}",
                                 "INFO" if held else "WARN")
                else:
                    result = stdout

                # Minimal error guard (Q4=A): OpenHands/LiteLLM already retry rate
                # limits per-request; here we only classify terminal vs transient.
                # Terminal (bad key / bad model / permission) -> abort the phase
                # instead of burning retries. Transient -> fall through to the
                # empty-run retry below.
                if oh_error_code:
                    self.log(
                        f"  [{agent_type}] OpenHands error: {oh_error_code} — "
                        f"{(oh_error_detail or '')[:200]}",
                        "WARN",
                    )
                    # OpenHands' LLM layer already retried transient failures
                    # internally (num_retries=5, exponential backoff) before
                    # surfacing this ConversationErrorEvent. So a surfaced error
                    # is effectively final for this invocation. We do NOT re-
                    # classify by matching error strings, and we do NOT busy-wait
                    # for a "quota reset" that only resets on a billing date.
                    #
                    # Default: surface the real error verbatim and abort fast —
                    # the detail IS the correct message (bad key, usage/spend cap,
                    # bad model, context overflow, …). The ONE exception is a
                    # clearly-transient code (rate limit / 5xx / timeout): give it
                    # a single bounded wait-and-retry in case the window reopened.
                    _TRANSIENT = (
                        "RateLimitError", "ServiceUnavailableError", "TimeoutError",
                        "InternalServerError", "APIConnectionError",
                        # 502/504 from the provider's gateway/proxy (Cloudflare etc.,
                        # often returns an HTML error page instead of JSON) — purely
                        # transient, so retry instead of hard-aborting the run.
                        "BadGatewayError", "GatewayTimeoutError",
                        # OpenHands renders agent/tool output through Rich. Tool
                        # output containing Rich-markup-like text with a backslash
                        # (e.g. a grep hit on LaTeX such as "[strat\\...]") makes
                        # Rich's style parser raise MissingStyle / StyleSyntaxError.
                        # That's a cosmetic runtime glitch, NOT a provider error —
                        # the agent's real work usually completed. Retry once
                        # instead of hard-aborting an otherwise-healthy run.
                        "MissingStyle", "StyleSyntaxError",
                    )
                    # Mid-stream network disconnects surface as a generic
                    # APIError whose *code* isn't in _TRANSIENT — but the upstream
                    # simply dropped the stream, so a retry usually completes.
                    # Match these on the error *detail* text. This is the failure
                    # that truncated a writer run into a 0.3-page stub on a
                    # MiniMax/OpenRouter paper (incomplete chunked read).
                    _TRANSIENT_DETAIL = (
                        "incomplete chunked read", "peer closed connection",
                        "connection reset", "connection aborted",
                        "connection broken", "server disconnected",
                        "eof occurred", "remoteprotocolerror",
                        "bad gateway", "gateway timeout",
                        # An upstream provider 5xx relayed by the gateway. The
                        # code is a generic APIError; only the detail names the
                        # real cause ("Upstream error from Nvidia: Internal
                        # server error"). This killed a run that had already
                        # produced a clean 7.6/10 paper — one hiccup at the
                        # review step discarded the whole thing. Any provider
                        # can 5xx, so this is not a free-tier concern.
                        "upstream error", "internal server error",
                        "service unavailable", "temporarily unavailable",
                        "overloaded",
                        # Empty/truncated provider body: litellm fails to parse a
                        # JSON response because OpenRouter (or its upstream) returned
                        # blank/non-JSON bytes mid-request. Same transient class as
                        # "incomplete chunked read" — a bounded retry clears it. This
                        # is the failure that aborted a whole run after an hour of
                        # planning+experiments on a deepseek/OpenRouter paper.
                        "unable to get json response", "expecting value",
                        # Rich style-parser crash on markup-like tool output (see
                        # MissingStyle note above) — match by message text too, in
                        # case the surfaced error_code is generic.
                        "is not a valid color", "failed to get style",
                    )
                    _detail_l = (oh_error_detail or "").lower()
                    _is_transient = (
                        any(oh_error_code.endswith(t) for t in _TRANSIENT)
                        or any(s in _detail_l for s in _TRANSIENT_DETAIL)
                    )
                    if _is_transient and attempt < MAX_RETRIES:
                        # Network/stream disconnects clear immediately — retry
                        # promptly; rate-limit codes honour the server's wait.
                        _net = any(s in _detail_l for s in _TRANSIENT_DETAIL)
                        wait_s = 5 * attempt if _net else min(self._parse_rate_limit_wait(oh_error_detail or ""), 120)
                        self.log(
                            f"  Transient ({oh_error_code}) — waiting {wait_s}s, then "
                            f"retry (attempt {attempt + 1}/{MAX_RETRIES})",
                            "INFO",
                        )
                        if not self._rate_limit_notified:
                            self._rate_limit_notified = True
                            self.send_notification(
                                "Rate Limit",
                                f"{agent_type}: {oh_error_code}, retrying in {wait_s}s",
                                priority="critical",
                            )
                        timer.stop()
                        RateLimitCountdown(wait_s).run()
                        self._rate_limit_notified = False
                        start_time = time.time()
                        continue  # one bounded transient retry
                    # Not transient (or transient retries exhausted): surface the
                    # provider's real error and abort. No guessing, no 30min wait.
                    self._terminal_error = (
                        f"{oh_error_code}: {(oh_error_detail or '')[:300]}"
                    )
                    self.send_notification(
                        "Agent Error",
                        f"{agent_type}: {oh_error_code} — {(oh_error_detail or '')[:160]}",
                        priority="critical",
                    )
                    result = ""
                    break

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
                                f"{agent_type} rate-limited, waiting {wait_minutes:.0f}min\\n"
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

                if returncode not in (0, None):
                    self.log(f"  [{agent_type}] CLI exited with code {returncode}", "WARN")
                    if stderr:
                        self.log(f"  [{agent_type}] stderr: {stderr[:300]}", "WARN")
                    # Signal kill (129=SIGHUP, 137=SIGKILL, etc.) — clean up CLI state
                    if returncode >= 128:
                        sig = returncode - 128
                        self.log(f"  [{agent_type}] Killed by signal {sig}, cleaning up CLI state...", "WARN")
                        self._cleanup_cli_state()

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
                    returncode != 0
                    or not stripped
                )
                if is_empty:
                    self.log(f"Agent [{agent_type}] empty-run detected (attempt {attempt}/{MAX_RETRIES}): ran only {elapsed}s, output only {len(result.strip())} chars", "WARN")
                    self.log(f"  returncode: {returncode}", "WARN")
                    if stderr:
                        self.log(f"  stderr: {stderr[:500]}", "WARN")
                    else:
                        self.log(f"  stderr: (empty)", "WARN")
                    if result.strip():
                        self.log(f"  stdout: {result.strip()[:200]}", "WARN")
                    self.log(f"  prompt length: {len(full_prompt)} chars", "WARN")

                    # NOTE: real quota / rate-limit / auth failures surface as a
                    # ConversationErrorEvent and are handled above (surfaced +
                    # aborted, or one bounded transient retry). A bare empty result
                    # with no error event means a hang or a parse miss — retry once
                    # then give up. We deliberately do NOT string-match "quota" here.

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
                        # Retries exhausted on a no-error empty run. Give up and
                        # let the caller handle the empty result — no busy-wait.
                        # (Transient API errors were already retried by OpenHands
                        # internally and would have surfaced as an error event.)
                        self.log(f"  Empty after {MAX_RETRIES} attempts with no error event — giving up on this agent run", "WARN")
                        self._agent_empty_count += 1
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

        # Spend gate: notify at the soft threshold, ask at the hard cap. Runs on
        # cumulative cost across the whole run. No-op without an InterventionManager.
        # A denial at the hard cap must STOP the run — flag a terminal error so the
        # pipeline aborts at its next checkpoint (same mechanism as other terminal
        # errors); otherwise the "ask at cap" guard would be toothless.
        _mgr = getattr(self, "_intervention", None)
        if _mgr is not None:
            try:
                total = sum(float(s.get("cost_usd") or 0.0) for s in self._agent_stats)
                if not _mgr.check_action("spend", total_usd=total):
                    self._terminal_error = (
                        f"Spend gate: human/policy declined to continue past "
                        f"cumulative cost ${total:.2f}"
                    )
                    self.log(f"  {self._terminal_error} — stopping run", "ERROR")
            except Exception:
                pass

        return result
