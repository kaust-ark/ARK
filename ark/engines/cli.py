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

    def build_env(self) -> dict:
        _strip = {"CLAUDECODE", "GEMINI_API_KEY", "GOOGLE_API_KEY"}
        return {k: v for k, v in os.environ.items() if k not in _strip}
        
    def execute(self, prompt: str, path_boundary: str, code_dir: Path, timeout: int, log_fn=None) -> Tuple[int, str, str, int, bool]:
        """Runs the CLI and returns (returncode, stdout, stderr, elapsed_seconds, timeout_expired)."""
        cmd = self.build_command(prompt, path_boundary, code_dir)
        env = self.build_env()
        
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

class ClaudeCLI(AgentCLI):
    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
        cmd = [
            "claude", "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--output-format", "json",
            "--append-system-prompt", path_boundary,
        ]
        if self.model_variant:
            cmd.extend(["--model", self.model_variant])
        return cmd

class GeminiCLI(AgentCLI):
    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
        cmd = [
            "gemini",
            "-p", f"[SYSTEM RULE] {path_boundary}\n\n{prompt}",
            "--approval-mode", "auto_edit",
            "-o", "json",
        ]
        if self.model_variant:
            cmd.extend(["-m", self.model_variant])
        else:
            cmd.extend(["-m", "auto"])
        return cmd

class CodexCLI(AgentCLI):
    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
        return [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C", str(code_dir),
            f"[SYSTEM RULE] {path_boundary}\n\n{prompt}",
        ]

def get_cli_for_model(model: str, variant: Optional[str] = None) -> AgentCLI:
    if model == "claude":
        return ClaudeCLI(model, variant)
    elif model == "gemini":
        return GeminiCLI(model, variant)
    elif model == "codex":
        return CodexCLI(model, variant)
    else:
        raise ValueError(f"Unsupported model backend: {model}")
