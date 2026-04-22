#!/usr/bin/env python3
"""
ARK (Automatic Research Kit) - Automated Research Orchestrator

Usage:
    # Research mode (experiments + analysis)
    python -m ark.orchestrator --project myproject --mode research --max-days 3

    # Paper mode (review + improve iterations)
    python -m ark.orchestrator --project myproject --mode paper --iterations 10
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import yaml
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from typing import Optional, List
import re
import threading
import signal

# ARK package root (where projects/ lives)
ARK_ROOT = Path(__file__).parent.parent.absolute()

# PROJECT_DIR: legacy global, kept for backward compatibility
PROJECT_DIR = None

from ark.memory import get_memory, SimpleMemory
from ark.agents import AgentMixin
from ark.compiler import CompilerMixin
from ark.execution import ExecutionMixin
from ark.pipeline import PipelineMixin
from ark.development import DevMixin


class Orchestrator(AgentMixin, CompilerMixin, ExecutionMixin, PipelineMixin, DevMixin):
    """Main orchestrator class composing all mixins."""

    def __init__(self, project: str, max_days: float = 3, max_iterations: int = 100,
                 mode: str = "research", model: str = None, code_dir: str = None,
                 project_dir: str = None, db_path: str = None, project_id: str = None):
        global PROJECT_DIR

        self.max_end_time = datetime.now() + timedelta(days=max_days)
        self.max_iterations = max_iterations
        self.iteration = 0
        self.mode = mode
        self._model_arg = model  # Store the CLI/constructor argument
        self.project_name = project

        # ── DB awareness ──
        self._db_path = db_path
        self._project_id = project_id
        self._db_sync_errors = 0  # count consecutive failures

        # Human-readable display name (resolved after config loads)
        self._display_name = None

        # Project paths and config
        if project_dir:
            self.project_path = Path(project_dir).absolute()
        else:
            self.project_path = ARK_ROOT / "projects" / self.project_name
        if not self.project_path.exists():
            raise FileNotFoundError(f"Project directory not found: {self.project_path}")

        config_file = self.project_path / "config.yaml"
        if config_file.exists():
            with open(config_file) as f:
                self.config = yaml.safe_load(f) or {}
        else:
            self.config = {}

        # Resolve model: Argument > config.yaml > fallback to "claude"
        self.model = self._model_arg or self.config.get("model") or "claude"

        # Set code_dir and legacy global PROJECT_DIR
        if code_dir:
            PROJECT_DIR = Path(code_dir).absolute()
        else:
            PROJECT_DIR = Path(self.config.get("code_dir", str(ARK_ROOT.parent))).absolute()
        self.code_dir = PROJECT_DIR
        os.chdir(PROJECT_DIR)

        # Load project hooks
        hooks_file = self.project_path / "hooks.py"
        if hooks_file.exists():
            spec = importlib.util.spec_from_file_location("hooks", hooks_file)
            self.hooks = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.hooks)
        else:
            self.hooks = None

        # Paths
        self.state_dir = self.code_dir / "auto_research" / "state"
        self.log_dir = self.code_dir / "auto_research" / "logs"
        self.agents_dir = self.project_path / "agents"

        # Config-driven paths
        self.latex_dir = self.code_dir / self.config.get("latex_dir", "Latex")
        self.figures_dir = self.code_dir / self.config.get("figures_dir", "Latex/figures")

        # State file paths (agent working state — stays as YAML)
        self.state_file = self.state_dir / "research_state.yaml"
        self.findings_file = self.state_dir / "findings.yaml"
        self.paper_state_file = self.state_dir / "paper_state.yaml"
        self.paper_requirements_file = self.state_dir / "paper_requirements.yaml"
        self.checkpoint_file = self.state_dir / "checkpoint.yaml"
        self.action_plan_file = self.state_dir / "action_plan.yaml"
        self.latest_review_file = self.state_dir / "latest_review.md"
        self.literature_file = self.state_dir / "literature.yaml"
        self.dev_state_file = self.state_dir / "dev_state.yaml"

        # Ensure directories exist
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.figures_dir.mkdir(parents=True, exist_ok=True)

        # Project symlinks (skip if project_path IS code_dir)
        from ark.cli import ensure_project_symlinks
        if self.project_path.resolve() != self.code_dir.resolve():
            ensure_project_symlinks(self.project_path, str(self.code_dir))

        # Paper acceptance threshold
        self.paper_accept_threshold = self.config.get("paper_accept_threshold", 8)

        # Dev mode threshold
        self.code_review_threshold = self.config.get("code_review_threshold", 7)

        # Logging setup
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"{self.project_name}_{mode}_{self.run_id}.log"
        self._cleanup_old_logs(keep=5)

        # Token stats
        self.total_input_tokens = 0
        self.total_output_tokens = 0

        # Rate limit state
        self._rate_limit_notified = False

        # Agent empty-run counter (initialized here, not dynamically)
        self._agent_empty_count = 0
        self._quota_exhausted = False
        self._asked_this_iteration = False

        # Cost tracking
        self._agent_stats = []

        # Last successfully compiled PDF (set by compile_latex)
        self._latest_pdf = None

        # Deep research background thread
        self._deep_research_thread = None

        # Memory
        self.memory = get_memory(state_dir=self.state_dir)
        self._last_score = 0.0

        # Goal Anchor
        if hasattr(self.memory, 'set_goal_anchor'):
            self.memory.set_goal_anchor(self.config.get("goal_anchor", ""))

        # Seed language preference from config if not already set
        prefs_file = self.state_dir / "user_prefs.yaml"
        if not prefs_file.exists():
            config_lang = self.config.get("language", "en")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(prefs_file, "w") as _pf:
                yaml.dump({"language": config_lang}, _pf, default_flow_style=False)

        # Compute backend
        from ark.compute import ComputeBackend
        self._compute_backend = ComputeBackend.from_config(
            self.config, self.project_name, self.code_dir, self.log
        )

        # Telegram dispatcher (dedicated per-project bot)
        from ark.telegram import TelegramDispatcher, TelegramConfig
        tg_config = TelegramConfig.from_project_config(self.config)
        self.telegram = TelegramDispatcher(self.project_name, tg_config)

        # Optional Haiku-powered message polishing. Defaults ON when an
        # Anthropic key is available; the project can disable with
        # `telegram_polish: false`. Fail-soft: if the key is missing or the
        # API call errors, the raw message is sent unchanged.
        if self.config.get("telegram_polish", True):
            anthropic_key = (
                self.config.get("anthropic_api_key")
                or self.config.get("anthropic")
                or os.environ.get("ANTHROPIC_API_KEY", "")
            )
            if anthropic_key:
                try:
                    from ark.telegram_ai import polish_message
                    polish_model = self.config.get("telegram_polish_model", "claude-haiku-4-5")
                    self.telegram._polish_fn = (
                        lambda text, ctx, _k=anthropic_key, _m=polish_model:
                        polish_message(text, ctx, api_key=_k, model=_m)
                    )
                except Exception as e:
                    self.log(f"Telegram polish hook setup failed: {e}", "WARN")

        # Telegram conversation history (in-memory, thread-safe)
        self._tg_chat_history: list[dict] = []
        self._tg_chat_lock = threading.Lock()
        self._tg_history_file = self.state_dir / "tg_history.jsonl"

        # Background threads that upload artifacts (PDF, review report) to
        # Telegram after each iteration. Tracked so stop_telegram_listener()
        # can join them on shutdown — otherwise the daemon threads can be
        # killed mid-upload when the orchestrator exits, and the user never
        # receives the final iteration's PDF.
        self._artifact_threads: list[threading.Thread] = []
        self._artifact_threads_lock = threading.Lock()

    @property
    def display_name(self) -> str:
        """Human-readable project name: title from config, else project slug."""
        if self._display_name is None:
            title = self.config.get("title") or ""
            name = self.config.get("name") or ""
            self._display_name = title or name or self.project_name
        return self._display_name

    # ========== DB Sync ==========

    def _sync_db(self, **kwargs):
        """Update project record in the webapp DB. Fail-soft: errors are logged, never raised."""
        if not self._db_path or not self._project_id:
            return
        try:
            import sqlalchemy  # noqa: F401 — availability check
        except ImportError:
            self._db_path = None  # disable future sync attempts silently
            return
        # Ensure ARK root is on sys.path (pipeline chdir's to project dir)
        ark_root = str(Path(__file__).parent.parent.absolute())
        if ark_root not in sys.path:
            sys.path.insert(0, ark_root)
        try:
            from website.dashboard.db import get_session, get_project, update_project
            with get_session(self._db_path) as session:
                project = get_project(session, self._project_id)
                if project:
                    update_project(session, project, **kwargs)
            self._db_sync_errors = 0
        except Exception as e:
            self._db_sync_errors += 1
            if self._db_sync_errors <= 3:
                self.log(f"DB sync failed ({self._db_sync_errors}): {e}", "WARN")

    # ========== Deep Research (background) ==========

    def _start_deep_research_background(self):
        """Start Gemini Deep Research in background thread if needed."""
        deep_research_file = self.state_dir / "deep_research.md"

        if deep_research_file.exists():
            self.log("Deep Research report already exists, skipping.", "INFO")
            return

        if self.config.get("skip_deep_research", False):
            self.log("Deep Research disabled in config.", "INFO")
            return

        from ark.deep_research import run_deep_research_async, get_gemini_api_key
        api_key = get_gemini_api_key()
        if not api_key:
            self.log("No Gemini API key found, skipping Deep Research.", "WARN")
            return

        def _on_complete(report_path):
            self.log(f"Deep Research completed: {report_path}", "INFO")
            self._send_deep_research_telegram(report_path)

        def _on_error(error_msg):
            self.log(f"Deep Research failed: {error_msg}", "WARN")
            if self.telegram.is_configured:
                self.telegram.send(f"Deep Research failed: {error_msg[:200]}")

        self.log("Starting Deep Research in background...", "INFO")
        if self.telegram.is_configured:
            self.telegram.send("Deep Research started in background (5-20 min)...")

        self._deep_research_thread = run_deep_research_async(
            config=self.config,
            output_dir=self.state_dir,
            api_key=api_key,
            on_complete=_on_complete,
            on_error=_on_error,
        )

    def _send_deep_research_telegram(self, report_path: str):
        """Send deep research report as PDF to Telegram (with md fallback)."""
        if not self.telegram.is_configured:
            return
        try:
            self.telegram.send("Deep Research completed!")
            # Convert markdown to PDF for better readability
            pdf_path = self._convert_md_to_pdf(report_path)
            if pdf_path:
                ok = self.telegram.send_document(pdf_path, caption="📄 Deep Research Report (PDF)")
                if ok:
                    return
            # Fallback: send the .md file
            ok = self.telegram.send_document(report_path, caption="📄 Deep Research report (Markdown)")
            if not ok:
                content = Path(report_path).read_text()
                self.telegram.send_raw(content[:4000])
        except Exception as e:
            self.log(f"Failed to send deep research to Telegram: {e}", "WARN")

    def _convert_md_to_pdf(self, md_path: str) -> str:
        """Convert a markdown file to PDF. Returns PDF path or empty string on failure."""
        try:
            import markdown
            from weasyprint import HTML

            md_content = Path(md_path).read_text()
            html_body = markdown.markdown(
                md_content,
                extensions=["tables", "fenced_code", "codehilite", "toc"],
            )

            # Wrap in styled HTML
            html_full = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 11pt;
       line-height: 1.6; max-width: 800px; margin: 40px auto; padding: 0 20px; color: #333; }}
h1 {{ font-size: 20pt; color: #1a1a2e; border-bottom: 2px solid #0d9488; padding-bottom: 8px; }}
h2 {{ font-size: 15pt; color: #1a1a2e; margin-top: 24px; }}
h3 {{ font-size: 12pt; color: #374151; }}
code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-size: 10pt; }}
pre {{ background: #f3f4f6; padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 9pt; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
th, td {{ border: 1px solid #d1d5db; padding: 8px 12px; text-align: left; font-size: 10pt; }}
th {{ background: #f0fdfa; font-weight: 600; }}
blockquote {{ border-left: 4px solid #0d9488; margin: 12px 0; padding: 8px 16px; color: #555; background: #f0fdfa; }}
a {{ color: #0d9488; }}
</style></head><body>{html_body}</body></html>"""

            pdf_path = str(Path(md_path).with_suffix(".pdf"))
            HTML(string=html_full).write_pdf(pdf_path)
            self.log(f"Converted deep research to PDF: {pdf_path}", "INFO")
            return pdf_path
        except ImportError:
            self.log("markdown/weasyprint not installed, skipping PDF conversion", "WARN")
        except Exception as e:
            self.log(f"PDF conversion failed: {e}", "WARN")
        return ""

    # ========== Telegram ==========

    def _tg_history_append(self, role: str, text: str):
        """Append to chat history, keep last 50 in memory. Persist to JSONL."""
        entry = {
            "role": role,
            "text": text[:500],
            "ts": datetime.now().strftime("%H:%M"),
            "date": datetime.now().strftime("%Y-%m-%d"),
        }
        with self._tg_chat_lock:
            self._tg_chat_history.append(entry)
            if len(self._tg_chat_history) > 50:
                self._tg_chat_history = self._tg_chat_history[-50:]
        # Persist outside lock (best-effort)
        try:
            with open(self._tg_history_file, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _tg_history_load(self, max_entries: int = 50):
        """Load last N entries from tg_history.jsonl into _tg_chat_history."""
        if not self._tg_history_file.exists():
            return
        try:
            lines = self._tg_history_file.read_text().splitlines()
            entries = []
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
            with self._tg_chat_lock:
                self._tg_chat_history = entries[-max_entries:]
        except Exception:
            pass

    def _tg_history_format(self) -> str:
        """Format chat history for prompt. Shows last 20 in full; older as compact header."""
        FULL_WINDOW = 20
        with self._tg_chat_lock:
            history = list(self._tg_chat_history)
        lines = []
        if len(history) > FULL_WINDOW:
            older = history[:-FULL_WINDOW]
            dates = sorted({m.get("date", "") for m in older if m.get("date")})
            lines.append(f"[Earlier: {len(older)} more message(s) on {dates[0]}...]")
            history = history[-FULL_WINDOW:]
        for msg in history:
            prefix = "User" if msg["role"] == "user" else "You"
            lines.append(f"[{msg.get('date', '')} {msg['ts']}] {prefix}: {msg['text']}")
        return "\n".join(lines)

    def start_telegram_listener(self):
        """Start the Telegram dispatcher for bidirectional communication."""
        self._tg_history_load(max_entries=50)
        self.telegram.start(on_message=self._handle_telegram_message)
        if self.telegram.is_configured:
            self.log("Telegram dispatcher started", "INFO")

    def stop_telegram_listener(self):
        """Stop the Telegram dispatcher.

        Joins any in-flight artifact-upload threads first so the user
        actually receives the final iteration's PDF and review report
        before the dispatcher is torn down. The wait is bounded so a
        stuck upload can't block process exit indefinitely.
        """
        with self._artifact_threads_lock:
            pending = [t for t in self._artifact_threads if t.is_alive()]
        if pending:
            self.log(
                f"Waiting for {len(pending)} artifact upload(s) to finish...",
                "INFO",
            )
            # 90s per thread is generous for ~5 MB PDFs over Telegram.
            for t in pending:
                t.join(timeout=90)
                if t.is_alive():
                    self.log(
                        f"Artifact thread {t.name} still running after 90s, "
                        f"detaching",
                        "WARN",
                    )
        # Unblock any pending ask_user_decision() before stopping the
        # polling thread, otherwise the orchestrator hangs until the
        # decision timeout expires.
        if self.telegram._is_waiting:
            self.telegram._ask_event.set()
        self.telegram.stop()

    def _get_bot_model(self) -> str:
        """
        Return the model used for Telegram bot replies.

        Prefers a per-project ``bot_model`` from the project config; falls
        back to the default. No global config fallback — ARK is multi-tenant.
        """
        return self.config.get("bot_model") or "claude-sonnet-4-6"

    def _handle_telegram_message(self, text: str):
        """Handle incoming Telegram message via Claude agent."""
        import threading as _threading

        # All messages go through Claude agent (it decides actions like sending PDF)
        _threading.Thread(target=self._agent_respond_telegram, args=(text,), daemon=True).start()

    def _build_tg_system_prompt(self) -> str:
        """Stable identity block: project name/title/venue/goal + language + style + capabilities."""
        lang = self.get_language_pref()
        lang_instruction = "Reply in Chinese." if lang == "zh" else "Reply in English."

        title = self.config.get("title", self.project_name)
        venue = self.config.get("venue", "")
        goal = self.config.get("goal_anchor", "")

        identity = f'You are ARK Bot, the assistant for project "{self.project_name}"'
        if title and title != self.project_name:
            identity += f' ("{title}")'
        if venue:
            identity += f', targeting {venue}'
        identity += ". You are a Telegram chatbot that monitors and manages this research pipeline. You know the project inside out."

        lines = [
            identity,
            lang_instruction,
            "",
            "STYLE (critical):",
            '- Talk like a person, NOT like a report. No section headers, no "**Project**:", no "Current status summary".',
            '- For casual questions ("how\'s it going", "what\'s up"): 2-4 sentences max. Just the key point.',
            "- Only use bullet points if there are 3+ genuinely distinct items. Never nest them.",
            "- **bold** only for the single most important thing in a reply.",
            '- No tables. No headers. No "---" dividers.',
            "- Use standard Markdown: **bold**, *italic*, `code`. Keep it simple.",
            "- Use the conversation history to understand follow-up questions. If the user refers to something from a previous message, use context to answer coherently.",
            "",
            "CAPABILITIES:",
            "- If the user wants the paper PDF, add on a new line at the end: [SEND_PDF]",
            "- Two ways to inject directives into the pipeline (add on a new line at the end):",
            "  [ACTION: ...] — one-time directive for the current/next iteration only (e.g. 'skip experiments this round', 'rerun figure 3')",
            "  [INSTRUCTION: ...] — persistent rule that must be followed in ALL future iterations (e.g. 'always use PyTorch', 'crawl real data from website X', 'use 2 GPUs')",
            "- Choose [ACTION] for temporary/situational requests, [INSTRUCTION] for lasting rules about how to do the research.",
            "- NEVER add either tag for: status queries, simple acknowledgments (ok, proceed, continue, 好的, 继续, 收到), or confirmations of what's already happening.",
        ]

        if goal:
            lines.append(f"\nProject Goal:\n{goal[:400]}")

        return "\n".join(lines)

    def _agent_respond_telegram(self, text: str):
        """Run Claude agent on user message, reply via Telegram."""
        from ark.telegram import TelegramDispatcher

        # Record user message in history
        self._tg_history_append("user", text)

        context = self._gather_telegram_agent_context()
        history = self._tg_history_format()

        # Show typing indicator
        self.telegram.send_typing()

        system_prompt = self._build_tg_system_prompt()
        history_block = history if history else "(no prior conversation)"

        prompt = f"""{system_prompt}

=== Current State ===
{context}

=== Conversation History ===
{history_block}

=== User says ===
{text}"""

        try:
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            result = subprocess.run(
                ["claude", "--print", "--model", self._get_bot_model(), "-p", prompt],
                capture_output=True, text=True, timeout=90, env=env,
            )
            response = result.stdout.strip()
            if not response:
                response = result.stderr.strip()[:500] or "Sorry, unable to respond right now."
        except subprocess.TimeoutExpired:
            response = "Sorry, response timed out."
        except Exception as e:
            response = f"Error: {e}"

        # Extract [SEND_PDF] if present
        send_pdf = False
        if "[SEND_PDF]" in response:
            send_pdf = True
            response = response.replace("[SEND_PDF]", "").strip()

        # Extract [ACTION: ...] (one-time) and [INSTRUCTION: ...] (persistent)
        action = None
        instruction = None
        if "[ACTION:" in response:
            try:
                action_start = response.index("[ACTION:") + 8
                action_end = response.index("]", action_start)
                action = response[action_start:action_end].strip()
                response = response[:response.index("[ACTION:")].strip()
            except ValueError:
                pass
        if "[INSTRUCTION:" in response:
            try:
                instr_start = response.index("[INSTRUCTION:") + 13
                instr_end = response.index("]", instr_start)
                instruction = response[instr_start:instr_end].strip()
                response = response[:response.index("[INSTRUCTION:")].strip()
            except ValueError:
                pass

        # Record bot response in history
        if response:
            self._tg_history_append("bot", response)

        # Send response
        if response:
            self.telegram.send_raw(
                TelegramDispatcher.to_html(response), parse_mode="HTML"
            )

        if send_pdf:
            self._send_pdf_via_telegram()

        if action:
            self.inject_user_update(action)
            self.telegram.send_raw(f"✅ Action queued: {action[:100]}")

        if instruction:
            self.add_user_instruction(instruction, source="telegram")
            self.inject_user_update(instruction)  # also apply immediately
            self.telegram.send_raw(f"✅ Instruction saved (persistent): {instruction[:100]}")

        tag_info = ""
        if action:
            tag_info += f" + ACTION: {action[:50]}"
        if instruction:
            tag_info += f" + INSTRUCTION: {instruction[:50]}"
        self.log(f"Telegram agent responded ({len(response)} chars){tag_info}", "INFO")

    def _gather_telegram_agent_context(self) -> str:
        """Collect project state for the Telegram agent's system prompt."""
        lines = []
        lines.append(f"Project: {self.project_name}")
        lines.append(f"Mode: {self.mode} | Iteration: {self.iteration}/{self.max_iterations}")

        try:
            score = getattr(self, '_last_score', 0)
            lines.append(f"Current score: {score}/10 (target: {self.paper_accept_threshold}/10)")
            if hasattr(self.memory, 'stagnation_count'):
                lines.append(f"Stagnation count: {self.memory.stagnation_count}")
            recent_scores = getattr(self.memory, 'scores', [])[-8:]
            if recent_scores:
                lines.append(f"Score history: {[f'{s:.1f}' for s in recent_scores]}")
        except Exception:
            pass

        goal = self.config.get("goal_anchor", "")
        if goal:
            lines.append(f"\nGoal Anchor:\n{goal[:600]}")

        persistent_instructions = self.load_user_instructions()
        if persistent_instructions:
            lines.append(f"\nUser Instructions (MUST follow):\n{persistent_instructions}")

        review_file = self.state_dir / "latest_review.md"
        if review_file.exists():
            lines.append(f"\nLatest Review (excerpt):\n{review_file.read_text()[:800]}")

        plan_file = self.state_dir / "action_plan.yaml"
        if plan_file.exists():
            lines.append(f"\nCurrent Action Plan:\n{plan_file.read_text()[:400]}")

        try:
            log_lines = [l for l in self.log_file.read_text().splitlines() if l.strip()][-20:]
            lines.append(f"\nRecent Log:\n" + "\n".join(log_lines))
        except Exception:
            pass

        return "\n".join(lines)

    # ========== Language Preference ==========

    def get_language_pref(self) -> str:
        """Return 'en' or 'zh'. Defaults to 'en'."""
        prefs_file = self.state_dir / "user_prefs.yaml"
        try:
            if prefs_file.exists():
                with open(prefs_file) as f:
                    return yaml.safe_load(f).get("language", "en")
        except Exception:
            pass
        return "en"

    def set_language_pref(self, lang: str):
        """Persist language preference ('en' or 'zh')."""
        prefs_file = self.state_dir / "user_prefs.yaml"
        try:
            data = {}
            if prefs_file.exists():
                with open(prefs_file) as f:
                    data = yaml.safe_load(f) or {}
            data["language"] = lang
            with open(prefs_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False)
            self._sync_db(language=lang)
            self.log(f"Language preference set to: {lang}", "INFO")
        except Exception as e:
            self.log(f"Failed to save language pref: {e}", "WARN")

    # ========== Iteration Summary ==========

    def _send_pdf_via_telegram(self):
        """Send the last compiled PDF via Telegram.

        Uses self._latest_pdf (set by compile_latex) as the single source
        of truth — no path guessing, no post-hoc validation needed.
        """
        pdf = getattr(self, '_latest_pdf', None)
        if pdf is None or not pdf.exists():
            self.telegram.send_raw("No compiled PDF available yet.")
            return

        caption = f"📄 {self.display_name} — iter {self.iteration}, score {self._last_score:.1f}/10"
        ok = self.telegram.send_document(pdf, caption=caption)
        if ok:
            self.log(f"PDF sent via Telegram: {pdf} ({pdf.stat().st_size} bytes)", "INFO")
        else:
            self.log(f"PDF upload failed: {pdf}", "WARN")

    def _render_review_to_pdf(self, md_text: str, out_path: "Path") -> bool:
        """Best-effort markdown → PDF conversion. Returns True on success.

        Tries pandoc first (if available), then python-markdown + weasyprint
        (both shipped in the ark conda env). Any failure returns False so the
        caller can fall back to sending the raw .md file.
        """
        from pathlib import Path
        import shutil
        import subprocess

        out_path = Path(out_path)

        # 1. pandoc (most universal). Skip if missing.
        if shutil.which("pandoc"):
            try:
                proc = subprocess.run(
                    ["pandoc", "-f", "markdown", "-o", str(out_path),
                     "--pdf-engine=xelatex", "-V", "geometry:margin=1in"],
                    input=md_text, text=True,
                    capture_output=True, timeout=60,
                )
                if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size >= 1024:
                    return True
            except Exception:
                pass

        # 2. python-markdown + weasyprint
        try:
            import markdown as _md
            from weasyprint import HTML
            html_body = _md.markdown(
                md_text,
                extensions=["fenced_code", "tables", "toc", "sane_lists"],
            )
            css = (
                "body { font-family: sans-serif; max-width: 760px; "
                "margin: 1em auto; padding: 0 1em; line-height: 1.5; "
                "font-size: 11pt; color: #222; }"
                "h1, h2, h3 { color: #111; }"
                "code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }"
                "pre { background: #f4f4f4; padding: 0.6em; border-radius: 4px; "
                "overflow-x: auto; font-size: 9pt; }"
                "table { border-collapse: collapse; }"
                "th, td { border: 1px solid #bbb; padding: 4px 8px; }"
                "blockquote { border-left: 3px solid #ccc; margin: 0; padding: 0 1em; color: #555; }"
                "hr { border: none; border-top: 1px solid #ccc; margin: 1em 0; }"
            )
            full_html = (
                f"<html><head><meta charset='utf-8'>"
                f"<style>{css}</style></head><body>{html_body}</body></html>"
            )
            HTML(string=full_html).write_pdf(str(out_path))
            if out_path.exists() and out_path.stat().st_size >= 1024:
                return True
        except Exception as e:
            self.log(f"weasyprint review→PDF failed: {e}", "WARN")

        return False

    def _send_review_report_via_telegram(self, score: float = None):
        """Send the latest reviewer report (latest_review.md) via Telegram.

        Tries to render to PDF first; falls back to sending the .md file as
        a text document. Called from send_iteration_summary() right after the
        paper PDF so the user receives both side-by-side.
        """
        if not self.telegram.is_configured:
            return

        review_md = self.state_dir / "latest_review.md"
        if not review_md.exists():
            return

        md_text = ""
        try:
            md_text = review_md.read_text()
        except Exception:
            return
        if not md_text.strip():
            return

        score_str = f"{score:.1f}/10" if score is not None else ""
        caption = (
            f"📝 Review report — {self.display_name} "
            f"iter {self.iteration}{(' · ' + score_str) if score_str else ''}"
        )

        # Try PDF rendering first
        pdf_path = self.state_dir / f"latest_review_iter{self.iteration}.pdf"
        try:
            ok = self._render_review_to_pdf(md_text, pdf_path)
        except Exception as e:
            self.log(f"Review report PDF render error: {e}", "WARN")
            ok = False

        if ok:
            try:
                sent = self.telegram.send_document(pdf_path, caption=caption)
                if sent:
                    self.log(f"Review report PDF sent: {pdf_path}", "INFO")
                    return
                self.log("Review PDF upload failed, falling back to .md", "WARN")
            except Exception as e:
                self.log(f"Review PDF send raised: {e}", "WARN")

        # Fallback: send the .md file directly
        try:
            sent = self.telegram.send_document(
                review_md, caption=caption,
                require_pdf=False, min_size=64,
            )
            if sent:
                self.log(f"Review report .md sent: {review_md}", "INFO")
            else:
                self.log("Review report .md upload failed", "WARN")
        except Exception as e:
            self.log(f"Review .md send raised: {e}", "WARN")

    def send_iteration_summary(self, score: float, prev_score: float, review_text: str = ""):
        """Send compact iteration summary + PDF to Telegram."""
        if not self.telegram.is_configured:
            return

        gap = self.paper_accept_threshold - score

        # Score line
        if prev_score == 0 and self.iteration == 1:
            score_line = f"First review: <b>{score:.1f}/10</b>"
        else:
            trend = score - prev_score
            trend_str = f"+{trend:.1f}" if trend > 0 else f"{trend:.1f}" if trend < 0 else "±0"
            trend_emoji = "📈" if trend > 0 else "📉" if trend < 0 else "➡️"
            score_line = f"{trend_emoji} {prev_score:.1f} → <b>{score:.1f}/10</b> ({trend_str})"

        gap_line = "🎉 Target reached!" if gap <= 0 else f"Gap: {gap:.1f}"

        # Major/minor issue counts from review
        review_src = review_text
        if not review_src and (self.state_dir / "latest_review.md").exists():
            review_src = (self.state_dir / "latest_review.md").read_text()

        issue_summary = ""
        if review_src:
            major_issues = self._extract_issue_summaries(review_src, "major") if hasattr(self, '_extract_issue_summaries') else []
            minor_issues = self._extract_issue_summaries(review_src, "minor") if hasattr(self, '_extract_issue_summaries') else []
            parts = []
            if major_issues:
                parts.append(f"Major: {len(major_issues)}")
            if minor_issues:
                parts.append(f"Minor: {len(minor_issues)}")
            if parts:
                issue_summary = " | ".join(parts)

        # Build compact message
        lines = [
            f"<b>{self.display_name}</b>",
            f"━━━ #{self.iteration}  {score_line} ━━━",
            f"Target: {self.paper_accept_threshold}/10 | {gap_line}",
        ]
        if issue_summary:
            lines.append(issue_summary)

        self.telegram.send_async(
            "\n".join(lines),
            parse_mode="HTML",
            polish=True,
            polish_ctx=self._polish_ctx("iteration_summary"),
        )

        # Send the paper PDF and the review report in the background so the
        # orchestrator doesn't block on the (slow) multipart uploads or on
        # the markdown→PDF render. The thread is tracked so that
        # stop_telegram_listener() can join it on shutdown — without that,
        # the daemon thread can be killed mid-upload when the orchestrator
        # exits and the user never receives the final iteration's PDF.
        def _send_artifacts_bg(_score):
            try:
                self._send_pdf_via_telegram()
            except Exception as e:
                self.log(f"Paper PDF send failed: {e}", "WARN")
            try:
                self._send_review_report_via_telegram(score=_score)
            except Exception as e:
                self.log(f"Review report send failed: {e}", "WARN")

        t = threading.Thread(
            target=_send_artifacts_bg, args=(score,), daemon=True,
            name=f"artifact-send-iter{self.iteration}",
        )
        with self._artifact_threads_lock:
            # Drop already-finished threads so the list doesn't grow forever.
            self._artifact_threads = [
                x for x in self._artifact_threads if x.is_alive()
            ]
            self._artifact_threads.append(t)
        t.start()

    # ========== User Updates ==========

    # ========== Persistent User Instructions ==========

    def load_user_instructions(self) -> str:
        """Load all persistent user instructions (never consumed, always active)."""
        instructions_file = self.state_dir / "user_instructions.yaml"
        if not instructions_file.exists():
            return ""
        try:
            with open(instructions_file) as f:
                data = yaml.safe_load(f) or {}
            entries = data.get("instructions", [])
            if not entries:
                return ""
            messages = [e.get("message", "") for e in entries if e.get("message")]
            if not messages:
                return ""
            return "\n".join(f"- {m}" for m in messages)
        except Exception as e:
            self.log(f"Error reading user instructions: {e}", "WARN")
            return ""

    def add_user_instruction(self, message: str, source: str = "telegram"):
        """Append a persistent instruction that agents must conform to every iteration."""
        instructions_file = self.state_dir / "user_instructions.yaml"
        try:
            data = {}
            if instructions_file.exists():
                with open(instructions_file) as f:
                    data = yaml.safe_load(f) or {}
            entries = data.get("instructions", [])
            entries.append({
                "message": message,
                "source": source,
                "timestamp": datetime.now().isoformat(),
            })
            data["instructions"] = entries
            with open(instructions_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
            self.log(f"Persistent instruction added ({source}): {message[:80]}", "INFO")
        except Exception as e:
            self.log(f"Failed to add user instruction: {e}", "WARN")

    def check_user_updates(self) -> str:
        """Check for user updates from 'ark update' and consume them."""
        updates_file = self.state_dir / "user_updates.yaml"
        if not updates_file.exists():
            return ""

        try:
            with open(updates_file) as f:
                data = yaml.safe_load(f) or {}
            updates = data.get("updates", [])
            pending = [u for u in updates if not u.get("consumed")]
            if not pending:
                return ""

            messages = []
            for u in pending:
                messages.append(u.get("message", ""))
                u["consumed"] = True

            with open(updates_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

            combined = "\n".join(messages)
            self.log(f"User updates received ({len(messages)} messages)", "INFO")
            return combined
        except Exception as e:
            self.log(f"Error reading user updates: {e}", "WARN")
            return ""

    # ========== Checkpoint ==========

    def save_checkpoint(self):
        """Save run state checkpoint (clears phase progress — iteration complete)."""
        checkpoint = {
            "run_id": self.run_id,
            "iteration": self.iteration,
            "mode": self.mode,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "timestamp": datetime.now().isoformat(),
            "completed_phase": 0,  # Reset — full iteration done
        }
        with open(self.checkpoint_file, "w") as f:
            yaml.dump(checkpoint, f, default_flow_style=False)
        self.log(f"Checkpoint saved: iteration={self.iteration}", "INFO")
        self._sync_db(
            checkpoint_data=json.dumps(checkpoint),
            iteration=self.iteration,
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
        )

    def save_step_checkpoint(self, step_num: int, step_name: str):
        """Save checkpoint after a step completes within a phase iteration."""
        checkpoint = {
            "run_id": self.run_id,
            "iteration": self.iteration,
            "mode": self.mode,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "timestamp": datetime.now().isoformat(),
            "completed_step": step_num,
            "completed_step_name": step_name,
            # Backward compat keys
            "completed_phase": step_num,
            "completed_phase_name": step_name,
        }
        with open(self.checkpoint_file, "w") as f:
            yaml.dump(checkpoint, f, default_flow_style=False)
        self._sync_db(checkpoint_data=json.dumps(checkpoint))

    # Backward compat alias
    save_phase_checkpoint = save_step_checkpoint

    def get_resume_step(self) -> int:
        """Get the last completed step for the current iteration. Returns 0 if none."""
        checkpoint = self.load_checkpoint()
        if checkpoint.get("iteration") == self.iteration:
            return checkpoint.get("completed_step", checkpoint.get("completed_phase", 0))
        return 0

    # Backward compat alias
    get_resume_phase = get_resume_step

    def load_checkpoint(self) -> dict:
        """Load checkpoint."""
        if self.checkpoint_file.exists():
            with open(self.checkpoint_file) as f:
                return yaml.safe_load(f) or {}
        return {}

    def resume_from_checkpoint(self):
        """Resume from checkpoint."""
        checkpoint = self.load_checkpoint()
        if checkpoint:
            self.iteration = checkpoint.get("iteration", 0)
            self.total_input_tokens = checkpoint.get("total_input_tokens", 0)
            self.total_output_tokens = checkpoint.get("total_output_tokens", 0)
            self.log(f"Resumed from checkpoint: iteration={self.iteration}", "INFO")
            return True

        try:
            if self.mode == "paper" and self.paper_state_file.exists():
                state = self.load_paper_state()
                reviews = state.get("reviews", [])
                if reviews:
                    last_iter = max((r.get("iteration", 0) for r in reviews), default=0)
                    if last_iter > 0:
                        self.iteration = last_iter
                        self.log(f"Resumed from paper_state: iteration={self.iteration}", "INFO")
                        return True
            elif self.mode == "research" and self.state_file.exists():
                with open(self.state_file) as f:
                    state = yaml.safe_load(f) or {}
                last_iter = state.get("current_iteration", {}).get("number", 0)
                if last_iter > 0:
                    self.iteration = last_iter
                    self.log(f"Resumed from research_state: iteration={self.iteration}", "INFO")
                    return True
        except Exception as e:
            self.log(f"Error resuming from state files: {e}", "WARN")

        return False

    # ========== Logging ==========

    def _cleanup_old_logs(self, keep: int = 5):
        """Clean up old log files, keep the most recent N."""
        project_logs = sorted(self.log_dir.glob(f"{self.project_name}_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        for old_log in project_logs[keep:]:
            old_log.unlink()

        for pattern in ["agent_*.log", "orchestrator_*.log"]:
            old_logs = sorted(self.log_dir.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
            for old_log in old_logs[keep:]:
                old_log.unlink()

    def cleanup_workspace(self):
        """Clean up workspace (LaTeX temp files, old logs, etc.)."""
        self.log("Cleaning up workspace...", "INFO")
        cleaned = 0

        latex_temp_exts = [".aux", ".log", ".out", ".toc", ".bbl", ".blg", ".fls", ".fdb_latexmk", ".synctex.gz"]
        for ext in latex_temp_exts:
            for f in self.latex_dir.glob(f"*{ext}"):
                try:
                    f.unlink()
                    cleaned += 1
                except Exception:
                    pass

        page_images = sorted(self.latex_dir.glob("page_*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
        for img in page_images[10:]:
            try:
                img.unlink()
                cleaned += 1
            except Exception:
                pass

        for cache_dir in self.code_dir.rglob("__pycache__"):
            if cache_dir.is_dir():
                try:
                    import shutil
                    shutil.rmtree(cache_dir)
                    cleaned += 1
                except Exception:
                    pass

        self.log(f"Cleanup done: deleted {cleaned} temp files", "INFO")

    def log(self, message: str, level: str = "INFO"):
        """Log a message with timestamp. ANSI codes stripped for file output."""
        from ark.ui import strip_ansi

        timestamp = datetime.now().strftime("%H:%M:%S")

        if level == "RAW":
            log_message = message
        else:
            log_message = f"[{timestamp}] {message}"

        print(log_message, flush=True)
        with open(self.log_file, "a") as f:
            f.write(strip_ansi(log_message) + "\n")
            f.flush()

    def log_section(self, title: str, char: str = "═"):
        """Print major section header."""
        from ark.ui import styled, Style
        line = char * 70
        self.log(styled(line, Style.DIM), "RAW")
        self.log(styled(f"  {title}", Style.BOLD), "RAW")
        self.log(styled(line, Style.DIM), "RAW")

    def log_step_header(self, step_num: int, total_steps: int, name: str, status: str = "start"):
        """Print step header within a phase iteration (e.g., Step 1/5: Compile LaTeX)."""
        from ark.ui import styled, Style, Icons
        timestamp = datetime.now().strftime("%H:%M:%S")
        step_icon = Icons.for_step_header(name)
        if status == "skipped":
            self.log(f"[{timestamp}] {styled(f'⏭ Step {step_num}/{total_steps}: {name} (resumed, skipping)', Style.DIM)}", "RAW")
        elif status == "start":
            self.log("", "RAW")
            header = f"┌─ {step_icon} STEP {step_num}/{total_steps}: {name} " + "─" * max(0, 48 - len(name))
            self.log(styled(header, Style.BOLD, Style.CYAN), "RAW")
            self.log(f"│ [{timestamp}] Starting...", "RAW")
        else:
            self.log(f"│ [{timestamp}] {styled('✓ Completed', Style.GREEN)}", "RAW")
            self.log(styled("└" + "─" * 69, Style.DIM), "RAW")

    # Backward compat alias
    log_phase = log_step_header

    def log_step(self, message: str, status: str = "info"):
        """Print step detail within a step header block."""
        from ark.ui import styled, Style, Icons
        timestamp = datetime.now().strftime("%H:%M:%S")
        icon = Icons.for_step(status)
        color_map = {
            "success": Style.GREEN,
            "warning": Style.YELLOW,
            "error": Style.RED,
            "progress": Style.CYAN,
        }
        color = color_map.get(status, "")
        if color:
            self.log(f"│ [{timestamp}] {styled(f'{icon} {message}', color)}", "RAW")
        else:
            self.log(f"│ [{timestamp}] {icon} {message}", "RAW")

    def log_summary_box(self, title: str, items: list, inside_phase: bool = True):
        """Print a summary box."""
        from ark.ui import styled, Style
        prefix = "│   " if inside_phase else ""
        if inside_phase:
            self.log("│", "RAW")
        self.log(f"{prefix}┌─ {styled(title, Style.BOLD)} " + "─" * max(0, 50 - len(title)) + "┐", "RAW")
        for item in items:
            lines = item.split("\n") if "\n" in item else [item]
            for line in lines:
                if len(line) > 52:
                    line = line[:49] + "..."
                self.log(f"{prefix}│ {line:<52} │", "RAW")
        self.log(f"{prefix}└" + "─" * 54 + "┘", "RAW")

    # ========== State I/O ==========

    def load_state(self) -> dict:
        """Load research state."""
        if not self.state_file.exists():
            self.log("Initializing new research state...", "INFO")
            default_state = {
                "phases": {
                    "C1_quantify": {"status": "pending"},
                    "C2_probe": {"status": "pending", "sub_tasks": []},
                    "C3_formulate": {"status": "pending"},
                    "C4_solver": {"status": "pending"},
                    "C5_validation": {"status": "pending"},
                },
                "current_iteration": {"number": 0},
                "history": []
            }
            self.save_state(default_state)
            return default_state

        with open(self.state_file) as f:
            return yaml.safe_load(f) or {}

    def save_state(self, state: dict):
        """Save research state."""
        with open(self.state_file, "w") as f:
            yaml.dump(state, f, default_flow_style=False, allow_unicode=True)

    def get_current_phase(self, state: dict) -> str:
        """Get current research phase."""
        phases = state.get("phases", {})
        for phase_name in ["C1_quantify", "C2_probe", "C3_formulate", "C4_solver", "C5_validation"]:
            phase = phases.get(phase_name, {})
            if phase.get("status") in ["pending", "in_progress"]:
                return phase_name
        return "completed"

    # ========== Memory ==========

    def record_score_to_memory(self, score: float):
        """Record score to Memory."""
        self.memory.record_score(score)
        self._last_score = score
        self.log(f"Memory: recorded score {score}/10", "MEMORY")

    def get_memory_context(self) -> str:
        """Get Memory context."""
        return self.memory.get_context()

    # ========== Paper State ==========

    def load_paper_state(self) -> dict:
        """Load paper review state."""
        if self.paper_state_file.exists():
            with open(self.paper_state_file) as f:
                return yaml.safe_load(f) or {}
        return {
            "reviews": [],
            "current_score": 0,
            "status": "in_progress",
        }

    def save_paper_state(self, state: dict):
        """Save paper review state."""
        with open(self.paper_state_file, "w") as f:
            yaml.dump(state, f, default_flow_style=False, allow_unicode=True)
        # Sync to DB
        db_update = {
            "score": float(state.get("current_score", 0)),
            "iteration": self.iteration,
        }
        reviews = state.get("reviews", [])
        if reviews:
            db_update["score_history"] = json.dumps([
                {"iteration": r.get("iteration", i + 1),
                 "score": float(r.get("score", 0)),
                 "timestamp": r.get("timestamp", "")}
                for i, r in enumerate(reviews)
            ])
        paper_status = state.get("status", "in_progress")
        if paper_status in ("accepted", "accepted_pending_cleanup"):
            db_update["phase"] = "accepted"
        else:
            db_update["phase"] = "review"
        self._sync_db(**db_update)

    def load_paper_requirements(self) -> dict:
        """Load paper requirements config."""
        if self.paper_requirements_file.exists():
            with open(self.paper_requirements_file) as f:
                return yaml.safe_load(f) or {}
        return {}

    def _paper_has_substantial_content(self) -> bool:
        """Check if main.tex has substantial content."""
        main_tex = self.latex_dir / "main.tex"
        if not main_tex.exists():
            return False

        try:
            content = main_tex.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return False

        if len(content.strip()) < 2000:
            return False

        section_count = len(re.findall(r"\\section\{", content))
        if section_count < 2:
            return False

        abstract_match = re.search(
            r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
            content,
            re.DOTALL,
        )
        if not abstract_match:
            return False

        abstract_text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})?", " ", abstract_match.group(1))
        abstract_text = re.sub(r"\s+", " ", abstract_text).strip()
        return len(abstract_text) >= 200

    def _should_run_paper_initialize(self, paper_state: dict) -> bool:
        """Whether to run first-run initialization."""
        if self.iteration != 1:
            return False
        if paper_state.get("reviews"):
            return False
        if self._paper_has_substantial_content():
            self.log("Detected existing substantial main.tex content; skip first-run initialization.", "INFO")
            return False
        return True

    # ========== Action Plan ==========

    def _load_action_plan(self) -> dict:
        """Load Planner-generated action plan with error recovery for LaTeX escapes."""
        if self.action_plan_file.exists():
            try:
                with open(self.action_plan_file) as f:
                    return yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                self.log(f"YAML parse error, attempting to fix LaTeX escape: {e}", "WARN")
                try:
                    raw = self.action_plan_file.read_text()  # Fixed: was ACTION_PLAN_FILE
                    def fix_dquoted(match):
                        content = match.group(1)
                        if '\\' in content:
                            content = content.replace("'", "''")
                            return "'" + content + "'"
                        return match.group(0)

                    fixed = re.sub(r'"([^"\n]*)"', fix_dquoted, raw)
                    self.action_plan_file.write_text(fixed)
                    result = yaml.safe_load(fixed) or {}
                    self.log("YAML fix succeeded (LaTeX escape -> single quotes)", "INFO")
                    return result
                except Exception as e2:
                    self.log(f"YAML fix failed: {e2}", "ERROR")
                    raise RuntimeError(
                        f"Cannot parse action plan {self.action_plan_file}: {e2}"
                    ) from e2
        return {"issues": []}

    def _save_action_plan(self, action_plan: dict):
        """Save action plan."""
        with open(self.action_plan_file, "w") as f:
            yaml.dump(action_plan, f, default_flow_style=False, allow_unicode=True)

    def _load_findings_summary(self) -> str:
        """Load findings.yaml summary."""
        if self.findings_file.exists():
            with open(self.findings_file) as f:
                findings = yaml.safe_load(f) or {}
            return yaml.dump(findings, allow_unicode=True)[:500]
        return "No findings yet"

    # ========== Review Parsing ==========

    def parse_review_score(self, review_output: str) -> float:
        """Parse overall score from review output."""
        patterns = [
            r"总体评分[：:]\s*(\d+\.?\d*)/10",
            r"Overall Score[：:]\s*(\d+\.?\d*)/10",
            r"总分[：:]\s*(\d+\.?\d*)/10",
            r"\*\*Total\*\*.*?\*\*(\d+\.?\d*)/10\*\*",
            r"\|\s*Total\s*\|.*?(\d+\.?\d*)/10",
        ]
        for pattern in patterns:
            match = re.search(pattern, review_output, re.IGNORECASE | re.DOTALL)
            if match:
                score = float(match.group(1))
                self.log(f"Parsed score: {score}/10")
                return score

        if self.latest_review_file.exists():
            content = self.latest_review_file.read_text()
            for pattern in patterns:
                match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
                if match:
                    score = float(match.group(1))
                    self.log(f"Parsed score from latest_review.md: {score}/10")
                    return score

        self.log("Warning: could not parse score, returning 0")
        return 0.0

    def extract_issue_ids(self) -> list:
        """Extract issue IDs and titles from latest_review.md.

        Returns a list of dicts: [{"id": "M1", "title": "Short descriptive title"}, ...]
        The title is used for content-based issue tracking across iterations.
        Falls back to ID-only if titles cannot be parsed.
        """
        if not self.latest_review_file.exists():
            return []

        content = self.latest_review_file.read_text()

        # Try to extract structured issues: ### M1. Title or ### M1. [TAG] Title
        structured_pattern = r'###\s+([Mm]\d+)\.\s*(?:\[.*?\]\s*)?(.+)'
        structured_matches = re.findall(structured_pattern, content)

        if structured_matches:
            issues = []
            seen = set()
            for issue_id, title in structured_matches:
                if issue_id not in seen:
                    seen.add(issue_id)
                    issues.append({"id": issue_id, "title": title.strip()})
            self.log(f"Extracted {len(issues)} issues: {[(i['id'], i['title'][:40]) for i in issues]}")
            return issues

        # Fallback: extract just IDs (legacy format)
        issue_pattern = r'\b([Mm]\d+)\b'
        matches = re.findall(issue_pattern, content)
        unique_ids = list(set(matches))
        issues = [{"id": i, "title": ""} for i in unique_ids]
        self.log(f"Extracted {len(issues)} issues (ID-only fallback): {unique_ids}")
        return issues

    def _check_needs_experiment(self, review_output: str) -> bool:
        """Analyze review to determine if experiments are needed."""
        content = review_output
        if self.latest_review_file.exists():
            content += "\n" + self.latest_review_file.read_text()

        experiment_keywords = [
            r"需要.*实验", r"补充.*实验", r"缺少.*数据", r"验证不足",
            r"建议.*增加.*实验", r"add.*experiment", r"missing.*data",
            r"insufficient.*validation", r"suggest.*adding.*experiment",
            r"need.*more.*evidence", r"require.*additional.*test",
        ]

        for pattern in experiment_keywords:
            if re.search(pattern, content, re.IGNORECASE):
                self.log(f"Detected experiment-needed keyword: {pattern}", "INFO")
                return True
        return False

    def _check_needs_literature_search(self, review_output: str) -> tuple:
        """Check if literature search is needed."""
        content = review_output
        if self.latest_review_file.exists():
            content += "\n" + self.latest_review_file.read_text()

        search_topics = []

        related_work_keywords = [
            r"related work.*insufficient", r"related work.*missing",
            r"缺少.*相关工作", r"should cite", r"compare with.*other",
            r"missing.*comparison", r"prior work", r"existing.*method",
        ]
        for pattern in related_work_keywords:
            if re.search(pattern, content, re.IGNORECASE):
                search_topics.append("related_work")
                break

        tech_keywords = [
            r"verify.*claim", r"documentation.*support",
            r"FlashAttention.*behavior", r"Tensor Core.*requirement",
            r"技术.*验证",
        ]
        for pattern in tech_keywords:
            if re.search(pattern, content, re.IGNORECASE):
                search_topics.append("technical_verification")
                break

        comparison_keywords = [
            r"compare.*baseline", r"other.*compression",
            r"alternative.*method", r"state.of.the.art", r"SOTA",
        ]
        for pattern in comparison_keywords:
            if re.search(pattern, content, re.IGNORECASE):
                search_topics.append("competitive_analysis")
                break

        return len(search_topics) > 0, search_topics

    # ========== Validation ==========

    def _validate_action_plan(self, plan: dict) -> tuple:
        """Validate action plan has required structure.

        Returns:
            (is_valid: bool, error_message: str)
        """
        if not isinstance(plan, dict):
            return False, "Plan is not a dictionary"

        issues = plan.get("issues")
        if issues is None:
            return False, "Missing 'issues' key"

        if not isinstance(issues, list):
            return False, "'issues' is not a list"

        for i, issue in enumerate(issues):
            if not isinstance(issue, dict):
                return False, f"Issue {i} is not a dictionary"
            if not issue.get("id"):
                return False, f"Issue {i} missing 'id'"
            if not issue.get("type"):
                return False, f"Issue {i} (id={issue.get('id')}) missing 'type'"
            if not issue.get("title"):
                return False, f"Issue {i} (id={issue.get('id')}) missing 'title'"

        return True, ""

    # ========== Git ==========

    def _ensure_git_repo(self):
        """Ensure code_dir is a git repo with a GitHub remote. Idempotent."""
        code_dir = self.code_dir
        # Already a git repo?
        check = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, cwd=code_dir, timeout=10,
        )
        if check.returncode != 0:
            # git init
            subprocess.run(["git", "init"], cwd=code_dir, capture_output=True, timeout=30)
            # Create .gitignore if missing
            gitignore = Path(code_dir) / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(
                    "__pycache__/\n*.pyc\n.env\n*.log\nslurm_*.out\n"
                    "auto_research/logs/\n*.pdf\n"
                )
                subprocess.run(["git", "add", ".gitignore"], cwd=code_dir, capture_output=True, timeout=10)
            self.log("Git: initialized repository", "INFO")

        # Check for remote
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=code_dir, timeout=10,
        )
        if remote.returncode != 0:
            # Create GitHub repo via gh CLI
            try:
                result = subprocess.run(
                    ["gh", "repo", "create", self.project_name,
                     "--private", "--source", str(code_dir), "--push"],
                    capture_output=True, text=True, cwd=code_dir, timeout=60,
                )
                if result.returncode == 0:
                    self.log(f"Git: created GitHub repo '{self.project_name}' and pushed", "INFO")
                else:
                    # Repo might already exist — try adding remote
                    gh_user = subprocess.run(
                        ["gh", "api", "user", "--jq", ".login"],
                        capture_output=True, text=True, timeout=10,
                    )
                    username = gh_user.stdout.strip()
                    if username:
                        subprocess.run(
                            ["git", "remote", "add", "origin",
                             f"git@github.com:{username}/{self.project_name}.git"],
                            cwd=code_dir, capture_output=True, timeout=10,
                        )
                        self.log(f"Git: added remote origin for {username}/{self.project_name}", "INFO")
                    else:
                        self.log(f"Git: could not create GitHub repo: {result.stderr[:200]}", "WARN")
            except FileNotFoundError:
                self.log("Git: gh CLI not found, skipping GitHub repo creation", "WARN")
            except Exception as e:
                self.log(f"Git: GitHub repo creation failed: {e}", "WARN")

    def git_commit(self, message: str, files: list = None):
        """Auto git commit and push at key checkpoints."""
        try:
            self._ensure_git_repo()

            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=self.code_dir, timeout=30
            )

            if not status_result.stdout.strip():
                self.log("Git: no changes to commit", "INFO")
                return False

            if files:
                for f in files:
                    subprocess.run(["git", "add", f], cwd=self.code_dir, timeout=30)
            else:
                latex_dir_name = self.config.get("latex_dir", "Latex")
                key_files = [
                    f"{latex_dir_name}/main.tex",
                    f"{latex_dir_name}/*.bib",
                    "report.md",
                    "auto_research/state/*.yaml",
                    "auto_research/state/*.md",
                    "experiments/",
                    "code/",
                ]
                for pattern in key_files:
                    subprocess.run(
                        ["git", "add", pattern],
                        cwd=self.code_dir, timeout=30,
                        capture_output=True
                    )

            commit_msg = f"[{self.project_name.upper()}] {message}\n\nIteration: {self.iteration}\nScore: {getattr(self, '_last_score', 'N/A')}"
            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                capture_output=True, text=True, cwd=self.code_dir, timeout=60
            )

            if result.returncode == 0:
                self.log(f"Git commit: {message}", "INFO")
                # Auto push
                push = subprocess.run(
                    ["git", "push", "-u", "origin", "HEAD"],
                    capture_output=True, text=True, cwd=self.code_dir, timeout=60,
                )
                if push.returncode == 0:
                    self.log("Git: pushed to GitHub", "INFO")
                else:
                    self.log(f"Git: push failed: {push.stderr[:200]}", "WARN")
                return True
            else:
                self.log(f"Git commit failed: {result.stderr[:200]}", "WARN")
                return False

        except Exception as e:
            self.log(f"Git commit error: {e}", "ERROR")
            return False

    # ========== Notifications ==========

    def inject_user_update(self, message: str):
        """Write a message into user_updates.yaml, as if the user had run 'ark update'."""
        updates_file = self.state_dir / "user_updates.yaml"
        try:
            data = {}
            if updates_file.exists():
                with open(updates_file) as f:
                    data = yaml.safe_load(f) or {}
            updates = data.get("updates", [])
            updates.append({
                "message": message,
                "consumed": False,
                "timestamp": datetime.now().isoformat(),
                "source": "telegram_reply",
            })
            data["updates"] = updates
            with open(updates_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
            self.log(f"Telegram reply injected as user update: {message[:80]}", "INFO")
        except Exception as e:
            self.log(f"Failed to inject user update: {e}", "WARN")

    def ask_telegram_user(self, question: str, timeout: int = 1800) -> str | None:
        """Send a question via Telegram and block until the user replies (or timeout).

        Returns the reply text, or None if not configured / timed out.
        The reply is also injected into user_updates.yaml.
        """
        if not self.telegram.is_configured:
            self.log("ask_telegram_user: Telegram not configured, skipping.", "WARN")
            return None

        self.log(f"Waiting for Telegram reply (timeout {timeout}s)...", "INFO")
        reply = self.telegram.ask(question, timeout=timeout)

        if reply:
            self.log(f"Telegram reply received: {reply[:80]}", "INFO")
            self.inject_user_update(reply)
            return reply

        self.log(f"ask_telegram_user: timed out after {timeout}s, continuing.", "WARN")
        return None

    def _send_session_banner(self):
        """Send a rich session start banner to Telegram."""
        if not self.telegram.is_configured:
            return

        # Gather resume context
        resume_info = "From scratch"
        score_info = ""

        if self.mode == "paper":
            paper_state = self.load_paper_state()
            current_score = paper_state.get("current_score", 0)
            reviews = paper_state.get("reviews", [])
            status = paper_state.get("status", "running")

            if self.iteration > 0:
                # Resuming
                checkpoint = self.load_checkpoint()
                completed_step = checkpoint.get("completed_step", 0)
                step_name = checkpoint.get("completed_step_name", "")
                if completed_step > 0 and completed_step < 5:
                    resume_info = f"Resume iter {self.iteration + 1}, step {completed_step + 1}/5 ({step_name} done)"
                else:
                    resume_info = f"Resume from iter {self.iteration + 1}"

                if current_score > 0:
                    gap = self.paper_accept_threshold - current_score
                    recent = [r.get("score", 0) for r in reviews[-5:]]
                    trend = " → ".join(f"{s:.1f}" for s in recent) if recent else ""
                    score_info = f"Score: {current_score}/10 | Gap: {gap:.1f}\n"
                    if trend:
                        score_info += f"History: {trend}\n"

                    # Stagnation warning
                    stag = getattr(self.memory, 'stagnation_count', 0)
                    if stag >= 2:
                        score_info += f"⚠️ Stagnation: {stag} rounds\n"
            else:
                resume_info = "Starting fresh"
        elif self.mode == "dev":
            if self.iteration > 0:
                resume_info = f"Resume from iter {self.iteration + 1}"
            else:
                resume_info = "Starting fresh"

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━━",
            f"🚀  {self.project_name}  |  {self.mode} mode",
            f"{resume_info}",
        ]
        if score_info:
            lines.append(score_info.rstrip())
        lines.append(f"Target: {self.paper_accept_threshold}/10  |  Max {self.max_iterations} iter")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━")

        self.telegram.send_raw("\n".join(lines))

    def send_notification(self, subject: str, message: str, priority: str = "normal"):
        """Send notification via Telegram (primary) and email (fallback).

        Notifications are formatted with distinctive banners based on type
        so they're easy to scan at a glance in Telegram. Non-critical
        notifications are routed to notify_progress() so the user actually
        sees them, instead of being silently dropped.
        """
        critical_keywords = ["error", "failed", "token", "accepted", "completed", "timeout", "started", "finished"]
        should_send = priority == "critical" or any(kw in subject.lower() for kw in critical_keywords)

        if not should_send:
            # Don't drop — route to a short progress ping so the user sees it.
            try:
                self.notify_progress(subject, message[:200] if message else "", level="info")
            except Exception:
                self.log(f"Notification rerouted-to-progress failed: {subject}", "INFO")
            return

        # Pick a distinctive banner based on notification type
        subj_lower = subject.lower()
        if "accepted" in subj_lower:
            banner = "🎉 ══ ACCEPTED ══"
        elif "error" in subj_lower or "failed" in subj_lower:
            banner = "❌ ══ ERROR ══"
        elif "started" in subj_lower:
            banner = "🚀 ══ STARTED ══"
        elif "finished" in subj_lower or "completed" in subj_lower:
            banner = "🏁 ══ FINISHED ══"
        elif "stagnation" in subj_lower:
            banner = "⚠️ ══ STAGNATION ══"
        elif "rate limit" in subj_lower or "quota" in subj_lower:
            banner = "⏳ ══ RATE LIMIT ══"
        else:
            banner = f"📢 {subject}"

        full_message = f"<b>{banner}</b>\n{message}"

        if self.telegram.is_configured:
            try:
                self.telegram.send(full_message, parse_mode="HTML")
                self.log(f"Telegram notification sent: {subject}", "INFO")
                return
            except Exception as e:
                self.log(f"Telegram notification failed: {e}, falling back to email", "WARN")

        # Fallback: email
        try:
            email = self.config.get("notification_email", "contact@idea2paper.org")
            subprocess.run(
                ["mail", "-s", f"[{self.project_name.upper()}] {subject}", email],
                input=full_message,
                text=True,
                timeout=30,
            )
            self.log(f"Email notification sent: {subject}", "INFO")
        except Exception as e:
            self.log(f"Failed to send notification: {e}", "WARN")

    # ========== Telegram Enhancements ==========

    def _status_block(self) -> str:
        """Compact 3-4 line status header used by every important message.

        Pulls from already-cached state (no new I/O on the hot path).
        Output is Telegram HTML.
        """
        import html as _html
        name = _html.escape(self.display_name or self.project_name)
        mode = self.mode or "?"
        max_iter = self.max_iterations or 0
        line1 = f"<b>{name}</b> · {mode} · iter {self.iteration}/{max_iter}"

        score_line = ""
        trend_line = ""
        stag_line = ""
        if self.mode == "paper":
            try:
                paper_state = self.load_paper_state()
                current_score = paper_state.get("current_score", 0) or 0
                if current_score:
                    gap = self.paper_accept_threshold - current_score
                    score_line = (
                        f"Score <b>{current_score}/10</b> → target "
                        f"{self.paper_accept_threshold}/10 (gap {gap:.1f})"
                    )

                # Recent score trend (last 5)
                reviews = paper_state.get("reviews") or []
                recent = [r.get("score", 0) for r in reviews[-5:]]
                if len(recent) >= 2:
                    trend_line = "Recent: " + " → ".join(f"{s:.1f}" for s in recent)

                # Stagnation: explain the rule inline so the user understands.
                # The memory module uses MIN_PROGRESS_DELTA=0.3 and
                # STAGNATION_THRESHOLD=5 — see ark/memory.py.
                stag = getattr(self.memory, "stagnation_count", 0)
                if stag >= 2:
                    stag_line = (
                        f"⚠️ Stagnation: <b>{stag}/5</b> rounds without "
                        f"≥0.3 score gain (self-repair triggers at 5)"
                    )
            except Exception:
                pass

        lines = [line1]
        if score_line:
            lines.append(score_line)
        if trend_line:
            lines.append(trend_line)
        if stag_line:
            lines.append(stag_line)
        return "\n".join(lines)

    def _polish_ctx(self, kind: str, phase: str = "") -> dict:
        """Context dict passed to Haiku polish (small + privacy-light)."""
        try:
            current_score = 0
            if self.mode == "paper":
                ps = self.load_paper_state()
                current_score = ps.get("current_score", 0) or 0
        except Exception:
            current_score = 0
        return {
            "project": self.display_name or self.project_name,
            "mode": self.mode,
            "iteration": self.iteration,
            "score": current_score,
            "phase": phase,
            "kind": kind,
        }

    def notify_progress(self, stage: str, detail: str = "", level: str = "info"):
        """Send a short progress ping at a pipeline checkpoint.

        Bypasses send_notification's keyword filter (which silently drops
        non-critical events). Routes through send_async so it never blocks
        the orchestrator. Polish OFF — these are short status lines.
        """
        if not self.telegram.is_configured:
            return
        if not self.config.get("telegram_progress_notify", True):
            return

        emoji = {
            "start": "▶️",
            "done": "✅",
            "working": "⚙️",
            "warn": "⚠️",
            "info": "•",
        }.get(level, "•")

        import html as _html
        stage_html = _html.escape(stage)
        detail_html = _html.escape(detail) if detail else ""
        line = f"{emoji} <b>{stage_html}</b>" + (f" — {detail_html}" if detail_html else "")

        try:
            msg = f"{self._status_block()}\n{line}"
            self.telegram.send_async(msg, parse_mode="HTML", polish=False)
        except Exception as e:
            self.log(f"notify_progress failed: {e}", "WARN")

    def ask_user_decision(self, question: str, options: list = None,
                          timeout: int = 900, default: int = 0,
                          *, what_happened: str = "",
                          background: list = None,
                          option_details: list = None,
                          phase: str = "",
                          polish: bool = True) -> tuple:
        """Send a multiple-choice decision request via Telegram.

        Backwards compatible: existing callers passing only positional args
        continue to work. New keyword args (`what_happened`, `background`,
        `option_details`, `phase`) opt in to the rich format. A "Custom"
        escape option is always appended automatically so the user is never
        forced into a canned choice.

        Returns (idx, reply_text). If the user typed a number, idx is that
        index and reply_text is the raw reply. If the user typed free text,
        idx is len(options)-1 (the Custom slot) and reply_text is the text.
        On timeout, returns (default, "").
        """
        if not self.telegram.is_configured:
            self.log(f"No Telegram configured, using default option {default}", "WARN")
            return default, ""

        timeout = self.config.get("telegram_decision_timeout", timeout)
        timeout_min = max(timeout // 60, 1)

        # Always offer a Custom escape (auto-appended if missing)
        opts = list(options or [])
        details = list(option_details or [])
        if not opts or not any("custom" in (o or "").lower() for o in opts):
            opts.append("Custom — type your own instruction")
            details.append("Free text. Whatever you reply becomes the next directive.")
        # Pad details so indices line up
        while len(details) < len(opts):
            details.append("")

        # Build the rich message
        import html as _html
        parts = [self._status_block(), "━━━━━━━━━━━━━━━━━━━━━",
                 "⚠️ <b>Decision needed</b>"]

        if what_happened:
            parts.append("")
            parts.append("<b>What happened</b>")
            parts.append(_html.escape(what_happened))

        if background:
            parts.append("")
            parts.append("<b>Background</b>")
            for b in background:
                if b:
                    parts.append(f"• {_html.escape(str(b))}")

        # If no rich context was supplied, fall back to using `question`
        # itself as the "what happened" body so legacy callers still get
        # a sensible message.
        if not what_happened and not background and question:
            parts.append("")
            parts.append(_html.escape(question))

        parts.append("")
        parts.append(
            f"<b>Options</b> (auto-pick <b>#{default + 1}</b> in {timeout_min} min)"
        )
        for i, (opt, det) in enumerate(zip(opts, details), 1):
            mark = "  ← default" if (i - 1) == default else ""
            parts.append(f"<b>{i}.</b> {_html.escape(opt)}{mark}")
            if det:
                parts.append(f"   ↳ <i>{_html.escape(det)}</i>")

        parts.append("")
        parts.append(f"Reply <b>1–{len(opts)}</b>, or type your own message.")

        msg = "\n".join(parts)

        # Apply polish synchronously for ask() (which needs to send first,
        # then block on the reply event). Same fail-soft semantics as the
        # async sender thread.
        polished = msg
        if polish and self.telegram._polish_fn is not None:
            try:
                ctx = self._polish_ctx("decision", phase=phase)
                result = [msg]
                def _run():
                    try:
                        r = self.telegram._polish_fn(msg, ctx)
                        if r and isinstance(r, str):
                            result[0] = r
                    except Exception:
                        pass
                t = threading.Thread(target=_run, daemon=True)
                t.start()
                t.join(timeout=getattr(self.telegram, "_polish_timeout", 8.0))
                polished = result[0]
            except Exception:
                polished = msg

        # Send + wait for reply. ask_telegram_user wraps telegram.ask which
        # uses send() (synchronous, then blocks on event). We pass the
        # already-rendered HTML directly via telegram.ask to preserve format.
        if not self.telegram.is_configured:
            return default, ""

        self.log(f"Waiting for Telegram decision reply (timeout {timeout}s)...", "INFO")
        # Use telegram.ask() but bypass its to_html re-conversion since we
        # already produced HTML. Send directly then wait.
        self.telegram.send(polished, parse_mode="HTML")
        self.telegram._is_waiting = True
        self.telegram._ask_reply = None
        self.telegram._ask_event.clear()
        try:
            got = self.telegram._ask_event.wait(timeout=timeout)
            reply = self.telegram._ask_reply if got else None
            if got and reply:
                self.telegram.send_raw("✅ Received, continuing...")
        finally:
            self.telegram._is_waiting = False

        if reply is None:
            default_label = opts[default] if opts else "N/A"
            self.log(f"Decision timed out, using default: {default_label}", "WARN")
            self.telegram.send_async(
                f"⏰ <b>{_html.escape(self.display_name)}</b>: timeout — "
                f"auto-selected option <b>#{default + 1}</b>: "
                f"{_html.escape(default_label)}",
                parse_mode="HTML",
                polish=False,
            )
            return default, ""

        # Numeric reply → option index. A bare digit is just a menu selection;
        # do NOT inject it as a user_update directive, otherwise downstream
        # agents would see "2" as next-iteration guidance.
        try:
            idx = int(reply.strip()) - 1
            if 0 <= idx < len(opts):
                return idx, reply
        except ValueError:
            pass

        # Free-text reply lands in the Custom slot (last option). This IS
        # real user guidance, so inject it into user_updates.yaml so the
        # next iteration's agents pick it up.
        try:
            self.inject_user_update(reply)
        except Exception:
            pass
        return len(opts) - 1, reply

    def send_error_alert(self, error: str, phase: str, blocking: bool = False,
                         options: list = None) -> str:
        """Send a structured error alert. If blocking, waits for user reply."""
        # Truncate long errors so the message stays scannable
        err_short = error if len(error) <= 600 else error[:600] + "..."

        if blocking:
            opts = list(options) if options else [
                "Retry now",
                "Skip and continue",
                "Pause and wait for me",
            ]
            details = [
                "Re-runs the failing step from the same state.",
                "Marks this step as done with the current (broken) output and moves on.",
                "Holds the orchestrator until you reply with new guidance.",
            ][: len(opts)]
            idx, reply = self.ask_user_decision(
                question=f"Error in {phase}",
                options=opts,
                timeout=3600,
                default=0,
                what_happened=f"{phase} failed: {err_short}",
                background=[
                    f"Phase: {phase}",
                    f"Iteration: {self.iteration}",
                ],
                option_details=details,
                phase=phase,
                polish=True,
            )
            return reply or (opts[idx] if opts else "")

        # Non-blocking: just notify
        import html as _html
        msg = (
            f"{self._status_block()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ <b>Error in {_html.escape(phase)}</b>\n"
            f"<pre>{_html.escape(err_short)}</pre>"
        )
        if self.telegram.is_configured:
            self.telegram.send_async(
                msg, parse_mode="HTML", polish=True,
                polish_ctx=self._polish_ctx("error", phase=phase),
            )
        return None


def main():
    parser = argparse.ArgumentParser(description="ARK Automated Research Orchestrator")
    parser.add_argument("--mode", type=str, default="research", choices=["research", "paper", "dev"],
                        help="Mode: 'research' for experiments, 'paper' for review iterations, 'dev' for development iterations")
    parser.add_argument("--project", type=str, required=True, help="Project name (e.g., prouter)")
    parser.add_argument("--model", type=str, default=None, choices=["claude", "gemini", "codex"],
                        help="Model backend: 'claude', 'gemini', or 'codex'")
    parser.add_argument("--max-days", type=float, default=3, help="Maximum runtime in days")
    parser.add_argument("--iterations", type=int, default=100, help="Number of iterations to run")
    parser.add_argument("--code-dir", type=str, default=None,
                        help="Override code directory (default: from project config)")
    parser.add_argument("--project-dir", type=str, default=None,
                        help="Override project directory (default: ARK_ROOT/projects/<project>)")
    parser.add_argument("--db-path", type=str, default=None,
                        help="Path to webapp SQLite DB for status sync")
    parser.add_argument("--project-id", type=str, default=None,
                        help="Project UUID in the webapp DB")
    args = parser.parse_args()
    
    # Handle termination signals
    def signal_handler(sig, frame):
        print(f"\nTermination signal {sig} received. cleaning up...", file=sys.stderr)
        sys.exit(0)
    signal.signal(signal.SIGTERM, signal_handler)

    # Resolve DB path: explicit arg > env > webapp.env > default
    db_path = args.db_path
    project_id = args.project_id
    if not db_path:
        from website.dashboard.db import resolve_db_path
        db_path = resolve_db_path()

    # Load project config to resolve code_dir if not specified
    project_dir = args.project_dir
    config_file = (Path(project_dir) if project_dir else ARK_ROOT / "projects" / args.project) / "config.yaml"
    code_dir = args.code_dir
    if code_dir is None and config_file.exists():
        import yaml as _yaml
        with open(config_file) as f:
            cfg = _yaml.safe_load(f) or {}
        code_dir = cfg.get("code_dir")

    # Auto-resolve project_id from DB if not provided
    if not project_id and db_path and Path(db_path).exists():
        try:
            from website.dashboard.db import get_session, get_project_by_name, get_project
            with get_session(db_path) as session:
                # Try looking up by project name or by project_dir matching id
                p = get_project_by_name(session, args.project)
                if not p:
                    # Maybe --project is actually a UUID
                    p = get_project(session, args.project)
                if p:
                    project_id = p.id
        except Exception:
            pass

    orchestrator = Orchestrator(
        max_days=args.max_days,
        max_iterations=args.iterations,
        mode=args.mode,
        project=args.project,
        model=args.model,
        code_dir=code_dir,
        project_dir=project_dir,
        db_path=db_path,
        project_id=project_id,
    )

    # Mark as running in DB
    if db_path and project_id:
        orchestrator._sync_db(status="running", pid=os.getpid())

    try:
        orchestrator.run()
        # Mark completion in DB
        if db_path and project_id:
            paper_state = orchestrator.load_paper_state()
            final_status = "done"
            if paper_state.get("status") in ("accepted", "accepted_pending_cleanup"):
                final_status = "done"
            orchestrator._sync_db(status=final_status, pid=0)
    except KeyboardInterrupt:
        orchestrator._sync_db(status="stopped", pid=0)
    except Exception:
        orchestrator._sync_db(status="failed", pid=0)
        raise


if __name__ == "__main__":
    main()
