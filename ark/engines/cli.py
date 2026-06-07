import os
import re
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional, Tuple
from pathlib import Path

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

    def build_env(self, code_dir: Optional[Path] = None) -> dict:
        _strip = {"CLAUDECODE", "GEMINI_API_KEY", "GOOGLE_API_KEY"}
        return {k: v for k, v in os.environ.items() if k not in _strip}

    def execute(self, prompt: str, path_boundary: str, code_dir: Path, timeout: int, log_fn=None) -> Tuple[int, str, str, int, bool]:
        """Runs the CLI and returns (returncode, stdout, stderr, elapsed_seconds, timeout_expired)."""
        cmd = self.build_command(prompt, path_boundary, code_dir)
        env = self.build_env(code_dir)
        
        start_time = time.time()
        
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(code_dir),
            env=env,
            start_new_session=True
        )
        
        watchdog = _BlockingCommandWatchdog(process.pid, log_fn=log_fn)
        watchdog.start()
        
        stdout, stderr, timeout_expired = "", "", False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timeout_expired = True
            watchdog.stop()
            kill_process_tree(process.pid)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except Exception:
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        try:
                            pipe.close()
                        except Exception:
                            pass
                stdout, stderr = "", ""
                
        watchdog.stop()
        elapsed = int(time.time() - start_time)
        return process.returncode, stdout or "", stderr or "", elapsed, timeout_expired

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
        return {
            "result": last_agent_msg,
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
            bs = (Path.home() / ".openhands" / "conversations"
                  / conv_id / "base_state.json")
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


def get_cli_for_model(model: str, variant: Optional[str] = None) -> AgentCLI:
    """Return the agent runtime. Everything runs through OpenHands now; ``model``
    / ``variant`` is the LiteLLM model string (e.g. ``anthropic/claude-sonnet-4-6``)."""
    return OpenHandsCLI(model, variant or model)
