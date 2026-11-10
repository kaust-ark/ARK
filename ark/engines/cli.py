import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Optional, Tuple
from pathlib import Path


@contextmanager
def _openhands_tmux_env(env: dict):
    """Own the tmux server for one CLI invocation, including all its terminals.

    OpenHands uses the fixed socket name ``openhands``. A private TMUX_TMPDIR
    isolates concurrent invocations without changing OpenHands or the shell's
    state within an invocation. A detached tmux server survives killing the CLI
    process group, so it must also be stopped explicitly.
    """
    child_env = dict(env)
    tmux = shutil.which("tmux", path=child_env.get("PATH"))
    if tmux is None:
        yield child_env  # OpenHands can fall back to its subprocess terminal.
        return

    # Keep the Unix socket path short even when TMPDIR is a long project path.
    root = Path(tempfile.mkdtemp(prefix="ark-tmux-", dir="/tmp"))
    child_env["TMUX_TMPDIR"] = str(root)
    child_env.pop("TMUX", None)
    socket = root / f"tmux-{os.getuid()}" / "openhands"
    try:
        yield child_env
    finally:
        try:
            result = subprocess.run(
                [tmux, "-S", str(socket), "kill-server"],
                env=child_env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=5,
            )
            if (result.returncode and socket.exists()
                    and "no server running" not in result.stderr):
                raise RuntimeError(result.stderr.strip())
            shutil.rmtree(root)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            # Retain the socket on failure so a live server stays reachable.
            # Cleanup must not mask the agent result or an active exception.
            logging.getLogger(__name__).warning(
                "Could not clean up OpenHands tmux socket %s: %s", socket, exc,
            )

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
    # Snapshot before killing the parent: children in their own process groups
    # may otherwise be reparented before pgrep can find them.
    descendants = _get_descendant_pids(pid)
    # First try to kill the entire process group
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    # Also kill individual descendants in case pgid differs
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
    # Seconds a stopped runtime gets to close its session after SIGTERM before
    # the tree is killed. 0 keeps the immediate SIGKILL.
    graceful_stop_s = 0

    def __init__(self, model_name: str, model_variant: Optional[str] = None):
        self.model_name = model_name
        self.model_variant = model_variant

    def _stop(self, process) -> None:
        """End the agent process: SIGTERM and a grace period, then the tree kill.

        The descendants are listed first, because a runtime that exits on the
        SIGTERM leaves its children to be reparented, out of the tree's reach.
        """
        grace = self.graceful_stop_s
        if grace and process.poll() is None:
            descendants = _get_descendant_pids(process.pid)
            try:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=grace)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                pass
            for pid in reversed(descendants):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        if process.poll() is None:
            kill_process_tree(process.pid)

    @abstractmethod
    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
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

        # Hard-timeout killer: fires even if the process emits no output at all
        # (a plain read loop could otherwise block forever on a silent hang).
        timeout_expired = {"v": False}
        finished = threading.Event()

        def _deadline_killer():
            if finished.wait(timeout=timeout):
                return
            timeout_expired["v"] = True
            self._stop(process)

        killer = threading.Thread(target=_deadline_killer, daemon=True)

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
        stdout_lines: list = []
        aborting = False
        # What the call printed when a Stop's SystemExit unwound through it, so
        # the caller can still bill it. None when the call returned normally.
        self.partial_stdout = None
        unwinding = True
        try:
            watchdog.start()
            killer.start()
            err_thread.start()
            try:
                if process.stdout:
                    for raw in process.stdout:
                        line = _to_str(raw)
                        stdout_lines.append(line)
                        if on_event and not aborting:
                            try:
                                if on_event(line) == "ABORT":
                                    if not self.graceful_stop_s:
                                        kill_process_tree(process.pid)
                                        break
                                    # Keep reading: the runtime reports its
                                    # result and usage while it closes.
                                    aborting = True
                                    threading.Thread(target=self._stop, args=(process,),
                                                     daemon=True).start()
                            except Exception:
                                pass
            except Exception:
                pass

            # stdout closed → process is ending (or the deadline killed it).
            process.wait(timeout=10)
            unwinding = False
        except subprocess.TimeoutExpired:
            unwinding = False
        finally:
            # Also runs for KeyboardInterrupt/SystemExit (orchestrator SIGTERM).
            # Stop/reap the CLI before releasing its tmux server, otherwise a
            # still-running CLI could create a new terminal during cleanup.
            finished.set()
            if killer.ident is not None:
                killer.join(timeout=2)
            watchdog.stop()
            if process.poll() is None:
                self._stop(process)
                process.wait(timeout=10)
            if err_thread.ident is not None:
                err_thread.join(timeout=2)
            if unwinding and process.stdout:
                # The runtime may have reported its usage while it closed. A
                # leftover grandchild can hold the pipe open, so read on a side
                # thread and give up after a few seconds.
                reader = threading.Thread(
                    target=lambda: stdout_lines.append(_to_str(process.stdout.read() or "")),
                    daemon=True)
                reader.start()
                reader.join(timeout=5)
                self.partial_stdout = "".join(stdout_lines)
            if process.stdout:
                process.stdout.close()
            if process.stderr and not err_thread.is_alive():
                process.stderr.close()

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

    # Whether build_env writes the `openhands` output-token cap. True for
    # anything that actually spawns that CLI, including `_ChatCLI`, which
    # inherits it: the gate used to be `type(self) is OpenHandsCLI`, which
    # excluded every subclass and so left dashboard chat — the surface users
    # touch most — asking litellm for the full 393k. `OpenHandsSDK` sets it
    # False because it never shells out and sizes its own window.
    _caps_output_tokens = True

    def _llm_model(self) -> str:
        return self.model_variant or self.model_name

    def execute(self, prompt: str, path_boundary: str, code_dir: Path, timeout: int,
                log_fn=None, on_event=None, env=None) -> Tuple[int, str, str, int, bool]:
        env = env if env is not None else self.build_env(code_dir)
        with _openhands_tmux_env(env) as child_env:
            return super().execute(
                prompt, path_boundary, code_dir, timeout,
                log_fn=log_fn, on_event=on_event, env=child_env,
            )

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
        if self._caps_output_tokens and code_dir is not None:
            _cap_output_tokens(env, Path(code_dir), model)
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
        failed_tools = []
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
            elif kind == "ObservationEvent":
                # Tool calls the environment REFUSED. The caller logs these
                # because the failure they precede is silent: the agent
                # summarises a rejected edit as if it had landed. Both other
                # runtimes have always reported them; this one, the production
                # default, dropped every one. Measured on a free-model run:
                # 52 refused file_editor calls, none of them logged.
                obs = evt.get("observation") or {}
                if isinstance(obs, dict) and obs.get("is_error"):
                    text = "".join(
                        p.get("text", "") for p in (obs.get("content") or [])
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                    tool = str(evt.get("tool_name") or "tool")
                    failed_tools.append(f"{tool}: {text.strip()[:240]}")
        return {
            "result": last_agent_msg or finish_msg,
            "usage": self._read_usage(conv_id) if conv_id else None,
            "error_code": error_code,
            "error_detail": error_detail,
            "conversation_id": conv_id,
            # Bounded as the SDK driver bounds it: a loop that fails the same
            # edit fifty times would otherwise push the real result out of view.
            "failed_tools": failed_tools[-10:],
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
            in_tok = out_tok = cache_read = cache_write = requests = 0
            model = ""
            for m in metrics.values():
                cost += float(m.get("accumulated_cost") or 0.0)
                requests += len(m.get("token_usages") or [])
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
                # One entry per model response; the estimate for killed
                # sessions divides by this (engines._estimate_usage).
                "requests": requests,
            }
        except Exception:
            return None


_GEN_SETTINGS = """
import sys
from openhands.sdk import LLM
from openhands_cli.utils import get_default_cli_agent
llm = LLM(model=sys.argv[1], api_key="set-by-env", usage_id="agent", max_output_tokens=int(sys.argv[2]))
print(get_default_cli_agent(llm).model_dump_json())
"""


def _cap_output_tokens(env: dict, code_dir: Path, model: str) -> None:
    """Give the headless CLI an output-token cap it has no env var for.

    The CLI reads only LLM_MODEL, LLM_API_KEY and LLM_BASE_URL from the
    environment and otherwise builds its LLM afresh, taking max_output_tokens
    from litellm's model table, which litellm downloads at run time. Since
    August that table gives deepseek-v4-pro 393,216. OpenRouter reserves credit
    for the whole cap before it answers, so d5db85a2's writer was refused about
    120 times with "requested up to 393216 tokens, can only afford 168958",
    while the account could still pay for ordinary replies.

    The CLI does load a persisted agent from its persistence directory and then
    applies the env overrides to it, so a spec carrying the cap is written there
    once per project and model. The directory is the project's own .openhands
    (where it already is when HOME is the project), so a run never reads or
    writes the user's personal OpenHands settings. Conversations stay where
    ARK reads their usage. Any failure leaves the CLI as it was, uncapped.
    """
    cap = int(os.environ.get("ARK_MAX_OUTPUT_TOKENS", "32768") or 0)
    if cap <= 0:
        return
    persist = code_dir.resolve() / ".openhands"
    home = Path(env.get("HOME") or Path.home())
    env["OPENHANDS_PERSISTENCE_DIR"] = str(persist)
    env.setdefault("OPENHANDS_CONVERSATIONS_DIR", str(home / ".openhands" / "conversations"))
    settings, marker = persist / "agent_settings.json", persist / ".ark_output_cap"
    # One marker for one `agent_settings.json`, deliberately. With per-role
    # `agent_models`, alternating models do invalidate each other's stamp and
    # re-run the generator on every call — but that is required, not wasteful:
    # the settings file is written per model and OpenHands dictates its path,
    # so a model switch MUST rewrite it. Keying the marker alone would skip a
    # regeneration and hand model A the settings generated for model B. Making
    # it genuinely cheap means a per-model persistence dir, which also moves
    # conversation state, so it is not worth doing inside this change.
    stamp = f"{model}|{cap}"
    try:
        if settings.exists() and marker.read_text() == stamp:
            return
    except OSError:
        pass
    from ark.engines.sdk_runtime import openhands_python
    interp = openhands_python()
    if not interp:
        return
    try:
        out = subprocess.run([interp, "-I", "-c", _GEN_SETTINGS, model, str(cap)],
                             capture_output=True, text=True, timeout=180,
                             env={**{k: v for k, v in env.items() if k != "LLM_API_KEY"},
                                  "OPENHANDS_SUPPRESS_BANNER": "1"})
        spec = json.loads(out.stdout)
        if (spec.get("llm") or {}).get("max_output_tokens") != cap:
            return
        persist.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(spec))
        marker.write_text(stamp)
    except Exception:
        return


def get_cli_for_model(model: str, variant: Optional[str] = None, *,
                      resume_session_id: Optional[str] = None,
                      resume_reason: str = "", next_turn: bool = False) -> AgentCLI:
    """Return the selected agent runtime for a provider/model identifier.

    ``ARK_AGENT_RUNTIME=dsh`` explicitly selects the isolated DSH SDK. It never
    falls back to another runtime when configuration or dependencies fail.

    ``ARK_AGENT_RUNTIME=sdk`` swaps the subprocess CLI for the in-process SDK
    runtime, which is the only place we can bound what the conversation
    history costs (see ark/engines/sdk_runtime.py). Both paths present the
    same interface, so they can be A/B compared on the same project. The CLI
    remains the default until the SDK path has been measured against it.

    ``resume_session_id`` continues an interrupted call in its own session.
    Only DSH supports it; the other runtimes ignore it and start fresh.
    """
    if os.environ.get("ARK_AGENT_RUNTIME", "").strip().lower() == "dsh":
        from ark.engines.dsh_runtime import DSHRuntime
        return DSHRuntime(model, variant or model, resume_session_id=resume_session_id,
                          resume_reason=resume_reason, next_turn=next_turn)
    try:
        from ark.engines.sdk_runtime import OpenHandsSDK, sdk_runtime_enabled
        if sdk_runtime_enabled():
            return OpenHandsSDK(model, variant or model)
    except Exception:
        # A broken/absent SDK must never take the agent runtime down with it.
        pass
    return OpenHandsCLI(model, variant or model)
