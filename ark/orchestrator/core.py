#!/usr/bin/env python3
"""
ARK (Automatic Research Kit) - Automated Research Orchestrator

Usage:
    python -m ark.orchestrator --project myproject --iterations 10
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import yaml
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from typing import Optional, List
import re
import threading
import signal

# ARK package root (where projects/ lives).
# core.py is at ark/orchestrator/core.py — three .parent walks reach
# the repo root that the original orchestrator.py landed on with two.
ARK_ROOT = Path(__file__).parent.parent.parent.absolute()

# PROJECT_DIR: legacy global, kept for backward compatibility
PROJECT_DIR = None

from ark.memory import get_memory, SimpleMemory
from ark.engines import AgentMixin
from ark.latex import CompilerMixin
from ark.execution import ExecutionMixin
from ark.pipeline import PipelineMixin
from .workspace import WorkspaceManager
from .state import StateManager, _atomic_write_yaml, _atomic_write_text


class Orchestrator(AgentMixin, CompilerMixin, ExecutionMixin, PipelineMixin):
    """Main orchestrator class composing all mixins."""

    def __init__(self, project: str, max_days: float = 3, max_iterations: int = 100,
                 model: str = None, model_variant: str = None, code_dir: str = None,
                 project_dir: str = None, db_path: str = None, project_id: str = None,
                 mode: str = "paper"):
        global PROJECT_DIR

        self.max_end_time = datetime.now() + timedelta(days=max_days)
        self.max_iterations = max_iterations
        self.iteration = 0
        self.mode = "paper" if not mode else mode
        self._model_arg = model
        self.project_name = project

        # ── DB awareness ──
        self._db_path = db_path
        self._project_id = project_id
        self._db_sync_errors = 0
        self._display_name = None

        # 1. Initialize Workspace Manager
        self.workspace = WorkspaceManager(
            project, ARK_ROOT, project_dir=project_dir, code_dir=code_dir, logger=self.log
        )
        self.config = self.workspace.config
        # Bridge config.yaml API keys into os.environ so the OpenHands runtime
        # (OpenHandsCLI.build_env) and the LiteLLM light helpers can find them.
        from ark.llm_lite import load_api_keys_into_env
        load_api_keys_into_env(self.config)
        self.project_path = self.workspace.project_path
        self.code_dir = self.workspace.code_dir
        PROJECT_DIR = self.code_dir  # legacy global

        # 2. Expose Core Paths (for mixin compatibility)
        self.state_dir = self.workspace.state_dir
        self.log_dir = self.workspace.log_dir
        self.agents_dir = self.workspace.agents_dir
        self.latex_dir = self.workspace.latex_dir
        self.figures_dir = self.workspace.figures_dir

        # 3. Initialize State Manager
        self.state = StateManager(self.state_dir, logger=self.log)

        # Backward-compat: many call sites in this module reference file
        # paths as ``self.paper_state_file``, ``self.findings_file``, etc.
        # The refactor that moved StateManager out of orchestrator.py
        # rebound those paths to ``self.state.<file>``; re-expose them on
        # the orchestrator so the existing call sites and external callers
        # (tests, hooks) continue to work without per-call rewrites.
        self.paper_state_file = self.state.paper_state_file
        self.action_plan_file = self.state.action_plan_file
        self.findings_file = self.state.findings_file
        self.literature_file = self.state.literature_file
        self.checkpoint_file = self.state.checkpoint_file
        self.latest_review_file = self.state_dir / "latest_review.md"

        # 4. Finalize Workspace Setup
        self.hooks = self.workspace.setup_workspace()

        # Resolve model
        self.model = self._model_arg or self.config.get("model") or "anthropic/claude-sonnet-4-6"
        if model_variant:
            self.config["model_variant"] = model_variant
        # Export the selected model so every light helper (title / summary /
        # ethical review / classify) runs on the SAME verified-key model as the
        # agents — not a separate bot_model that might have no working key and
        # break the run half-way. Special models (deep research, figures) opt
        # out by choosing their own model directly.
        if self.model and "/" in self.model:
            os.environ["ARK_UTILITY_MODEL"] = self.model

        from ark.config.defaults import DEFAULT_PAPER_ACCEPT_THRESHOLD
        self.paper_accept_threshold = self.config.get("paper_accept_threshold", DEFAULT_PAPER_ACCEPT_THRESHOLD)

        from ark.config.defaults import MAX_LOG_FILES_TO_KEEP
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"{self.project_name}_paper_{self.run_id}.log"
        self._cleanup_old_logs(keep=MAX_LOG_FILES_TO_KEEP)
        
        # Create a latest.log symlink for log streaming (Phase 5)
        latest_symlink = self.log_dir / "latest.log"
        try:
            if latest_symlink.is_symlink() or latest_symlink.exists():
                latest_symlink.unlink()
            latest_symlink.symlink_to(self.log_file.name)
        except Exception as e:
            # Fallback if symlinks aren't supported
            pass

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._rate_limit_notified = False
        self._agent_empty_count = 0
        self._quota_exhausted = False
        self._terminal_error = None
        self._asked_this_iteration = False
        self._agent_stats = []
        self._latest_pdf = None
        self._deep_research_thread = None

        # ── HITL control plane (DB-backed; see _poll_control / ask_user_decision) ──
        self._paused = False
        self._stop_requested = False
        self._autonomy_cache = None      # refreshed from DB at checkpoints
        self._control_lock = threading.Lock()  # serialize _poll_control across threads
        self._control_poller = None      # background thread: picks up commands ~every 20s

        # Memory
        self.memory = get_memory(state_dir=self.state_dir)
        self._last_score = 0.0
        if hasattr(self.memory, 'set_goal_anchor'):
            self.memory.set_goal_anchor(self.config.get("goal_anchor", ""))

        # Seed language preference
        prefs_file = self.state_dir / "user_prefs.yaml"
        if not prefs_file.exists():
            config_lang = self.config.get("language", "en")
            with open(prefs_file, "w") as _pf:
                yaml.dump({"language": config_lang}, _pf, default_flow_style=False)

        # Compute backend
        import ark.compute
        self._compute_backend = ark.compute.from_config(
            self.config, self.project_name, self.code_dir, self.log
        )

        # Telegram dispatcher
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
                    from ark.telegram import polish_message
                    polish_model = self.config.get("telegram_polish_model", "claude-haiku-4-5")
                    self.telegram._polish_fn = (
                        lambda text, ctx, _k=anthropic_key, _m=polish_model:
                        polish_message(text, ctx, api_key=_k, model=_m)
                    )
                except Exception as e:
                    self.log(f"Telegram polish hook setup failed: {e}", "WARN")

        # Intervention + observability: pre-action guardrails (delete / bulk
        # jobs / credentials / exfil) plus a live per-step log. Fail-open — with
        # no Telegram channel the gate auto-allows and just logs, so a run is
        # never blocked by a prompt no one can answer.
        try:
            from ark.intervention.manager import InterventionManager
            _secret_vals = [v for v in (
                self.config.get("anthropic_api_key"), self.config.get("openai_api_key"),
                self.config.get("gemini_api_key"), self.config.get("telegram_bot_token"),
                os.environ.get("ARK_GITHUB_PAT"),
            ) if v]
            # Wire the gate to a human channel if EITHER Telegram OR the webapp
            # DB channel is available. _intervention_ask routes through the
            # dual-channel ask_user_decision (webapp chat bubble + Telegram), so
            # the webapp alone is now enough to approve a guarded action.
            # Secret VALUES stay Telegram-only — never store a raw secret in the
            # webapp message thread.
            _tg_ok = self.telegram.is_configured
            _has_channel = _tg_ok or bool(self._db_path and self._project_id)
            self._intervention = InterventionManager(
                self.config, self.state_dir, [self.code_dir],
                ask_fn=(self._intervention_ask if _has_channel else None),
                notify_fn=(self._intervention_notify if _has_channel else None),
                ask_secret_fn=(self._intervention_ask_secret if _tg_ok else None),
                log_fn=self.log,
                secret_values=_secret_vals,
            )
            self._intervention.start()
            # Let the compute backend gate its own (billable) cloud provisioning.
            if getattr(self, "_compute_backend", None) is not None:
                try:
                    self._compute_backend._intervention_check = self._intervention.check_action
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Intervention manager init failed (continuing without): {e}", "WARN")
            self._intervention = None

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

    @staticmethod
    def _looks_like_uuid(value: str) -> bool:
        """True if the string is a bare UUID-ish identifier (hex + dashes).

        Used so the Telegram UX never shows a raw 36-char UUID to the user;
        it falls back to the compact ``Project-<id5>`` form instead.
        """
        if not value:
            return False
        v = value.strip()
        # Canonical UUID (8-4-4-4-12) or bare hex slab
        if len(v) >= 30:
            stripped = v.replace("-", "").replace("_", "")
            if stripped and all(c in "0123456789abcdefABCDEF" for c in stripped):
                return True
        return False

    @property
    def short_id(self) -> str:
        """First 5 hex-ish chars of project_id/project_name for compact headers.

        Target format: ``Project-d9b7f`` for UUID ``d9b7fab8-b466-40ba-...``.
        Falls back to ``?????`` if no identifier is available.
        """
        pid = (self._project_id or self.project_name or "").strip()
        if not pid:
            return "?????"
        compact = pid.replace("-", "").replace("_", "")
        return compact[:5] if compact else pid[:5]

    @property
    def display_name(self) -> str:
        """Human-readable project name.

        Prefers an actual title from config; falls back to ``Project-<id5>``
        so the user never sees a raw UUID in Telegram.
        """
        if self._display_name is None:
            title = (self.config.get("title") or "").strip()
            name = (self.config.get("name") or "").strip()
            if title and not self._looks_like_uuid(title):
                self._display_name = title
            elif name and not self._looks_like_uuid(name):
                self._display_name = name
            else:
                self._display_name = f"Project-{self.short_id}"
        return self._display_name

    def _invalidate_display_name(self):
        """Clear the cached display name so the next read picks up a new title."""
        self._display_name = None

    def tg_header(self, emoji: str = "🚤") -> str:
        """Unified Telegram message header.

        Format: ``{emoji} <b>ARK Project-<id5></b> | <title>`` when a real
        title is known, else just the ``ARK Project-<id5>`` part. Every
        outgoing Telegram message should start with this line so the user
        can scan which project a ping belongs to at a glance.
        """
        import html as _html
        short = self.short_id
        title = (self.config.get("title") or "").strip()
        if title and not self._looks_like_uuid(title):
            return (
                f"{emoji} <b>ARK Project-{_html.escape(short)}</b>"
                f" | {_html.escape(title)}"
            )
        return f"{emoji} <b>ARK Project-{_html.escape(short)}</b>"

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

    # ========== HITL control plane ==========
    # The detached orchestrator can't be reached by the webapp except via the
    # shared DB. These poll a command queue + an autonomy level at safe
    # checkpoints, and drive pause/resume/steer/stop. Decisions go through
    # ask_user_decision (DB-backed, dual-channel: webapp + Telegram).

    _AUTONOMY_ASK = {
        # which decision *kinds* actually prompt the human, per autonomy level
        "full_auto": {"blocker", "gate_a", "irreversible"},
        "collaborative": {"blocker", "gate_a", "irreversible",
                          "experiment_approval", "drift", "decision", "clarification"},
        "hands_on": None,  # None = ask for everything
    }

    def _hitl_db(self):
        """Yield a DB session if the control plane is available, else None."""
        if not self._db_path or not self._project_id:
            return None
        try:
            ark_root = str(Path(__file__).parent.parent.absolute())
            if ark_root not in sys.path:
                sys.path.insert(0, ark_root)
            from website.dashboard import db as _db  # noqa: F401
            return _db
        except Exception:
            return None

    def _set_activity(self, text: str):
        _db = self._hitl_db()
        if not _db:
            return
        try:
            with _db.get_session(self._db_path) as s:
                _db.set_activity(s, self._project_id, text)
        except Exception:
            pass

    def _set_control_state(self, state: str):
        _db = self._hitl_db()
        if not _db:
            return
        try:
            with _db.get_session(self._db_path) as s:
                _db.set_control_state(s, self._project_id, state)
        except Exception:
            pass

    def autonomy(self) -> str:
        """Current autonomy level (DB-backed, falls back to config/default)."""
        _db = self._hitl_db()
        if _db:
            try:
                with _db.get_session(self._db_path) as s:
                    p = _db.get_project(s, self._project_id)
                    if p and p.autonomy_level:
                        self._autonomy_cache = p.autonomy_level
            except Exception:
                pass
        return (self._autonomy_cache
                or self.config.get("autonomy_level", "collaborative"))

    def _should_ask(self, kind: str) -> bool:
        """Whether a decision of this kind should actually prompt the human,
        given the project's autonomy level."""
        allow = self._AUTONOMY_ASK.get(self.autonomy(), set())
        return True if allow is None else (kind in allow)

    def _ensure_control_poller(self):
        """Start a daemon thread that drains control commands ~every 20s, so a
        steer/pause/stop the user sends mid-step is picked up without waiting for
        the current (possibly multi-minute) agent call to finish. Step-boundary
        checkpoints still poll too; the lock serializes them. Idempotent."""
        if self._control_poller is not None or not (self._db_path and self._project_id):
            return

        def _loop():
            while not self._stop_requested:
                time.sleep(20)
                try:
                    self._poll_control()
                except Exception:
                    pass

        t = threading.Thread(target=_loop, name="ark-control-poller", daemon=True)
        self._control_poller = t
        t.start()

    def _poll_control(self):
        """Drain pending control commands from the DB and apply them. Safe to
        call from both the main thread (checkpoints) and the background poller —
        a lock prevents two threads from racing on the same command batch."""
        _db = self._hitl_db()
        if not _db:
            return
        if not self._control_lock.acquire(blocking=False):
            return  # another thread is already draining; skip this tick
        try:
            try:
                with _db.get_session(self._db_path) as s:
                    cmds = _db.take_pending_commands(s, self._project_id)
            except Exception:
                return
            self._apply_control_commands(cmds)
        finally:
            self._control_lock.release()

    def _apply_control_commands(self, cmds):
        for c in cmds:
            kind, payload = c.get("kind"), c.get("payload", "")
            if kind == "pause":
                self._paused = True
                self._set_control_state("paused")
                self.log("⏸  Paused by user.", "INFO")
            elif kind == "resume":
                self._paused = False
                self._set_control_state("")
                self.log("▶️  Resumed by user.", "INFO")
            elif kind == "stop":
                self._stop_requested = True
                self.log("⏹  Stop requested by user.", "WARN")
            elif kind == "steer" and payload:
                try:
                    self.inject_user_update(payload)
                except Exception:
                    pass
                self.log(f"🧭  Steer from user: {payload[:140]}", "INFO")
                self._chat("agent",
                           f"✅ Applied — I'll factor this into the upcoming steps: “{payload[:120]}”",
                           kind="message")
            elif kind == "set_autonomy" and payload:
                try:
                    with _db.get_session(self._db_path) as s:
                        _db.set_autonomy(s, self._project_id, payload)
                    self._autonomy_cache = payload
                    self.log(f"⚙️  Autonomy → {payload}", "INFO")
                except Exception:
                    pass

    def _maybe_park(self):
        """If paused, block (polling for resume/stop) until unparked."""
        if not self._paused:
            return
        self.log("⏸  Parked — waiting for resume…", "INFO")
        while self._paused and not self._stop_requested:
            if datetime.now() >= self.max_end_time:
                break
            time.sleep(3)
            self._poll_control()
        if not self._paused:
            self.log("▶️  Unparked — continuing.", "INFO")

    def checkpoint(self, label: str = ""):
        """Safe point between steps: surface activity, apply control commands,
        honor pause, and break out on stop. Call liberally between major steps."""
        if label:
            self._set_activity(label)
        self._ensure_control_poller()
        self._poll_control()
        self._maybe_park()
        if self._stop_requested:
            raise KeyboardInterrupt("stop requested via control command")

    def _parse_decision_reply(self, reply, opts):
        """(idx, is_freetext): a bare number selects an option; else free text."""
        try:
            idx = int(str(reply).strip()) - 1
            if 0 <= idx < len(opts):
                return idx, False
        except (ValueError, TypeError):
            pass
        return -1, True

    def _chat(self, role: str, text: str, kind: str = "message", meta: dict = None):
        """Append a bubble to the project's conversation thread (fail-soft)."""
        _db = self._hitl_db()
        if not _db:
            return
        try:
            with _db.get_session(self._db_path) as s:
                _db.add_message(s, self._project_id, role, text, kind=kind, meta=meta)
        except Exception:
            pass

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
                self.telegram.send(
                    f"{self.tg_header('🚤')}\n"
                    f"⚠️ <b>Deep Research failed</b>\n{error_msg[:200]}",
                    parse_mode="HTML",
                )

        self.log("Starting Deep Research in background...", "INFO")
        if self.telegram.is_configured:
            self.telegram.send(
                f"{self.tg_header('🚤')}\n"
                f"🔎 <b>Deep Research started</b> (Gemini, ~5-20 min)",
                parse_mode="HTML",
            )

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
            self.telegram.send(
                f"{self.tg_header('🚤')}\n"
                f"✅ <b>Deep Research completed</b> — sending report...",
                parse_mode="HTML",
            )
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
        # Stop the intervention approval watcher thread.
        if getattr(self, "_intervention", None) is not None:
            try:
                self._intervention.stop()
            except Exception:
                pass

    def _get_bot_model(self) -> str:
        """
        Return the model used for Telegram bot replies.

        Prefers a per-project ``bot_model`` from the project config; falls
        back to the default. No global config fallback — ARK is multi-tenant.
        """
        from ark.llm_lite import default_utility_model
        # Default to the run's selected model (config ``model``) so the bot
        # replies on the same verified-key model as everything else; an explicit
        # ``bot_model`` still overrides. Falls back to a cheap model only when no
        # model is configured at all.
        return self.config.get("bot_model") or self.config.get("model") or default_utility_model()

    def _handle_telegram_message(self, text: str):
        """Handle incoming Telegram message via Claude agent."""
        import threading as _threading
        from ark.telegram import TelegramDispatcher

        # All messages go through Claude agent (it decides actions like sending PDF)
        _threading.Thread(target=self._agent_respond_telegram, args=(text,), daemon=True).start()

    def _build_tg_system_prompt(self) -> str:
        """Stable identity block: project name/title/venue/goal + language + style + capabilities."""
        lang = self.get_language_pref()
        lang_instruction = "Reply in Chinese." if lang == "zh" else "Reply in English."

        title = self.config.get("title", self.project_name)
        venue = self.config.get("venue", "")
        goal = self.config.get("goal_anchor", "")
        short = self.short_id

        # Identity: always include the short id so the user can disambiguate
        # between projects that share a bot even when titles are not yet set.
        identity = f'You are ARK Bot for Project-{short}'
        if title and title != self.project_name and not self._looks_like_uuid(title):
            identity += f' ("{title}")'
        if venue:
            identity += f', targeting {venue}'
        identity += (
            ". You are the Telegram-facing interface of an ARK research "
            "pipeline that is currently running. You know the project's "
            "state and can inject user directives into it."
        )

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
            "WHAT YOU CAN DO (if the user asks):",
            "- Explain what the pipeline is doing right now, what the latest score is, what went wrong, what's blocking.",
            "- Send the paper PDF on request.",
            "- Inject one-time course-corrections for the current/next iteration.",
            "- Save persistent rules the pipeline must follow from now on.",
            "",
            "CAPABILITY TAGS (add on a new line at the very end of your reply; do NOT mention the tags in the prose):",
            "- [SEND_PDF] — attach the latest compiled paper PDF.",
            "- [ACTION: one-sentence directive] — one-time directive for the current/next iteration "
            "(e.g. \"skip experiments this round\", \"regenerate figure 3\").",
            "- [INSTRUCTION: one-sentence rule] — persistent rule that applies to ALL future iterations "
            "(e.g. \"always use PyTorch\", \"crawl real data from website X\").",
            "",
            "GUARDRAILS (do NOT violate):",
            "- Pick [ACTION] for temporary/situational requests, [INSTRUCTION] for lasting rules.",
            "- NEVER add either tag for: status queries, simple acknowledgments (ok, proceed, continue, 好的, 继续, 收到), or confirmations of what's already happening.",
            "- For destructive-sounding requests (\"delete everything\", \"wipe state\", \"force-reset\"), "
            "do NOT emit a tag on the first reply. Ask the user to confirm in plain words first, and only emit a tag after they explicitly confirm in the next message.",
            "- You cannot edit config.yaml, paper_state.yaml, or any code directly. If the user asks for a config change "
            "(e.g. \"set max_iterations to 30\"), express it as an [INSTRUCTION] the pipeline can honour.",
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
            from ark.llm_lite import complete
            response = complete(prompt, model=self._get_bot_model(), timeout=90)
            if not response:
                response = "Sorry, unable to respond right now."
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
        """Collect project state for the Telegram agent's system prompt.

        The agent runs as a separate Claude process so it cannot inspect
        in-memory orchestrator state — everything we want it to reason
        about has to be flattened into this block. Keep each section
        bounded so the prompt stays small.
        """
        lines = []
        lines.append(f"Project ID: {self.project_name} (short: {self.short_id})")
        title = (self.config.get("title") or "").strip()
        if title and not self._looks_like_uuid(title):
            lines.append(f"Title: {title}")
        lines.append(
            f"Mode: {self.mode} | Iteration: {self.iteration}/{self.max_iterations}"
        )

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

        # Cost so the user can ask "how much have we spent"
        cost_file = self.state_dir / "cost_report.yaml"
        if cost_file.exists():
            try:
                with open(cost_file) as f:
                    cost = yaml.safe_load(f) or {}
                total = cost.get("total_cost_usd") or cost.get("total_cost")
                if total:
                    lines.append(f"Cost so far: ${float(total):.2f}")
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

        # Pending user_updates.yaml entries not yet consumed — so the agent
        # knows what directives the pipeline is about to pick up.
        try:
            updates_file = self.state_dir / "user_updates.yaml"
            if updates_file.exists():
                with open(updates_file) as f:
                    udata = yaml.safe_load(f) or {}
                pending = [u for u in udata.get("updates", []) if not u.get("consumed")]
                if pending:
                    lines.append(
                        "\nPending directives (queued, not yet consumed):\n"
                        + "\n".join(f"- {u.get('message', '')}" for u in pending[-5:])
                    )
        except Exception:
            pass

        # Expanded log window so the agent can tell the user what the
        # pipeline is actually doing right now, not just a stale snapshot.
        try:
            log_lines = [l for l in self.log_file.read_text().splitlines() if l.strip()][-40:]
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
        The on-disk file is `main.pdf` (LaTeX-build convention from
        main.tex); we present it as `paper.pdf` in Telegram so the user
        sees a descriptive name in chat.
        """
        pdf = getattr(self, '_latest_pdf', None)
        if pdf is None or not pdf.exists():
            self.telegram.send_raw("No compiled PDF available yet.")
            return

        caption = f"📄 {self.display_name} — iter {self.iteration}, score {self._last_score:.1f}/10"
        ok = self.telegram.send_document(pdf, caption=caption, filename="paper.pdf")
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

    def _generate_project_summary_md(self, score: float, prev_score: float) -> str:
        """Build a per-iteration project summary in markdown.

        Lead with a "Progress Summary" that frames what the project
        explores and what's been done, then a sequence of subsections:
        what changed this iteration, the reviewer's feedback, known
        limitations, suggested next steps, and citation verification.
        The reviewer feedback subsection replaces the standalone
        latest_review.md PDF — the user receives one consolidated
        document instead of two overlapping ones.
        """
        import re as _re
        import yaml as _yaml

        lines = [
            f"# {self.display_name} — Project Summary",
            f"_Iteration {self.iteration} • Score {score:.2f}/10 "
            f"(previous {prev_score:.2f}/10) • Target "
            f"{self.paper_accept_threshold}/10_",
            "",
        ]

        # Read shared state once for use across multiple sections.
        idea_md = self.state_dir / "idea.md"
        idea_text = ""
        if idea_md.exists():
            idea_text = _re.sub(
                r"^#[^\n]*\n+", "",
                idea_md.read_text(errors="replace").strip(),
                count=1,
            )

        review_md = self.state_dir / "latest_review.md"
        review_text = (
            review_md.read_text(errors="replace") if review_md.exists() else ""
        )

        # ── Progress Summary (lead) ──────────────────────────────
        # Narrative that opens the document: what is this project
        # exploring + cumulative progress so far. Pulls the first
        # paragraph or two of idea.md as the framing, then states
        # the current state ("at iteration N with score X").
        lines.append("## Progress Summary")
        if idea_text:
            # Take the first paragraph as the opening framing — it's
            # usually the elevator pitch.
            first_para = idea_text.split("\n\n", 1)[0].strip()
            if len(first_para) > 1200:
                first_para = first_para[:1200].rstrip() + "…"
            lines.append(first_para)
            lines.append("")
        lines.append(
            f"After {self.iteration} review iteration"
            f"{'s' if self.iteration != 1 else ''}, the paper currently scores "
            f"**{score:.2f}/10** against a target of "
            f"{self.paper_accept_threshold}/10. The sections below summarise "
            f"this iteration's changes, the reviewer's feedback, the "
            f"open methodological gaps, and the suggested next steps."
        )
        lines.append("")

        # ── What changed this iteration ──────────────────────────
        lines.append("## Changes this iteration")
        findings_path = self.state_dir / "findings.yaml"
        added_any = False
        if findings_path.exists():
            try:
                data = _yaml.safe_load(findings_path.read_text()) or {}
                findings = data.get("findings") or []
                relevant = [
                    f for f in findings
                    if isinstance(f, dict)
                    and f.get("iteration") in (self.iteration, str(self.iteration))
                ] or findings[-5:]
                for f in relevant[-8:]:
                    if not isinstance(f, dict):
                        continue
                    title = (f.get("title") or "").strip()
                    summary = (f.get("summary") or
                               f.get("description") or "").strip()
                    if title:
                        lines.append(f"- **{title}**: {summary}")
                        added_any = True
                    elif summary:
                        lines.append(f"- {summary}")
                        added_any = True
            except Exception as e:
                lines.append(f"_findings.yaml unreadable: {e}_")
                added_any = True
        if not added_any:
            lines.append("_No iteration-specific findings recorded._")
        lines.append("")

        # ── Reviewer feedback (subsumes the standalone review PDF) ──
        lines.append("## Reviewer feedback")
        if not review_text.strip():
            lines.append("_No reviewer report available yet._")
        else:
            # Score table → keep as-is for context (it's already a
            # markdown table and renders nicely).
            scores_match = _re.search(
                r"(\|\s*Dimension[\s\S]*?\n)(?:\n|##|---)",
                review_text,
            )
            if scores_match:
                lines.append("### Score breakdown")
                # Trim to the table proper (lines starting with `|`).
                table_lines = [
                    ln for ln in scores_match.group(1).split("\n")
                    if ln.strip().startswith("|")
                ]
                lines.extend(table_lines)
                lines.append("")

            # Major Issues — keep both the headings and a one-line
            # context per issue so the user sees the substance.
            major_match = _re.search(
                r"##\s*Major\s*Issues\s*\n(.*?)(?=\n##\s|\Z)",
                review_text, _re.DOTALL | _re.IGNORECASE,
            )
            if major_match:
                lines.append("### Major issues raised")
                snippet = major_match.group(1).strip()
                # Find each "### M1. ..." block.
                blocks = _re.split(r"(?=^###\s+M\d+)", snippet, flags=_re.MULTILINE)
                for block in blocks:
                    block = block.strip()
                    if not block.startswith("###"):
                        continue
                    head_match = _re.match(r"###\s*(M\d+\.[^\n]*)", block)
                    if not head_match:
                        continue
                    heading = head_match.group(1).strip()
                    # First non-heading paragraph after the heading.
                    body = block[head_match.end():].strip()
                    para = body.split("\n\n", 1)[0].strip() if body else ""
                    lines.append(f"- **{heading}** — {para[:400]}"
                                 + ("…" if len(para) > 400 else ""))
                lines.append("")

            # Minor Issues — just count + list IDs/titles.
            minor_match = _re.search(
                r"##\s*Minor\s*Issues\s*\n(.*?)(?=\n##\s|\Z)",
                review_text, _re.DOTALL | _re.IGNORECASE,
            )
            if minor_match:
                heads = [
                    ln.strip().lstrip("#").strip()
                    for ln in minor_match.group(1).split("\n")
                    if ln.strip().startswith("###")
                ]
                if heads:
                    lines.append("### Minor issues")
                    for h in heads[:10]:
                        lines.append(f"- {h}")
                    if len(heads) > 10:
                        lines.append(f"- …and {len(heads) - 10} more")
                    lines.append("")

        # ── Known limitations ────────────────────────────────────
        lines.append("## Known limitations")
        if review_text:
            major_match = _re.search(
                r"##\s*Major\s*Issues\s*\n(.*?)(?=\n##\s|\Z)",
                review_text, _re.DOTALL | _re.IGNORECASE,
            )
            head_lines = []
            if major_match:
                head_lines = [
                    ln.strip().lstrip("#").strip()
                    for ln in major_match.group(1).split("\n")
                    if ln.strip().startswith("###")
                ]
            if head_lines:
                for h in head_lines[:8]:
                    lines.append(f"- {h}")
            else:
                lines.append("_Reviewer report did not list Major Issues._")
        else:
            lines.append("_No reviewer report available yet._")
        lines.append("")

        # ── Suggested next steps ─────────────────────────────────
        lines.append("## Suggested next steps")
        if review_text:
            pathway_match = _re.search(
                r"##\s*Acceptance\s*Pathway\s*\n(.*?)(?=\n##\s|\Z)",
                review_text, _re.DOTALL | _re.IGNORECASE,
            )
            if pathway_match:
                lines.append(pathway_match.group(1).strip())
            else:
                hints = _re.findall(
                    r"(?:^|\n)[^\n]*?\b(?:should|could|recommend|consider)\b[^\n]*",
                    review_text, _re.IGNORECASE,
                )
                if hints:
                    for h in hints[:5]:
                        h = h.strip().lstrip("-").strip()
                        if h:
                            lines.append(f"- {h[:200]}")
                else:
                    lines.append("_No explicit next-step recommendations parsed._")
        else:
            lines.append("_No reviewer report available yet._")
        lines.append("")

        # ── Citation verification ────────────────────────────────
        lines.append("## Citation verification")
        bib_path = self.latex_dir / "references.bib"
        if bib_path.exists():
            bib_text = bib_path.read_text(errors="replace")
            unverified = []
            for m in _re.finditer(
                r"%\s*\[NEEDS-CHECK[^\]]*\][^\n]*\n+@\w+\s*\{\s*([^,\s]+)",
                bib_text, _re.IGNORECASE,
            ):
                unverified.append(m.group(1).strip())
            total_entries = len(_re.findall(r"^@\w+\s*\{", bib_text, _re.MULTILINE))
            verified_count = total_entries - len(unverified)
            lines.append(
                f"- Verified: **{verified_count}** / "
                f"unverified (`NEEDS-CHECK`): **{len(unverified)}** "
                f"out of {total_entries} entries."
            )
            if unverified:
                lines.append("")
                lines.append("Entries flagged for manual review:")
                for k in unverified[:30]:
                    lines.append(f"  - `{k}`")
                if len(unverified) > 30:
                    lines.append(f"  - …and {len(unverified) - 30} more.")
        else:
            lines.append("_references.bib not present._")

        return "\n".join(lines)

    def _send_project_summary_via_telegram(
        self, score: float, prev_score: float,
    ) -> None:
        """Render the project summary markdown to PDF and send via Telegram.

        Uses the same _render_review_to_pdf helper as the review report
        so the output styling matches. On render failure, falls back to
        sending the .md file as a text document — the user still gets
        the content, just without typesetting.
        """
        if not self.telegram.is_configured:
            return
        try:
            md = self._generate_project_summary_md(score, prev_score)
        except Exception as e:
            self.log(f"_generate_project_summary_md raised: {e}", "WARN")
            return
        if not md.strip():
            return

        # Persist the .md so it ends up in the project's git history
        # alongside the iteration's other artefacts.
        out_md = self.state_dir / f"summary_iter{self.iteration}.md"
        out_pdf = self.state_dir / f"summary_iter{self.iteration}.pdf"
        try:
            out_md.write_text(md)
        except Exception as e:
            self.log(f"summary md write failed: {e}", "WARN")
        caption = (
            f"📋 {self.display_name} — iter {self.iteration} project summary "
            f"(score {score:.2f}/10)"
        )
        if self._render_review_to_pdf(md, out_pdf):
            ok = self.telegram.send_document(out_pdf, caption=caption)
            if ok:
                self.log(f"Project summary PDF sent: {out_pdf}", "INFO")
            return
        # Fallback: send .md as text document.
        if out_md.exists():
            self.telegram.send_document(
                out_md, caption=caption,
                require_pdf=False, min_size=64,
            )

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

        # Build compact message with the unified header so multi-project
        # users can tell which project the score belongs to.
        lines = [
            self.tg_header("🚤"),
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
            # Note: the standalone reviewer report PDF is intentionally
            # not sent. The reviewer's findings are now folded into the
            # project summary below as a "Reviewer feedback" section, so
            # the user receives one consolidated summary instead of two
            # overlapping documents.
            try:
                self._send_project_summary_via_telegram(
                    score=_score, prev_score=prev_score,
                )
            except Exception as e:
                self.log(f"Project summary send failed: {e}", "WARN")

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
        _atomic_write_yaml(self.checkpoint_file, checkpoint, default_flow_style=False)
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
        _atomic_write_yaml(self.checkpoint_file, checkpoint, default_flow_style=False)
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

    def save_checkpoint(self):
        """Save current iteration and score to a checkpoint file."""
        checkpoint = {
            "iteration": self.iteration,
            "last_score": self._last_score,
            "timestamp": datetime.now().isoformat(),
            "run_id": self.run_id,
        }
        self.state.save_checkpoint(checkpoint)

    def load_checkpoint(self) -> dict:
        return self.state.load_checkpoint()

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
            if self.paper_state_file.exists():
                state = self.load_paper_state()
                reviews = state.get("reviews", [])
                if reviews:
                    last_iter = max((r.get("iteration", 0) for r in reviews), default=0)
                    if last_iter > 0:
                        self.iteration = last_iter
                        self.log(f"Resumed from paper_state: iteration={self.iteration}", "INFO")
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
            # HITL: surface this step as live activity, drain control commands,
            # and park here if the user paused (step boundaries are safe points).
            try:
                self._set_activity(f"Step {step_num}/{total_steps}: {name}")
                self._poll_control()
                self._maybe_park()
            except Exception:
                pass
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

    # ========== Memory ==========

    def record_score_to_memory(self, score: float):
        """Record an iteration's review score in long-term memory.

        ``PipelineMixin.run_paper_iteration`` calls this at iteration end.
        The pre-refactor orchestrator.py defined it directly; the package
        refactor inadvertently dropped it, so add it back here.
        """
        self.memory.record_score(score)
        self._last_score = score
        self.log(f"Memory: recorded score {score}/10", "MEMORY")

    def get_memory_context(self) -> str:
        """Return the current Memory context block for prompt injection."""
        return self.memory.get_context()

    def get_current_phase(self) -> str:
        """Return the current research phase id (or empty string).

        Reads research_state.yaml via StateManager. Pipeline.py uses this
        to decide whether to enter a phase-specific code path.
        """
        state = self.state.load_state()
        for phase_id, phase in (state.get("phases") or {}).items():
            if isinstance(phase, dict) and phase.get("status") == "in_progress":
                return phase_id
        return ""

    # ========== State I/O ==========

    def load_state(self) -> dict:
        return self.state.load_state()

    def save_state(self, state: dict):
        self.state.save_state(state)

    def _load_action_plan(self) -> dict:
        return self.state.load_action_plan()

    def _save_action_plan(self, action_plan: dict):
        self.state.save_action_plan(action_plan)

    def load_paper_state(self) -> dict:
        return self.state.load_paper_state()

    def save_paper_state(self, state: dict):
        self.state.save_paper_state(state)
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
        return self.state.load_paper_requirements()

    def _load_findings_summary(self) -> str:
        return self.state.load_findings_summary()

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
                    _atomic_write_text(self.action_plan_file, fixed)
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
        _atomic_write_yaml(
            self.action_plan_file, action_plan,
            default_flow_style=False, allow_unicode=True,
        )

    def _load_findings_summary(self) -> str:
        """Load findings.yaml summary.

        Tolerates malformed YAML by degrading to a note rather than
        crashing the plan phase. A previous experimenter run may have
        emitted ``library_use:`` mid-list (see experimenter.prompt
        layout guidance); that file should still not prevent downstream
        agents from running — planner can proceed from the review alone.
        """
        if self.findings_file.exists():
            try:
                with open(self.findings_file) as f:
                    text = f.read()
                findings = yaml.safe_load(text) or {}
                return yaml.dump(findings, allow_unicode=True)[:500]
            except yaml.YAMLError as e:
                # Try the same auto-repair as state.load_findings_summary
                # so this read path doesn't lose evidence to a known bug.
                try:
                    from ark.findings_schema import attempt_repair
                    repaired, changes = attempt_repair(text)
                except Exception:
                    repaired, changes = None, []
                if repaired is not None:
                    try:
                        backup = self.findings_file.with_suffix(".yaml.malformed")
                        _atomic_write_text(backup, text)
                        _atomic_write_text(self.findings_file, repaired)
                        findings = yaml.safe_load(repaired) or {}
                        detail = "; ".join(changes) if changes else "auto-repaired"
                        self.log(
                            f"findings.yaml had a parse error and was "
                            f"auto-repaired ({detail}); original saved to "
                            f"{backup.name}",
                            "INFO",
                        )
                        return yaml.dump(findings, allow_unicode=True)[:500]
                    except Exception as repair_err:
                        self.log(
                            f"findings.yaml auto-repair failed at write step: "
                            f"{repair_err}",
                            "WARN",
                        )
                detail = "; ".join(changes) if changes else "no known repair pattern matched"
                self.log(
                    f"findings.yaml is malformed ({type(e).__name__}); "
                    f"planner will proceed without it. Fix the file to "
                    f"restore evidence-aware planning. Repair attempt: {detail}.",
                    "WARN",
                )
                return f"[findings.yaml unparseable: {e.__class__.__name__}]"
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
        """Ensure code_dir is a git repo with (optionally) a GitHub remote. Idempotent.

        Detection uses the literal ``<code_dir>/.git`` marker rather than
        ``git rev-parse --git-dir``, because the latter walks up the filesystem
        and returns success if any ancestor is a repo. Webapp projects live at
        ``<ARK root>/.ark/data/projects/<user>/<id>/`` — an ancestor of that
        path is the ARK source repo, so the walk-up check silently skipped
        init and left every downstream ``git diff`` / ``git commit`` broken.

        GitHub-remote creation is gated on ``auto_github_remote`` (default
        True to preserve existing CLI behaviour). Webapp projects set this to
        False so every new project doesn't silently become a private repo
        under the host user's gh account.
        """
        code_dir = self.code_dir
        # Already a git repo rooted at code_dir? (NOT an ancestor repo)
        if not (Path(code_dir) / ".git").exists():
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

        if not self.config.get("auto_github_remote", True):
            return

        # Check for remote
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=code_dir, timeout=10,
        )
        if remote.returncode != 0:
            # Prefer a user-supplied PAT (webapp path). The token is read from
            # the orchestrator env only — never placed in argv, the remote URL,
            # or .git/config.
            pat = os.environ.get("ARK_GITHUB_PAT")
            if pat:
                self._setup_github_remote_pat(pat)
                return
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

    def _github_api(self, method, path, pat, body=None):
        """Minimal GitHub REST helper using urllib (token in header, not argv).

        Returns (status_code, parsed_json_or_None). Never raises for HTTP
        errors; network/parse errors return (None, None).
        """
        url = "https://api.github.com" + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {pat}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "ARK")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        # Conda envs often lack a system CA bundle, so urllib's default SSL
        # context fails to verify api.github.com (curl works via the OS bundle).
        # Use certifi's bundle. Never disable verification — this carries a token.
        try:
            import ssl, certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = None
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8")
                parsed = json.loads(raw) if raw else None
            except Exception:
                parsed = None
            return e.code, parsed
        except Exception:
            return None, None

    def _setup_github_remote_pat(self, pat):
        """Create (or reuse) a private GitHub repo via PAT and add origin.

        The remote URL contains NO token; the credential is supplied at
        push time via an env-backed inline credential helper.
        """
        repo = "ark-" + re.sub(r"[^A-Za-z0-9._-]", "-", self.project_name)

        # Owner = the configured org if set (ARK_GITHUB_ORG), else the
        # authenticated user. Org repos are created under /orgs/<org>/repos.
        org = (os.environ.get("ARK_GITHUB_ORG") or "").strip()
        if org:
            owner = org
            create_path = f"/orgs/{org}/repos"
        else:
            status, me = self._github_api("GET", "/user", pat)
            owner = (me or {}).get("login") if me else None
            if not owner:
                self.log("Git: GitHub PAT auth failed; skipping remote setup", "WARN")
                return
            create_path = "/user/repos"

        # Create the private repo. 422 == already exists -> treat as success.
        status, _ = self._github_api(
            "POST", create_path, pat,
            body={"name": repo, "private": True, "auto_init": False},
        )
        if status not in (200, 201, 422):
            self.log(f"Git: GitHub repo create under '{owner}' returned HTTP {status}; skipping remote", "WARN")
            return

        # Set remote WITHOUT any token in the URL.
        add = subprocess.run(
            ["git", "remote", "add", "origin",
             f"https://github.com/{owner}/{repo}.git"],
            cwd=self.code_dir, capture_output=True, text=True, timeout=10,
        )
        if add.returncode != 0 and "already exists" not in (add.stderr or ""):
            self.log(f"Git: failed to add remote: {add.stderr[:200]}", "WARN")
            return
        self.log(f"Git: GitHub remote ready -> {owner}/{repo} (private)", "INFO")

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
                latex_dir_name = self.config.get("latex_dir", "paper")
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
                # Gate the outward push (an autonomous, outward-facing action).
                # Denied → keep the commit local and skip publishing.
                _mgr = getattr(self, "_intervention", None)
                if _mgr is not None and not _mgr.check_action(
                        "git_push", remote="origin", branch="HEAD"):
                    self.log("Git: push blocked by intervention policy (commit kept local)", "WARN")
                    return True
                # Auto push. When a PAT is present, supply the credential at
                # push time via an inline credential helper that reads the token
                # from the env — the token never lands in argv or .git/config.
                pat = os.environ.get("ARK_GITHUB_PAT")
                if pat:
                    helper = '!f() { echo username=x-access-token; echo "password=$ARK_GITHUB_PAT"; }; f'
                    push = subprocess.run(
                        ["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}",
                         "push", "-u", "origin", "HEAD"],
                        capture_output=True, text=True, cwd=self.code_dir, timeout=120,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                else:
                    push = subprocess.run(
                        ["git", "push", "-u", "origin", "HEAD"],
                        capture_output=True, text=True, cwd=self.code_dir, timeout=60,
                    )
                if push.returncode == 0:
                    self.log("Git: pushed to GitHub", "INFO")
                else:
                    err = push.stderr or ""
                    if pat:
                        err = err.replace(pat, "***")
                    self.log(f"Git: push failed: {err[:200]}", "WARN")
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

        import html as _html
        header = self.tg_header("🚤")
        lines = [
            header,
            f"<i>🚀 starting paper mode</i>",
            f"━━━━━━━━━━━━━━━━━━━━━",
            _html.escape(resume_info),
        ]
        if score_info:
            lines.append(_html.escape(score_info.rstrip()))
        lines.append(
            f"Target: {self.paper_accept_threshold}/10  |  Max {self.max_iterations} iter"
        )
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━")

        self.telegram.send_raw("\n".join(lines), parse_mode="HTML")

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

        First line is the unified ``🚤 ARK Project-<id5> | <title>`` header,
        so the user can always tell which project a message belongs to. The
        meta line underneath carries mode + iteration counter. Pulls from
        already-cached state (no new I/O on the hot path). Output is
        Telegram HTML.
        """
        mode = self.mode or "?"
        max_iter = self.max_iterations or 0
        line1 = self.tg_header("🚤")
        meta = f"<i>{mode} · iter {self.iteration}/{max_iter}</i>"

        score_line = ""
        trend_line = ""
        stag_line = ""
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

        lines = [line1, meta]
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
        # Always post to the in-app conversation thread (chat bubbles), even
        # when Telegram isn't configured.
        self._chat("agent", f"{stage}{(': ' + detail) if detail else ''}",
                   kind="notice" if level in ("info", "working") else "milestone")
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

    def _intervention_ask(self, question: str, options: list, timeout_s: int):
        """Adapter: relay an intervention ApprovalRequest through the existing
        ``ask_user_decision`` Telegram flow.

        ``options`` is a list of ``{id, title, consequence}``. Returns
        ``{"option_id", "text"}``, or ``None`` on timeout so the gate applies its
        safe default (deny). The safe default index is the ``deny`` option, so a
        timed-out decision never silently approves a risky action.
        """
        titles = [o.get("title") or o.get("id") for o in options]
        details = [o.get("consequence", "") for o in options]
        deny_idx = next((i for i, o in enumerate(options) if o.get("id") == "deny"), 0)
        idx, text = self.ask_user_decision(
            question, titles, timeout=timeout_s, default=deny_idx,
            option_details=details, phase="intervention", polish=False,
            kind="guardrail")
        text = (text or "").strip()
        if text == "" and idx == deny_idx:
            return None  # timeout signature → let the gate deny
        if 0 <= idx < len(options):
            return {"option_id": options[idx].get("id"), "text": text}
        # free-text / custom slot → interpret an explicit yes, else deny
        if text.lower() in ("y", "yes", "ok", "approve", "allow", "go"):
            return {"option_id": "approve", "text": text}
        return {"option_id": "deny", "text": text}

    def _intervention_notify(self, text: str):
        """Relay a gate notification (e.g. an auto-allowed low-risk action, or a
        blocked one) to the webapp chat thread, and Telegram when configured."""
        try:
            self._chat("agent", text, kind="notice")
        except Exception:
            pass
        if self.telegram.is_configured:
            try:
                self.telegram.send_raw(text)
            except Exception:
                pass

    def _intervention_ask_secret(self, name: str, purpose: str, timeout_s: int):
        """Ask the human for a secret VALUE over Telegram; the reply text IS the
        value. Returns the value, or None on deny/timeout. The value is not
        logged here (the gate redacts it downstream)."""
        if not self.telegram.is_configured:
            return None
        why = f" — purpose: {purpose}" if purpose else ""
        question = (f"🔑 An agent needs a secret value for <b>{name}</b>{why}.\n"
                    f"Reply with the value to supply it, or 'deny' to refuse.")
        reply = self.telegram.ask(question, timeout=timeout_s)
        if not reply:
            return None
        reply = reply.strip()
        if reply.lower() in ("deny", "no", "n", "refuse", "cancel"):
            return None
        return reply or None

    def ask_user_decision(self, question: str, options: list = None,
                          timeout: int = 600, default: int = 0,
                          *, what_happened: str = "",
                          background: list = None,
                          option_details: list = None,
                          phase: str = "",
                          polish: bool = True,
                          kind: str = "decision",
                          timeout_action: str = "proceed_default") -> tuple:
        """Ask the human a multiple-choice decision — DUAL-CHANNEL (webapp + Telegram).

        Publishes a ``pending_decision`` row to the shared DB (so the webapp can
        show + answer it) AND, if configured, sends a Telegram menu. Blocks until
        EITHER channel answers, or the timeout fires.

        ``kind`` tags the decision (decision | clarification | gate_a |
        experiment_approval | drift | blocker | irreversible) for the UI and for
        autonomy gating. ``timeout_action``: ``proceed_default`` auto-picks the
        default on timeout; ``pause`` parks the run and keeps waiting (use for
        ethics / expensive / irreversible decisions).

        A "Custom" escape option is always appended. Returns (idx, reply_text):
        a numeric answer → that index; free text → (len(opts)-1, text).
        """
        has_telegram = self.telegram.is_configured
        has_db = bool(self._db_path and self._project_id)
        if not has_telegram and not has_db:
            self.log(f"No HITL channel available, using default option {default}", "WARN")
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

        # ── Publish the decision to the DB so the webapp can see + answer it ──
        _db = self._hitl_db()
        decision_id = None
        if has_db and _db:
            try:
                deadline = datetime.utcnow() + timedelta(seconds=timeout)
                with _db.get_session(self._db_path) as s:
                    decision_id = _db.create_pending_decision(
                        s, self._project_id,
                        question or what_happened or "Decision needed",
                        opts, kind=kind, context=what_happened or "",
                        default_index=default, timeout_action=timeout_action,
                        deadline_at=deadline)
                self._set_control_state("awaiting")
                # Post the question as an agent bubble in the chat thread.
                self._chat("agent", question or what_happened or "Decision needed",
                           kind="decision",
                           meta={"options": opts, "decision_id": decision_id,
                                 "default_index": default, "context": what_happened or "",
                                 "kind": kind})
            except Exception as e:
                self.log(f"Could not publish decision to DB: {e}", "WARN")

        # ── Second channel: Telegram (if configured) ──
        if has_telegram:
            self.log(f"Awaiting decision (Telegram + webapp, timeout {timeout}s)…", "INFO")
            self.telegram.send(polished, parse_mode="HTML")
            self.telegram._is_waiting = True
            self.telegram._ask_reply = None
            self.telegram._ask_event.clear()
        else:
            self.log(f"Awaiting decision (webapp, timeout {timeout}s)…", "INFO")

        # ── Dual-channel wait loop: whichever channel answers first wins ──
        start = time.time()
        paused_for_decision = False
        pause_deadline = None
        result = None
        try:
            while result is None:
                # 1) webapp / DB answer
                if decision_id and _db:
                    try:
                        with _db.get_session(self._db_path) as s:
                            dec = _db.get_decision(s, decision_id)
                    except Exception:
                        dec = None
                    if dec is not None and dec.status == "answered":
                        if dec.answer_text and (dec.answer_index is None or dec.answer_index < 0):
                            try: self.inject_user_update(dec.answer_text)
                            except Exception: pass
                            result = (len(opts) - 1, dec.answer_text)
                        else:
                            ridx = dec.answer_index if (dec.answer_index is not None and 0 <= dec.answer_index < len(opts)) else default
                            result = (ridx, dec.answer_text or "")
                        break
                    if dec is not None and dec.status == "cancelled":
                        result = (default, ""); break
                # 2) Telegram reply
                if has_telegram and self.telegram._ask_event.wait(1.5):
                    reply = self.telegram._ask_reply
                    self.telegram._ask_event.clear()
                    self.telegram._ask_reply = None
                    if reply:
                        idx, is_text = self._parse_decision_reply(reply, opts)
                        if decision_id and _db:
                            try:
                                with _db.get_session(self._db_path) as s:
                                    _db.answer_decision(s, decision_id,
                                        index=(idx if not is_text else -1),
                                        text=(reply if is_text else ""),
                                        by="telegram", source="telegram")
                            except Exception: pass
                        self.telegram.send_raw("✅ Received, continuing...")
                        # user bubble for the Telegram answer (webapp answers get
                        # their bubble from the route)
                        self._chat("user", reply if is_text else f"Option {idx + 1}: {opts[idx]}",
                                   kind="answer")
                        if is_text:
                            try: self.inject_user_update(reply)
                            except Exception: pass
                            result = (len(opts) - 1, reply)
                        else:
                            result = (idx, reply)
                        break
                elif not has_telegram:
                    time.sleep(1.0)
                # 3) control commands mid-wait (stop / steer)
                self._poll_control()
                if self._stop_requested:
                    result = (default, ""); break
                # 4) timeout
                if time.time() - start >= timeout:
                    if timeout_action == "pause":
                        # "pause" buys a sensitive decision (ethics / expensive /
                        # irreversible) one extra grace window + a louder ping —
                        # but it must NEVER block the run forever. After the grace
                        # it auto-continues with the default (which for these is
                        # the SAFE choice, e.g. Gate A → Reject).
                        if not paused_for_decision:
                            grace_min = max(timeout // 60, 1)
                            self.log("Decision timed out — pausing + notifying; "
                                     "auto-continues with the default after grace.", "WARN")
                            self._paused = True
                            self._set_control_state("paused")
                            notice = (f"No answer yet — paused. I'll auto-continue with the "
                                      f"default (option #{default + 1}) in ~{grace_min} min "
                                      f"unless you respond.")
                            self._chat("agent", "⏸ " + notice, kind="notice")
                            if has_telegram:
                                self.telegram.send_async(
                                    f"⏸ <b>{_html.escape(self.display_name)}</b>: {_html.escape(notice)}",
                                    parse_mode="HTML", polish=False)
                            paused_for_decision = True
                            pause_deadline = time.time() + timeout
                        if datetime.now() >= self.max_end_time or (
                                pause_deadline and time.time() >= pause_deadline):
                            self._paused = False
                            self._set_control_state("")
                            if decision_id and _db:
                                try:
                                    with _db.get_session(self._db_path) as s:
                                        _db.expire_decision(s, decision_id)
                                except Exception: pass
                            default_label = opts[default] if opts else "N/A"
                            self.log(f"Pause grace elapsed — auto-continuing with default: {default_label}", "WARN")
                            if has_telegram:
                                self.telegram.send_async(
                                    f"⏰ <b>{_html.escape(self.display_name)}</b>: still no answer — "
                                    f"continuing with option <b>#{default + 1}</b>: {_html.escape(default_label)}",
                                    parse_mode="HTML", polish=False)
                            result = (default, ""); break
                        time.sleep(2); continue
                    else:
                        if decision_id and _db:
                            try:
                                with _db.get_session(self._db_path) as s:
                                    _db.expire_decision(s, decision_id)
                            except Exception: pass
                        default_label = opts[default] if opts else "N/A"
                        self.log(f"Decision timed out, using default: {default_label}", "WARN")
                        if has_telegram:
                            self.telegram.send_async(
                                f"⏰ <b>{_html.escape(self.display_name)}</b>: timeout — "
                                f"auto-selected option <b>#{default + 1}</b>: "
                                f"{_html.escape(default_label)}",
                                parse_mode="HTML", polish=False)
                        result = (default, ""); break
        finally:
            if has_telegram:
                self.telegram._is_waiting = False
            # An answer (or default) closes the gate — clear awaiting/pause.
            if result is not None:
                self._paused = False
                self._set_control_state("")

        return result if result is not None else (default, "")

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
    # `--mode` accepted for backward compatibility with existing slurm
    # scripts that pass `--mode paper`; there is only one mode now.
    parser.add_argument("--mode", type=str, default="paper", choices=["paper"],
                        help=argparse.SUPPRESS)
    parser.add_argument("--project", type=str, required=True, help="Project name (e.g., prouter)")
    parser.add_argument("--model", type=str, default=None,
                        help="LiteLLM model string, e.g. anthropic/claude-sonnet-4-6, "
                             "gemini/gemini-2.5-flash, openai/gpt-5 (overrides config.yaml `model`)")
    parser.add_argument("--model-variant", type=str, default=None,
                        help="Specific model id (e.g. claude-sonnet-4-6, "
                             "gemini-3.1-pro-preview, gemini-3-flash-preview, "
                             "gemini-3.1-flash-lite-preview, gemini-2.5-pro, auto)")
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
    parser.add_argument("--no-research", action="store_true", default=False,
                        help="Skip Gemini Deep Research")
    parser.add_argument("--apply-instruction", type=str, default=None,
                        help="Apply ONE targeted change (no full iteration) and exit.")
    parser.add_argument("--apply-scope", type=str, default="edit",
                        choices=["edit", "experiment", "answer"],
                        help="Granularity for --apply-instruction (answer = read-only Q&A).")
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
        try:
            from website.dashboard.db import resolve_db_path
            db_path = resolve_db_path()
        except ImportError:
            pass  # webapp deps not available (e.g. running on remote VM)

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
        model_variant=args.model_variant,
        code_dir=code_dir,
        project_dir=project_dir,
        db_path=db_path,
        project_id=project_id,
    )
    if args.no_research:
        orchestrator.config["skip_deep_research"] = True

    # Mark as running in DB
    if db_path and project_id:
        orchestrator._sync_db(status="running", pid=os.getpid())

    # Lightweight apply path: one targeted change, then back to done — no loop.
    if args.apply_instruction:
        try:
            orchestrator.apply_instruction(args.apply_instruction, args.apply_scope)
        except Exception as e:
            orchestrator.log(f"apply_instruction error: {e}", "ERROR")
        finally:
            if db_path and project_id:
                orchestrator._sync_db(status="done", pid=0)
        return

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
