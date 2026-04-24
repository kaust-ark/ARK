"""Persistent Telegram daemon that runs independently of orchestrator processes.

Polls getUpdates, routes messages to running orchestrators via mailbox,
and handles messages for stopped projects (status, PDF requests).

Lifecycle:
  - Started by `ark run` or `ark new` (if Telegram is configured)
  - Survives `ark stop` (keeps responding to Telegram messages)
  - Stopped only by `ark delete` (when no registered projects remain)

PID file: ~/.ark/telegram_daemon.pid
Log file: ~/.ark/telegram_daemon.log
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

from ark.paths import get_config_dir
from ark.telegram.client import TelegramConfig


# ══════════════════════════════════════════════════════════════
#  Daemon
# ══════════════════════════════════════════════════════════════

class TelegramDaemon:
    """Standalone Telegram polling daemon with multi-project routing."""

    def __init__(self):
        self._ark_dir = get_config_dir()
        self._pid_file = self._ark_dir / "telegram_daemon.pid"
        self._state_file = self._ark_dir / "telegram_state.yaml"
        self._lock_file = self._ark_dir / "telegram.lock"
        self._state_lock_file = self._ark_dir / ".telegram_state.lock"
        self._mailbox_dir = self._ark_dir / "telegram_mailbox"
        self._config = TelegramConfig()
        self._stop_event = threading.Event()
        self._lock_fd = None
        self._offset = 0

    # ── Main Loop ─────────────────────────────────────────

    def run(self):
        """Main entry point: acquire lock, poll forever."""
        # Ensure we can call claude CLI without "nested session" error
        os.environ.pop("CLAUDECODE", None)

        if not self._config.is_configured:
            self._log("Telegram not configured, exiting.")
            return

        self._ark_dir.mkdir(parents=True, exist_ok=True)
        self._mailbox_dir.mkdir(parents=True, exist_ok=True)

        # Handle SIGTERM gracefully
        signal.signal(signal.SIGTERM, self._handle_sigterm)

        self._write_pid()
        self._acquire_lock()

        self._log("Daemon started.")

        # Get baseline offset (skip old messages)
        try:
            resp = self._api_call("getUpdates", limit=1, offset=-1)
            if resp and resp.get("result"):
                self._offset = resp["result"][-1]["update_id"] + 1
        except Exception:
            pass

        try:
            while not self._stop_event.is_set():
                try:
                    resp = self._api_call("getUpdates", offset=self._offset, timeout=15, limit=10)
                    if resp and resp.get("result"):
                        for update in resp["result"]:
                            self._offset = update["update_id"] + 1
                            msg = update.get("message", {})
                            text = msg.get("text", "")
                            if text:
                                self._route_message(text, update["update_id"])
                except Exception as e:
                    self._log(f"Poll error: {e}")
                    self._stop_event.wait(timeout=5)
                    continue

                self._stop_event.wait(timeout=1)
        finally:
            self._release_lock()
            self._remove_pid()
            self._log("Daemon stopped.")

    def _handle_sigterm(self, signum, frame):
        self._log("Received SIGTERM, shutting down...")
        self._stop_event.set()

    # ── Message Routing ───────────────────────────────────

    # ── Model Preferences ─────────────────────────────────

    MODEL_ALIASES = {
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-7",
        "haiku": "claude-haiku-4-5-20251001",
    }

    def _get_model(self, kind: str) -> str:
        """Get model ID for 'bot' or 'ark'. Falls back to sonnet."""
        state = self._load_state()
        models = state.get("models", {})
        return models.get(kind, "claude-sonnet-4-6")

    def _set_model(self, kind: str, model_id: str):
        """Set model ID for 'bot' or 'ark'."""
        state = self._load_state()
        models = state.setdefault("models", {})
        models[kind] = model_id
        self._save_state(state)

    def _resolve_model(self, name: str) -> str | None:
        """Resolve alias or full model ID. Returns None if invalid."""
        lower = name.lower().strip()
        if lower in self.MODEL_ALIASES:
            return self.MODEL_ALIASES[lower]
        # Accept full model IDs (claude-*)
        if lower.startswith("claude-"):
            return lower
        return None

    # ── Bind Commands ─────────────────────────────────────

    def _handle_bind_command(self, text: str) -> bool:
        """Handle ``/bind <name>``, ``/release``, ``/bound`` commands.

        Returns True if the message was a bind command (and therefore
        should not be routed to any project).

        The bound-project model pins every non-prefixed user message to a
        single project so the user doesn't have to prefix their replies
        when multiple projects share the same bot. Explicit prefix routing
        (``safeclaw pdf``) still works even when a binding is active, but
        *only* for projects other than the bound one — they temporarily
        override the pin for that one message.
        """
        stripped = text.strip()
        lower = stripped.lower()

        if lower == "/bound":
            state = self._load_state()
            bound = state.get("bound_project")
            if bound:
                active = state.get("active_projects", {})
                status = "🟢 running" if bound in active else "⏸ stopped"
                self._send(
                    f"🔗 Bound to <b>{bound}</b> ({status}).\n"
                    f"Use <code>/release</code> to unpin, or "
                    f"<code>/bind &lt;name&gt;</code> to switch.",
                    parse_mode="HTML",
                )
            else:
                self._send(
                    "No project is bound. Messages route by heuristic "
                    "(prefix → single project → last sender).\n"
                    "Pin one with <code>/bind &lt;name&gt;</code>.",
                    parse_mode="HTML",
                )
            return True

        if lower == "/release":
            state = self._load_state()
            prev = state.get("bound_project")
            state["bound_project"] = None
            self._save_state(state)
            if prev:
                self._send(
                    f"🔓 Released <b>{prev}</b>. Routing falls back to "
                    f"heuristics.",
                    parse_mode="HTML",
                )
            else:
                self._send("No project was bound.")
            return True

        if lower.startswith("/bind"):
            parts = stripped.split(maxsplit=1)
            state = self._load_state()
            registered = state.get("registered_projects", [])
            if len(parts) < 2:
                names = ", ".join(f"<code>{p}</code>" for p in sorted(registered))
                self._send(
                    f"Usage: <code>/bind &lt;name&gt;</code>\n"
                    f"Registered: {names or '(none)'}",
                    parse_mode="HTML",
                )
                return True
            target = parts[1].strip()
            if target not in registered:
                self._send(
                    f"Project <b>{target}</b> is not registered. "
                    f"Run <code>ark run {target}</code> first.",
                    parse_mode="HTML",
                )
                return True
            state["bound_project"] = target
            self._save_state(state)
            active = state.get("active_projects", {})
            status = "🟢 running" if target in active else "⏸ stopped"
            self._send(
                f"🔗 Bound to <b>{target}</b> ({status}). Every message "
                f"now routes here until <code>/release</code>.",
                parse_mode="HTML",
            )
            return True

        return False

    def _handle_model_command(self, text: str) -> bool:
        """Handle /model commands. Returns True if handled."""
        text_lower = text.strip().lower()
        if not text_lower.startswith("/model"):
            return False

        parts = text.strip().split()

        # /model — show current models
        if len(parts) == 1:
            bot_model = self._get_model("bot")
            ark_model = self._get_model("ark")
            # Reverse lookup for display
            def short(mid):
                for alias, full in self.MODEL_ALIASES.items():
                    if full == mid:
                        return f"{alias} ({mid})"
                return mid
            self._send(
                f"<b>Bot model:</b> {short(bot_model)}\n<b>ARK model:</b> {short(ark_model)}\n\n"
                f"Usage:\n<code>/model bot sonnet</code>\n<code>/model ark opus</code>\n"
                f"Options: sonnet, opus, haiku",
                parse_mode="HTML",
            )
            return True

        # /model bot <name> or /model ark <name>
        if len(parts) == 3:
            kind = parts[1].lower()
            if kind not in ("bot", "ark"):
                self._send("Usage: <code>/model bot|ark sonnet|opus|haiku</code>", parse_mode="HTML")
                return True
            resolved = self._resolve_model(parts[2])
            if not resolved:
                self._send(f"Unknown model: <code>{parts[2]}</code>. Options: sonnet, opus, haiku", parse_mode="HTML")
                return True
            self._set_model(kind, resolved)
            def short(mid):
                for alias, full in self.MODEL_ALIASES.items():
                    if full == mid:
                        return alias
                return mid
            self._send(f"<b>{kind.upper()}</b> model set to <b>{short(resolved)}</b>", parse_mode="HTML")
            return True

        self._send("Usage: <code>/model bot|ark sonnet|opus|haiku</code>", parse_mode="HTML")
        return True

    # ── Message Routing ───────────────────────────────────

    def _route_message(self, text: str, update_id: int):
        """Route message to running project (mailbox) or handle stopped project."""
        # Handle global commands before project routing
        if self._handle_model_command(text):
            return
        if self._handle_bind_command(text):
            return

        state = self._load_state()
        active = state.get("active_projects", {})
        registered = state.get("registered_projects", [])

        # Prune dead processes from active list
        self._prune_dead(state)
        active = state.get("active_projects", {})

        # Combined pool: active + registered (for prefix matching)
        all_projects = list(set(list(active.keys()) + registered))

        if not all_projects:
            self._send("No projects registered. Use <code>ark new</code> to create one.", parse_mode="HTML")
            return

        text_stripped = text.strip()
        text_lower = text_stripped.lower()

        # Rule 1: Explicit prefix — "safeclaw pdf" → safeclaw.
        # Highest priority even when a bind is active, so the user can
        # temporarily poke another project (e.g. ask for its PDF) without
        # releasing the binding.
        target = None
        remainder = text_stripped
        for project_name in all_projects:
            prefix = project_name.lower()
            if text_lower.startswith(prefix) and (
                len(text_lower) == len(prefix) or
                text_lower[len(prefix)] in (' ', ':', ',', '\n')
            ):
                remainder = text_stripped[len(prefix):].lstrip(' :,') or text_stripped
                target = project_name
                break

        if not target:
            # Rule 2: Bound project — pinned via /bind, overrides heuristics.
            bound = state.get("bound_project")
            if bound and bound in all_projects:
                target = bound
            elif bound and bound not in all_projects:
                # Stale binding (project deregistered). Clear it silently.
                state["bound_project"] = None
                self._save_state(state)

        if not target:
            # Rule 3: Waiting project (orchestrator blocked on a decision)
            waiting = state.get("waiting_project")
            if waiting and waiting in active:
                target = waiting

        if not target:
            # Rule 4: Single project
            if len(all_projects) == 1:
                target = all_projects[0]

        if not target:
            # Rule 5: Last sender
            last = state.get("last_sender")
            if last and last in all_projects:
                target = last

        if not target:
            # Rule 6: Ambiguous
            statuses = []
            for p in sorted(all_projects):
                s = "🟢 running" if p in active else "⏸ stopped"
                statuses.append(f"  <b>{p}</b> — {s}")
            self._send(
                "Which project?\n" + "\n".join(statuses)
                + "\n\nPrefix your message with the project name, or "
                "<code>/bind &lt;name&gt;</code> to pin one.",
                parse_mode="HTML",
            )
            return

        # Update last_sender
        state["last_sender"] = target
        self._save_state(state)

        # Dispatch
        if target in active:
            # Running → deliver to mailbox (orchestrator picks it up)
            self._deliver_to_mailbox(target, remainder, update_id)
        else:
            # Stopped → handle directly
            self._handle_stopped_project(target, remainder)

    def _handle_stopped_project(self, project: str, text: str):
        """Handle all messages for a stopped project via Claude agent."""
        threading.Thread(
            target=self._agent_respond, args=(project, text), daemon=True
        ).start()

    def _load_project_tg_history(self, project: str, max_entries: int = 10) -> list:
        """Load last N entries from tg_history.jsonl for a stopped project."""
        config = self._load_project_config(project)
        if not config:
            return []
        code_dir = config.get("code_dir", "")
        if not code_dir:
            return []
        history_file = Path(code_dir) / "auto_research" / "state" / "tg_history.jsonl"
        if not history_file.exists():
            return []
        try:
            lines = history_file.read_text().splitlines()
            entries = []
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
            return entries[-max_entries:]
        except Exception:
            return []

    def _format_tg_history(self, history: list) -> str:
        """Format chat history entries for prompt."""
        if not history:
            return ""
        lines = []
        for msg in history:
            prefix = "User" if msg.get("role") == "user" else "You"
            lines.append(f"[{msg.get('date', '')} {msg.get('ts', '')}] {prefix}: {msg.get('text', '')}")
        return "\n".join(lines)

    def _agent_respond(self, project: str, text: str):
        """Call Claude to generate an intelligent response for a stopped project."""
        from ark.telegram.client import TelegramDispatcher

        # Gather project context and history
        context = self._gather_project_context(project)
        lang = self._get_language_pref(project)
        lang_instruction = "Reply in Chinese." if lang == "zh" else "Reply in English."
        history = self._load_project_tg_history(project, max_entries=10)
        history_block = self._format_tg_history(history) or "(no prior conversation)"

        # Send typing indicator
        self._api_call("sendChatAction", chat_id=self._config.chat_id, action="typing")

        config = self._load_project_config(project) or {}
        title = config.get("title", project)
        venue = config.get("venue", "")

        identity = f'You are ARK Bot, the assistant for project "{project}"'
        if title and title != project:
            identity += f' ("{title}")'
        if venue:
            identity += f', targeting {venue}'
        identity += ". The project is currently STOPPED (not running)."

        system_prompt = f"""{identity}
{lang_instruction}

STYLE (critical):
- Talk like a person, NOT like a report. No section headers, no "**Project**:", no "Current status summary".
- For casual questions ("how's it going", "what's up"): 2-4 sentences max. Just the key point.
- Only use bullet points if there are 3+ genuinely distinct items. Never nest them.
- **bold** only for the single most important thing in a reply.
- No tables. No headers. No "---" dividers.
- Use standard Markdown: **bold**, *italic*, `code`. Keep it simple.
- Use the conversation history to understand follow-up questions.

If the user asks to start/resume the project, tell them to run `ark run {project}` from terminal.
If the user wants the PDF, add [SEND_PDF] on a new line at the end."""

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
                ["claude", "--print", "--model", self._get_model("bot"), "-p", prompt],
                capture_output=True, text=True, timeout=90, env=env,
            )
            response = result.stdout.strip()
            if not response:
                response = result.stderr.strip()[:500] or "Sorry, unable to respond right now."
        except subprocess.TimeoutExpired:
            response = "Sorry, response timed out."
        except FileNotFoundError:
            # claude CLI not available — fallback to basic response
            response = f"⏸ *{project}* is stopped. Use `ark run {project}` to resume."
        except Exception as e:
            response = f"Error: {e}"

        # Extract [SEND_PDF]
        send_pdf = False
        if "[SEND_PDF]" in response:
            send_pdf = True
            response = response.replace("[SEND_PDF]", "").strip()

        if response:
            self._send(TelegramDispatcher.to_html(response), parse_mode="HTML")

        if send_pdf:
            self._send_last_pdf(project)

    def _gather_project_context(self, project: str) -> str:
        """Collect project state files for Claude context."""
        config = self._load_project_config(project)
        if not config:
            return f"Project: {project}\nStatus: stopped (config not found)"

        lines = [f"Project: {project}", "Status: stopped"]
        code_dir = config.get("code_dir", "")
        state_dir = Path(code_dir) / "auto_research" / "state" if code_dir else None

        if not state_dir or not state_dir.exists():
            return "\n".join(lines)

        # Paper state (score, iteration)
        paper_state_file = state_dir / "paper_state.yaml"
        if paper_state_file.exists():
            try:
                with open(paper_state_file) as f:
                    ps = yaml.safe_load(f) or {}
                iteration = ps.get("iteration", 0)
                scores = ps.get("scores", [])
                lines.append(f"Mode: paper | Iteration: {iteration}")
                if scores:
                    lines.append(f"Current score: {scores[-1]}/10")
                    lines.append(f"Score history: {scores[-8:]}")
            except Exception:
                pass

        # Goal
        goal = config.get("goal_anchor", "")
        if goal:
            lines.append(f"\nGoal:\n{goal[:600]}")

        # Latest review
        review_file = state_dir / "latest_review.md"
        if review_file.exists():
            try:
                lines.append(f"\nLatest Review (excerpt):\n{review_file.read_text()[:800]}")
            except Exception:
                pass

        # Action plan
        plan_file = state_dir / "action_plan.yaml"
        if plan_file.exists():
            try:
                lines.append(f"\nCurrent Action Plan:\n{plan_file.read_text()[:400]}")
            except Exception:
                pass

        # Cost
        cost_file = state_dir / "cost_report.yaml"
        if cost_file.exists():
            try:
                with open(cost_file) as f:
                    cost = yaml.safe_load(f) or {}
                total = cost.get("total_cost_usd", 0)
                if total:
                    lines.append(f"\nTotal cost: ${total:.2f}")
            except Exception:
                pass

        # Recent log (last 15 lines)
        log_dir = Path(code_dir) / "auto_research" / "logs"
        if log_dir.exists():
            try:
                log_files = sorted(log_dir.glob(f"{project}_*.log"))
                if log_files:
                    log_lines = log_files[-1].read_text().splitlines()[-15:]
                    lines.append(f"\nRecent Log:\n" + "\n".join(log_lines))
            except Exception:
                pass

        return "\n".join(lines)

    def _get_language_pref(self, project: str) -> str:
        """Get language preference for a project."""
        config = self._load_project_config(project)
        if not config:
            return "en"
        code_dir = config.get("code_dir", "")
        prefs_file = Path(code_dir) / "auto_research" / "state" / "user_prefs.yaml" if code_dir else None
        if prefs_file and prefs_file.exists():
            try:
                with open(prefs_file) as f:
                    return yaml.safe_load(f).get("language", "en")
            except Exception:
                pass
        return "en"

    def _send_last_pdf(self, project: str):
        """Find and send the most recent PDF for a stopped project."""
        config = self._load_project_config(project)
        if not config:
            self._send(f"Cannot find config for {project}")
            return

        code_dir = config.get("code_dir", "")
        latex_dir_rel = config.get("latex_dir", "Latex")
        latex_dir = Path(code_dir) / latex_dir_rel if code_dir else None
        pdf = latex_dir / "main.pdf" if latex_dir else None

        if pdf and pdf.exists() and pdf.stat().st_size > 1000:
            self._send_document(pdf, caption=f"📄 {project} (last compiled)")
        else:
            self._send(f"No compiled PDF available for <b>{project}</b>.", parse_mode="HTML")

    # ── Mailbox Delivery ──────────────────────────────────

    def _deliver_to_mailbox(self, project: str, text: str, update_id: int):
        """Write message to project's mailbox for the running orchestrator to pick up."""
        entry = {
            "text": text,
            "update_id": update_id,
            "timestamp": datetime.now().isoformat(),
        }
        mailbox_file = self._mailbox_dir / f"{project}.jsonl"
        try:
            with open(mailbox_file, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self._log(f"Mailbox write error for {project}: {e}")

    # ── Telegram API ──────────────────────────────────────

    def _api_call(self, method: str, **params) -> Optional[dict]:
        token = self._config.bot_token
        if not token:
            return None
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=max(params.get("timeout", 10) + 5, 15))
            return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def _send(self, text: str, parse_mode: str = ""):
        """Send a text message."""
        token = self._config.bot_token
        chat_id = self._config.chat_id
        if not token or not chat_id:
            return
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            data = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                data["parse_mode"] = parse_mode
            self._api_call("sendMessage", **data)

    def _send_document(self, file_path: Path, caption: str = ""):
        """Send a file (PDF, etc.)."""
        token = self._config.bot_token
        chat_id = self._config.chat_id
        if not token or not chat_id:
            return

        file_data = file_path.read_bytes()
        # Guard against mid-compilation truncated files
        if len(file_data) < 1024 or not file_data[:5] == b'%PDF-':
            self._send(f"PDF not ready (file may be mid-compilation). Try again in a moment.")
            return
        if len(caption) > 1024:
            caption = caption[:1021] + "..."

        boundary = uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n',
            f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n',
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{file_path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n',
        ]
        body = "".join(parts).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            self._send(f"⚠️ Upload failed: {str(e)[:200]}")

    # ── State Management ──────────────────────────────────

    def _load_state(self) -> dict:
        try:
            self._state_lock_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_lock_file, "a+") as lf:
                fcntl.flock(lf, fcntl.LOCK_SH)
                try:
                    if self._state_file.exists():
                        with open(self._state_file) as f:
                            return yaml.safe_load(f) or {}
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass
        return {}

    def _save_state(self, state: dict):
        try:
            self._state_lock_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_lock_file, "a+") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    with open(self._state_file, "w") as f:
                        yaml.dump(state, f, default_flow_style=False, allow_unicode=True)
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass

    def _prune_dead(self, state: dict):
        """Remove entries for processes that no longer exist."""
        active = state.get("active_projects", {})
        dead = []
        for name, info in active.items():
            pid = info.get("pid", 0)
            if pid:
                try:
                    os.kill(pid, 0)
                except OSError:
                    dead.append(name)
        for name in dead:
            del active[name]
        if dead:
            self._save_state(state)

    def _load_project_config(self, project: str) -> Optional[dict]:
        """Load a project's config.yaml."""
        try:
            from ark.cli import get_projects_dir
            config_file = get_projects_dir() / project / "config.yaml"
        except ImportError:
            # Fallback: relative to this file
            config_file = Path(__file__).parent.parent / "projects" / project / "config.yaml"
        if not config_file.exists():
            return None
        try:
            with open(config_file) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return None

    # ── PID / Lock ────────────────────────────────────────

    def _write_pid(self):
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(os.getpid()))

    def _remove_pid(self):
        try:
            self._pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _acquire_lock(self):
        """Acquire exclusive flock, retrying with timeout so SIGTERM can interrupt."""
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = open(self._lock_file, "w")
        while not self._stop_event.is_set():
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return  # Got the lock
            except (OSError, IOError):
                self._stop_event.wait(timeout=2)
        # If stop_event was set, we never got the lock — raise to exit
        raise SystemExit("Shutdown requested before lock acquired")

    def _release_lock(self):
        if self._lock_fd:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:
                pass
            self._lock_fd = None

    def _log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════
#  Lifecycle helpers (imported by cli.py)
# ══════════════════════════════════════════════════════════════

def _pid_file() -> Path:
    return get_config_dir() / "telegram_daemon.pid"


def _read_pid() -> Optional[int]:
    """Read daemon PID from file, return None if missing or invalid."""
    try:
        if _pid_file().exists():
            return int(_pid_file().read_text().strip())
    except (ValueError, OSError):
        pass
    return None


def _is_alive(pid: int) -> bool:
    """Check if a process with given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_daemon_running() -> bool:
    """Check if daemon is currently running."""
    pid = _read_pid()
    return pid is not None and _is_alive(pid)


def ensure_daemon():
    """Start daemon if not already running. Idempotent. Called by ark run/new."""
    config = TelegramConfig()
    if not config.is_configured:
        return  # No Telegram configured, skip

    pid = _read_pid()
    if pid and _is_alive(pid):
        return  # Already running

    # Clean stale PID file
    _pid_file().unlink(missing_ok=True)

    log_file = get_config_dir() / "telegram_daemon.log"

    # Strip CLAUDECODE env var so daemon can call claude CLI
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    with open(log_file, "a") as lf:
        subprocess.Popen(
            [sys.executable, "-m", "ark.telegram.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )


def stop_daemon():
    """Stop all daemon processes. Called by ark delete or ark restart-bot."""
    # Kill PID-file daemon
    pid = _read_pid()
    if pid and _is_alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(6):
            time.sleep(0.5)
            if not _is_alive(pid):
                break
        else:
            # Force kill if SIGTERM didn't work
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    _pid_file().unlink(missing_ok=True)

    # Also kill any orphaned daemon processes
    try:
        import subprocess as _sp
        result = _sp.run(
            ["pgrep", "-f", "python.*-m ark.telegram.daemon"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            orphan_pid = int(line.strip())
            if orphan_pid != os.getpid():
                try:
                    os.kill(orphan_pid, signal.SIGTERM)
                except OSError:
                    pass
        # Brief wait then force kill remaining
        time.sleep(1)
        _sp.run(
            ["pkill", "-9", "-f", "python.*-m ark.telegram.daemon"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def register_project(project_name: str):
    """Add project to registered_projects list (persists across stop/start)."""
    ark_dir = get_config_dir()
    state_file = ark_dir / "telegram_state.yaml"
    lock_file = ark_dir / ".telegram_state.lock"

    try:
        with open(lock_file, "a+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                state = {}
                if state_file.exists():
                    with open(state_file) as f:
                        state = yaml.safe_load(f) or {}
                registered = state.setdefault("registered_projects", [])
                if project_name not in registered:
                    registered.append(project_name)
                with open(state_file, "w") as f:
                    yaml.dump(state, f, default_flow_style=False, allow_unicode=True)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception:
        pass


def deregister_project(project_name: str) -> list:
    """Remove project from registered_projects. Returns remaining registered projects."""
    ark_dir = get_config_dir()
    state_file = ark_dir / "telegram_state.yaml"
    lock_file = ark_dir / ".telegram_state.lock"

    try:
        with open(lock_file, "a+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                state = {}
                if state_file.exists():
                    with open(state_file) as f:
                        state = yaml.safe_load(f) or {}

                # Remove from registered
                registered = state.get("registered_projects", [])
                registered = [p for p in registered if p != project_name]
                state["registered_projects"] = registered

                # Also remove from active (just in case)
                active = state.get("active_projects", {})
                active.pop(project_name, None)

                # Clean up sender/waiting/bound refs
                if state.get("waiting_project") == project_name:
                    state["waiting_project"] = None
                if state.get("last_sender") == project_name:
                    state["last_sender"] = None
                if state.get("bound_project") == project_name:
                    state["bound_project"] = None

                with open(state_file, "w") as f:
                    yaml.dump(state, f, default_flow_style=False, allow_unicode=True)

                return registered
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════
#  Entry point: python -m ark.telegram.daemon
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    daemon = TelegramDaemon()
    daemon.run()
