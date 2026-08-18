import os
import re
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional, Tuple
from pathlib import Path

import yaml

# ── Blocking-command watchdog ─────────────────────────────
# Executables that will block forever waiting for user input.
# Matched against argv[0] basename only — never against arguments/prompts.
_BLOCKING_EXECUTABLES = frozenset({
    "tail", "watch", "top", "htop", "less", "more", "vi", "vim", "nano", "emacs",
})

# tail -f is the only non-basename check needed (tail itself is fine without -f)
_TAIL_FOLLOW_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*[fF]|--follow(?:\s|$)")

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
            # Split on null bytes to get argv list; check only argv[0] basename
            # so prompt text embedded as arguments never triggers a false positive.
            argv = cmdline_path.read_bytes().split(b'\x00')
            argv = [a.decode(errors='replace') for a in argv if a]
            if not argv:
                continue
            exe_name = Path(argv[0]).name
            is_blocking = exe_name in _BLOCKING_EXECUTABLES
            # tail is only blocking when -f / -F / --follow is present
            if exe_name == "tail":
                rest = " ".join(argv[1:])
                is_blocking = bool(_TAIL_FOLLOW_RE.search(rest))
            if is_blocking:
                os.kill(pid, signal.SIGTERM)
                killed += 1
                if log_fn:
                    display = " ".join(argv)[:80]
                    log_fn(f"  Watchdog killed blocking process (PID {pid}): {display}", "WARN")
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return killed

class _BlockingCommandWatchdog:
    """Background thread that periodically scans for and kills blocking child processes."""
    def __init__(self, parent_pid: int, log_fn=None, interval: int = 30, grace_seconds: int = 60):
        self._parent_pid = parent_pid
        self._log_fn = log_fn
        self._interval = interval
        self._grace_seconds = grace_seconds
        self._stop = threading.Event()
        self._thread = None
        self._violation_time: float | None = None

    def start(self):
        self._stop.clear()
        self._violation_time = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _escalate(self) -> bool:
        """SIGTERM the parent's process group. Returns True if signal sent."""
        try:
            pgid = os.getpgid(self._parent_pid)
        except (ProcessLookupError, PermissionError):
            return False
        try:
            os.killpg(pgid, signal.SIGTERM)
            if self._log_fn:
                self._log_fn(
                    f"  Watchdog escalating: parent PID {self._parent_pid} still "
                    f"blocked {self._grace_seconds}s after banned command — "
                    f"SIGTERM process group",
                    "WARN",
                )
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _run(self):
        # Wait a bit before first check — give the agent time to start
        if self._stop.wait(timeout=self._interval):
            return
        while not self._stop.wait(timeout=self._interval):
            killed = _kill_blocking_descendants(self._parent_pid, self._log_fn)
            if killed and self._violation_time is None:
                self._violation_time = time.monotonic()
            if (self._violation_time is not None
                    and time.monotonic() - self._violation_time >= self._grace_seconds):
                if self._escalate():
                    return

def kill_process_tree(pid: int):
    """Kill a process and all its descendants (including the process itself)."""
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


class AgentCLI(ABC):
    def __init__(self, model_name: str, model_variant: Optional[str] = None):
        self.model_name = model_name
        self.model_variant = model_variant

    @abstractmethod
    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
        pass

    def _start_aux_stream(self, process, on_event):
        """Optional side-channel event source started right after the agent
        process spawns. Runtimes whose live events do NOT arrive on stdout
        (dsh writes them to a session log on disk) override this to start a
        tailer that feeds ``on_event`` — same contract as stdout lines,
        including killing the process tree on an ``"ABORT"`` return. Returns
        an opaque handle passed back to ``_stop_aux_stream``."""
        return None

    def _stop_aux_stream(self, handle) -> None:
        """Stop the side-channel started by ``_start_aux_stream`` (no-op)."""
        pass

    def build_env(self, code_dir: Optional[Path] = None) -> dict:
        # ARK_GITHUB_PAT (and GITHUB_TOKEN) are orchestrator-only credentials
        # used for auto-push; they must NEVER reach the autonomous agent
        # sandbox, so they are stripped from the inherited environment here.
        _strip = {"CLAUDECODE", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ARK_GITHUB_PAT", "GITHUB_TOKEN"}
        return {k: v for k, v in os.environ.items() if k not in _strip}

    def execute(self, prompt: str, path_boundary: str, code_dir: Path, timeout: int,
                log_fn=None, on_event=None, env=None) -> Tuple[int, str, str, int, bool]:
        """Run the CLI, streaming stdout, and return
        (returncode, stdout, stderr, elapsed_seconds, timeout_expired).

        ``stdout`` is the full concatenated stream (identical to the old
        ``communicate()`` result), so ``parse_output`` and every caller are
        unaffected. The new ``on_event`` callback, if given, is invoked with each
        raw stdout line *as it arrives* — the substrate for the live step log and
        the circuit breaker. ``env`` lets the caller pass a sandbox environment
        (e.g. with intervention wrappers prepended to PATH); defaults to
        ``build_env()``.
        """
        cmd = self.build_command(prompt, path_boundary, code_dir)
        env = env if env is not None else self.build_env(code_dir)

        start_time = time.time()

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,                     # line-buffered for live streaming
            cwd=str(code_dir),
            env=env,
            start_new_session=True,
        )

        watchdog = _BlockingCommandWatchdog(process.pid, log_fn=log_fn)
        watchdog.start()

        try:
            aux_stream = self._start_aux_stream(process, on_event)
        except Exception:
            aux_stream = None

        # Hard-timeout killer: fires even if the process emits no output at all
        # (a plain read loop could otherwise block forever on a silent hang).
        timeout_expired = {"v": False}
        finished = threading.Event()

        def _deadline_killer():
            if finished.wait(timeout=timeout):
                return
            timeout_expired["v"] = True
            kill_process_tree(process.pid)

        killer = threading.Thread(target=_deadline_killer, daemon=True)
        killer.start()

        # Drain stderr on a side thread so a full stderr pipe can't deadlock the
        # stdout read loop.
        stderr_chunks: list = []

        def _to_str(raw):
            return raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw

        def _drain_stderr():
            try:
                if process.stderr:
                    for line in process.stderr:
                        stderr_chunks.append(_to_str(line))
            except Exception:
                pass

        err_thread = threading.Thread(target=_drain_stderr, daemon=True)
        err_thread.start()

        stdout_lines: list = []
        try:
            if process.stdout:
                for raw in process.stdout:
                    line = _to_str(raw)
                    stdout_lines.append(line)
                    if on_event:
                        try:
                            if on_event(line) == "ABORT":
                                kill_process_tree(process.pid)
                                break
                        except Exception:
                            pass
        except Exception:
            pass

        # stdout closed → process is ending (or was just killed by the deadline).
        try:
            process.wait(timeout=10)
        except Exception:
            kill_process_tree(process.pid)
        finished.set()
        killer.join(timeout=2)
        watchdog.stop()
        err_thread.join(timeout=2)
        try:
            self._stop_aux_stream(aux_stream)
        except Exception:
            pass

        elapsed = int(time.time() - start_time)
        rc = process.returncode if process.returncode is not None else -1
        return rc, "".join(stdout_lines), "".join(stderr_chunks), elapsed, timeout_expired["v"]

class OpenHandsCLI(AgentCLI):
    """Drive the official OpenHands headless CLI (`openhands --headless --json`).

    OpenHands runs the full autonomous coding-agent loop (read/edit files, run
    bash) and routes to any provider via its internal LiteLLM. This single class
    replaces the old per-provider ClaudeCLI/GeminiCLI/CodexCLI — one code path
    for every model.

    ``model_variant`` carries the LiteLLM model string, e.g.
    ``anthropic/claude-sonnet-4-6`` / ``gemini/gemini-2.5-flash`` /
    ``openai/gpt-5``. The prefix selects which API key env var to forward.
    """

    def _llm_model(self) -> str:
        return self.model_variant or self.model_name

    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
        # OpenHands has no system-prompt flag; fold the path restriction into the
        # task text (same approach the old Gemini/Codex CLIs used).
        # --override-with-envs is REQUIRED for headless mode to honour the LLM_*
        # env vars set in build_env() (verified in Phase 0 — without it they are
        # silently ignored).
        task = f"[SYSTEM RULE] {path_boundary}\n\n{prompt}"
        return [
            "openhands", "--headless", "--json", "--override-with-envs",
            "-t", task,
        ]

    def build_env(self, code_dir: Optional[Path] = None) -> dict:
        # Resolve the provider's key via the shared <PROVIDER>_API_KEY convention
        # (llm_lite.provider_key_env) so ANY OpenHands/LiteLLM provider works,
        # not just the mainstream three.
        from ark.llm_lite import provider_key_env
        env = super().build_env()
        model = self._llm_model()
        env["LLM_MODEL"] = model
        provider = model.split("/", 1)[0] if "/" in model else ""
        key = os.environ.get(provider_key_env(provider)) if provider else None
        if key:
            env["LLM_API_KEY"] = key
        if os.environ.get("LLM_BASE_URL"):
            env["LLM_BASE_URL"] = os.environ["LLM_BASE_URL"]
        env["OPENHANDS_SUPPRESS_BANNER"] = "1"
        return env

    def parse_output(self, stdout: str) -> dict:
        """Parse `openhands --json` output.

        The stream is JSONL events interleaved with terminal-UI noise, so we
        keep only cleanly-parseable JSON objects. Returns:
            {result, usage, error_code, error_detail, conversation_id}
        - result: final agent message text ("" if none)
        - usage: cost/token dict (read from the persisted base_state.json) or None
        - error_code/detail: from a ConversationErrorEvent, if any
        """
        import json
        conv_id = None
        error_code = None
        error_detail = None
        last_agent_msg = ""
        finish_msg = ""
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if conv_id is None and line.startswith("Conversation ID:"):
                conv_id = line.split(":", 1)[1].strip()
                continue
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                evt = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            kind = evt.get("kind")
            if kind == "ConversationErrorEvent":
                error_code = evt.get("code")
                error_detail = str(evt.get("detail", ""))[:500]
            elif kind == "MessageEvent" and evt.get("source") == "agent":
                msg = evt.get("llm_message") or {}
                parts = msg.get("content") or []
                text = "".join(
                    p.get("text", "") for p in parts
                    if isinstance(p, dict) and p.get("type") == "text"
                )
                if text.strip():
                    last_agent_msg = text
            elif kind == "ActionEvent":
                # Some models (notably Gemini) end the task with a FinishAction
                # carrying the final message, and never emit a final
                # MessageEvent/agent. Without this, parse_output returns "" and
                # the orchestrator wrongly treats a successful run as empty.
                action = evt.get("action") or {}
                if isinstance(action, dict) and action.get("kind") == "FinishAction":
                    fm = action.get("message")
                    if isinstance(fm, str) and fm.strip():
                        finish_msg = fm
        return {
            "result": last_agent_msg or finish_msg,
            "usage": self._read_usage(conv_id) if conv_id else None,
            "error_code": error_code,
            "error_detail": error_detail,
            "conversation_id": conv_id,
        }

    @staticmethod
    def _read_usage(conv_id: str) -> Optional[dict]:
        """Read token/cost from the persisted conversation state.

        OpenHands does not put cost in the JSON stream; it persists it to
        ``~/.openhands/conversations/<id>/base_state.json`` under
        ``stats.usage_to_metrics`` (computed by its internal LiteLLM). We sum the
        ``agent`` and ``condenser`` usages. Returns a dict shaped like ARK's
        existing usage records, or None if unavailable.
        """
        import json
        try:
            # Production: openhands persists under the orchestrator's
            # $HOME/.openhands (it inherits HOME), so Path.home() matches.
            # ARK_OPENHANDS_CONV_DIR is a test-only override (unset in prod).
            conv_root = os.environ.get("ARK_OPENHANDS_CONV_DIR")
            base = (Path(conv_root) if conv_root
                    else Path.home() / ".openhands" / "conversations")
            bs = base / conv_id / "base_state.json"
            if not bs.exists():
                return None
            data = json.loads(bs.read_text())
            metrics = (data.get("stats") or {}).get("usage_to_metrics") or {}
            cost = 0.0
            in_tok = out_tok = cache_read = cache_write = 0
            model = ""
            for m in metrics.values():
                cost += float(m.get("accumulated_cost") or 0.0)
                tu = m.get("accumulated_token_usage") or {}
                in_tok += int(tu.get("prompt_tokens") or 0)
                out_tok += int(tu.get("completion_tokens") or 0)
                cache_read += int(tu.get("cache_read_tokens") or 0)
                cache_write += int(tu.get("cache_write_tokens") or 0)
                model = tu.get("model") or model
            return {
                "model": model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_write,
                "cost_usd": cost,
                "duration_api_ms": 0,
            }
        except Exception:
            return None


class _DshSessionTailer(threading.Thread):
    """Follow a dsh session log as it is appended and feed lines to ``on_event``.

    dsh's headless runner prints ONLY the final assistant text on stdout; the
    live event stream (tool calls, assistant messages, turn boundaries) goes to
    an append-only ``session.jsonl`` under ``$DSH_HOME/sessions``. This tailer
    is the bridge that keeps ARK's live step log and circuit breaker working:
    it discovers the session file the run creates, streams each new line to the
    same ``on_event`` callback the stdout loop uses, and kills the agent
    process tree if the callback returns ``"ABORT"`` (same contract).
    """

    POLL_SECONDS = 1.0

    def __init__(self, sessions_root: Path, baseline: set, process, on_event):
        super().__init__(daemon=True)
        self._root = sessions_root
        self._baseline = baseline
        self._process = process
        self._on_event = on_event
        self._stop = threading.Event()
        self.session_file: Optional[Path] = None

    def _discover(self) -> Optional[Path]:
        try:
            candidates = [p for p in self._root.rglob("session.jsonl")
                          if p not in self._baseline]
        except OSError:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _emit(self, raw: bytes) -> Optional[str]:
        try:
            if self._on_event:
                return self._on_event(raw.decode("utf-8", "replace"))
        except Exception:
            pass
        return None

    def run(self):
        # Binary mode: a partial line's rewind must count BYTES, not chars
        # (session events routinely carry multi-byte UTF-8 text).
        fh = None
        try:
            while not self._stop.is_set():
                if fh is None:
                    found = self._discover()
                    if found is not None:
                        self.session_file = found
                        try:
                            fh = open(found, "rb")
                        except OSError:
                            fh = None
                    if fh is None:
                        if self._stop.wait(self.POLL_SECONDS):
                            break
                        continue
                line = fh.readline()
                if not line:
                    if self._stop.wait(self.POLL_SECONDS):
                        break
                    continue
                if not line.endswith(b"\n"):
                    # Partial line still being written — rewind and retry.
                    fh.seek(-len(line), os.SEEK_CUR)
                    if self._stop.wait(self.POLL_SECONDS):
                        break
                    continue
                if self._emit(line) == "ABORT":
                    kill_process_tree(self._process.pid)
                    break
        finally:
            # Drain whatever is already on disk so a fast run loses no events —
            # including runs that finished before the first discovery poll.
            try:
                if fh is None:
                    found = self._discover()
                    if found is not None:
                        self.session_file = found
                        try:
                            fh = open(found, "rb")
                        except OSError:
                            fh = None
                if fh is not None:
                    for line in fh:
                        if line.endswith(b"\n"):
                            self._emit(line)
                    fh.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        self.join(timeout=5)


class DshCLI(AgentCLI):
    """Drive DeepSeek Harness's one-shot runner (`dsh --profile headless`).

    Selected with a ``dsh/`` model prefix, e.g. ``dsh/deepseek-v4`` or the
    explicit ``dsh/<provider>/<model>`` form (default provider:
    ``deepseek-official``). Unlike OpenHands, dsh enforces the workspace
    boundary at the OS level (Landlock sandbox, ``DSH_PERMISSION_MODE``,
    default ``workspace-write`` rooted at the agent's cwd = the project dir)
    and fails CLOSED on approval escalations when no human answerer is
    attached — so the ARK path restriction is enforced, not just prompted.

    Runtime contract (verified against dsh 0.1.0-rc.7):
      * stdout: the final assistant text only (+ trailing newline)
      * stderr: ``dsh: <CODE>: <detail>`` on failure
      * exit code: 0 only when the turn ended with reason ``completed``
      * full event stream: ``$DSH_HOME/sessions/<project>/<session-id>/
        session.jsonl`` (we patch compression to ``none`` so Python can read
        it); usage lives in ``assistant/chunk``/``assistant/message`` events,
        errors in ``turn/end``.

    Each project gets its own ``DSH_HOME`` (``<code_dir>/.dsh_home``) —
    sessions, profile state, and skills stay inside the project sandbox,
    mirroring ARK's per-project conda env isolation.
    """

    DEFAULT_PROVIDER = "deepseek-official"

    # dsh provider id → env var holding its API key. Anything not listed
    # falls back to the shared <PROVIDER>_API_KEY convention on the first
    # dash-separated token ("deepseek-official" → DEEPSEEK_API_KEY).
    _PROVIDER_KEY_ENV = {
        "deepseek-official": "DEEPSEEK_API_KEY",
    }

    def __init__(self, model_name: str, model_variant: Optional[str] = None):
        super().__init__(model_name, model_variant)
        self._sessions_root: Optional[Path] = None
        self._session_baseline: set = set()
        self._tailer: Optional[_DshSessionTailer] = None

    # ---- model / provider resolution ----

    def _spec(self) -> Tuple[str, str]:
        """Resolve (provider, model) from the ``dsh/...`` model string."""
        raw = self.model_variant or self.model_name or ""
        if raw.startswith("dsh/"):
            raw = raw[len("dsh/"):]
        parts = raw.split("/", 1)
        if len(parts) == 2 and parts[0]:
            return parts[0], parts[1]
        return self.DEFAULT_PROVIDER, raw

    def _key_env(self, provider: str) -> str:
        try:
            from ark.llm_lite import provider_key_env
        except Exception:
            provider_key_env = lambda p: f"{p.upper()}_API_KEY"  # noqa: E731
        return self._PROVIDER_KEY_ENV.get(
            provider, provider_key_env(provider.split("-", 1)[0]))

    def _dsh_home(self, code_dir: Path) -> Path:
        return Path(code_dir) / ".dsh_home"

    # ---- launch ----

    def _write_patch(self, code_dir: Path) -> Path:
        """Write the per-run patch overlay that configures dsh for ARK.

        NOTE: a patch entry's ``config`` REPLACES the plugin's whole config
        (no deep-merge), so every entry restates required fields.
        """
        provider, model = self._spec()
        dsh_home = self._dsh_home(code_dir)
        sessions_root = dsh_home / "sessions"
        sessions_root.mkdir(parents=True, exist_ok=True)

        bash_timeout_ms = int(os.environ.get("ARK_DSH_BASH_TIMEOUT_MS", "600000"))
        patch = [
            # Which model answers — the same knob the Web UI's model picker sets.
            {"id": "agent-default-model",
             "config": {"provider": provider, "model": model}},
            # Plain (uncompressed) session logs so the orchestrator can tail
            # and parse them without a zstd dependency.
            {"id": "session-persistence-jsonl",
             "config": {"root": str(sessions_root), "compression": "none"}},
            # dsh's default per-command limit is 60s — far too short for
            # experiment installs/compiles. Match ARK's long-running reality.
            {"id": "bash-sandbox",
             "config": {"timeoutMs": bash_timeout_ms}},
        ]
        patch_file = dsh_home / "ark.patch.yml"
        patch_file.write_text(yaml.safe_dump(patch, sort_keys=False))
        return patch_file

    @staticmethod
    def _ensure_agents_skills_link(code_dir: Path) -> None:
        """Expose ARK's installed skills to dsh.

        ARK installs skills into ``<project>/.claude/skills``; dsh discovers
        project skills in ``<project>/.agents/skills`` (same SKILL.md +
        YAML-frontmatter format, verified compatible). A symlink shares one
        skill tree across both runtimes.
        """
        skills_src = Path(code_dir) / ".claude" / "skills"
        agents_dir = Path(code_dir) / ".agents"
        link = agents_dir / "skills"
        if not skills_src.is_dir() or link.exists():
            return
        try:
            agents_dir.mkdir(parents=True, exist_ok=True)
            link.symlink_to(Path("..") / ".claude" / "skills")
        except OSError:
            pass  # e.g. FS without symlinks — dsh just won't see the skills

    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
        patch_file = self._write_patch(code_dir)
        self._ensure_agents_skills_link(code_dir)
        self._sessions_root = self._dsh_home(code_dir) / "sessions"
        try:
            self._session_baseline = set(self._sessions_root.rglob("session.jsonl"))
        except OSError:
            self._session_baseline = set()
        # The OS sandbox enforces the boundary; the [SYSTEM RULE] line keeps the
        # agent's *intent* aligned too (same convention as the OpenHands path).
        task = f"[SYSTEM RULE] {path_boundary}\n\n{prompt}"
        dsh_bin = os.environ.get("ARK_DSH_BIN", "dsh")
        return [dsh_bin, "--profile", "headless", "--patch", str(patch_file), task]

    def build_env(self, code_dir: Optional[Path] = None) -> dict:
        env = super().build_env(code_dir)
        if code_dir is not None:
            env["DSH_HOME"] = str(self._dsh_home(code_dir))
        # workspace-write = OS-enforced writes only inside cwd (the project),
        # with approval escalations failing closed in headless runs.
        env.setdefault(
            "DSH_PERMISSION_MODE",
            os.environ.get("ARK_DSH_PERMISSION_MODE", "workspace-write"))
        # Never phone telemetry home from an autonomous research run unless
        # the operator explicitly opted in.
        env.setdefault("DSH_TELEMETRY_MODE", "DISABLED")
        provider, _ = self._spec()
        key_env = self._key_env(provider)
        key = os.environ.get(key_env)
        if key:
            env[key_env] = key
        return env

    def _start_aux_stream(self, process, on_event):
        if on_event is None or self._sessions_root is None:
            return None
        tailer = _DshSessionTailer(
            self._sessions_root, self._session_baseline, process, on_event)
        tailer.start()
        self._tailer = tailer
        return tailer

    def _stop_aux_stream(self, handle) -> None:
        if handle is not None:
            handle.stop()

    # ---- result parsing ----

    def _find_session_file(self) -> Optional[Path]:
        if self._tailer is not None and self._tailer.session_file is not None:
            return self._tailer.session_file
        if self._sessions_root is None:
            return None
        try:
            fresh = [p for p in self._sessions_root.rglob("session.jsonl")
                     if p not in self._session_baseline]
        except OSError:
            return None
        if not fresh:
            return None
        return max(fresh, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _estimate_cost(model: str, in_tok: int, out_tok: int,
                       cache_read: int, cache_write: int) -> float:
        """Best-effort USD estimate via LiteLLM's price table (0.0 if unknown).

        dsh session logs carry provider-reported token counts but no price;
        the dashboard already labels non-provider-billed totals as estimates.
        """
        try:
            from litellm import cost_per_token
            prompt_tokens = in_tok + cache_read + cache_write
            for candidate in (f"deepseek/{model}", model):
                try:
                    in_cost, out_cost = cost_per_token(
                        model=candidate,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=out_tok,
                    )
                    return float(in_cost) + float(out_cost)
                except Exception:
                    continue
        except Exception:
            pass
        return 0.0

    def parse_output(self, stdout: str) -> dict:
        """Assemble ARK's runner result from stdout + the session event log.

        Returns the same shape as ``OpenHandsCLI.parse_output``:
        ``{result, usage, error_code, error_detail, conversation_id}``.
        """
        import json as _json

        result = (stdout or "").strip()
        error_code = None
        error_detail = None
        conv_id = None
        model_seen = ""
        # Usage events repeat per (turn, step) with running totals — keep the
        # LAST value per step (mirrors dsh's own token-meter fold), then sum.
        step_usage: dict = {}

        session_file = self._find_session_file()
        if session_file is not None:
            conv_id = session_file.parent.name
            try:
                with open(session_file, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = _json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        etype = evt.get("type")
                        data = evt.get("data") or {}
                        if etype == "request/header":
                            cfg = (data.get("header") or {}).get("config") or {}
                            model_seen = cfg.get("model") or model_seen
                        elif etype == "assistant/chunk":
                            chunk = data.get("chunk") or {}
                            if chunk.get("type") == "usage" and chunk.get("usage"):
                                step_usage[(data.get("turn"), data.get("step"))] = chunk["usage"]
                        elif etype == "assistant/message":
                            if data.get("usage"):
                                step_usage[(data.get("turn"), data.get("step"))] = data["usage"]
                        elif etype == "turn/end":
                            reason = data.get("reason") or {}
                            if reason.get("kind") == "error":
                                err = reason.get("error") or {}
                                error_code = str(err.get("code") or "DSH_ERROR")
                                error_detail = str(err.get("message") or "")[:500]
                            else:
                                error_code = None
                                error_detail = None
            except OSError:
                pass

        usage = None
        if step_usage:
            in_tok = sum(int(u.get("inputTokens") or 0) for u in step_usage.values())
            out_tok = sum(int(u.get("outputTokens") or 0) for u in step_usage.values())
            cache_read = sum(int(u.get("cacheReadTokens") or 0) for u in step_usage.values())
            cache_write = sum(int(u.get("cacheWriteTokens") or 0) for u in step_usage.values())
            _, model = self._spec()
            usage = {
                "model": model_seen or model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_write,
                "cost_usd": self._estimate_cost(
                    model_seen or model, in_tok, out_tok, cache_read, cache_write),
                "duration_api_ms": 0,
            }

        return {
            "result": result,
            "usage": usage,
            "error_code": error_code,
            "error_detail": error_detail,
            "conversation_id": conv_id,
        }


def get_cli_for_model(model: str, variant: Optional[str] = None) -> AgentCLI:
    """Return the agent runtime for a model string.

    ``dsh/...`` prefixes select the DeepSeek Harness runtime (e.g.
    ``dsh/deepseek-v4``); everything else runs through OpenHands, where
    ``model`` / ``variant`` is the LiteLLM model string (e.g.
    ``anthropic/claude-sonnet-4-6``)."""
    selected = variant or model or ""
    if selected.startswith("dsh/"):
        return DshCLI(model, selected)
    return OpenHandsCLI(model, variant or model)
