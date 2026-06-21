"""InterventionManager — one object the orchestrator wires up and the agent
execution path consults.

It bundles the policy, the approval gate (over a Telegram channel when the
orchestrator supplies ``ask_fn``/``notify_fn``, else fail-open), the shadow-PATH
wrappers + their watcher, and a per-agent step logger. Keeping it in one place
means ``run_agent`` only needs ``self._intervention`` and the orchestrator only
needs to build it once.

Everything degrades safely: if intervention is disabled, or no approval channel
is configured, agents run exactly as before (plus the richer step log, which is
always on).
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Callable, Optional, Sequence

from .policy import load_policy
from .gate import Gate, build_channel
from .wrappers import install_wrappers, sandbox_env as _sandbox_env, ApprovalWatcher, WRAPPED_EXES
from ark.observability.steps import StepLogger, Redactor, StepEvent


class CircuitBreaker:
    """Backstop for the wrappers (the 'C' layer).

    The shadow-PATH wrappers gate risky commands the agent runs by name. They
    can be bypassed by calling a binary via its absolute path (``/bin/rm``),
    which sidesteps PATH. The breaker watches the live event stream and, when it
    sees such a bypass of a risky binary, runs it through the gate; a denial
    aborts the agent run.
    """

    def __init__(self, policy, gate, work_dirs, log_fn=None):
        self.policy = policy
        self.gate = gate
        self.work_dirs = work_dirs
        self._log = log_fn or (lambda *a, **k: None)

    def inspect(self, evt: StepEvent) -> bool:
        """Return True if the agent run should be aborted."""
        if evt.type != "command":
            return False
        cmd = evt.detail or evt.summary
        if cmd.startswith("$ "):
            cmd = cmd[2:]
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return False
        if not argv:
            return False
        exe = os.path.basename(argv[0])
        # Only act on an absolute-path call to a binary the wrappers would have
        # shadowed — i.e. a genuine bypass. Name-based calls already went through
        # the wrapper, so re-checking here would double-ask.
        if argv[0].startswith("/") and exe in WRAPPED_EXES:
            argv[0] = exe  # classify by basename
            allowed = self.gate.check_command(
                argv, work_dirs=self.work_dirs, origin="agent (PATH bypass)")
            if not allowed:
                self._log("  [intervention] circuit breaker: denied wrapper-bypass "
                          f"command, aborting agent — {cmd}", "ERROR")
                return True
        return False


class InterventionManager:
    def __init__(
        self,
        config: Optional[dict],
        state_dir,
        work_dirs: Sequence[Path],
        *,
        ask_fn: Optional[Callable] = None,
        notify_fn: Optional[Callable] = None,
        ask_secret_fn: Optional[Callable] = None,
        log_fn: Optional[Callable] = None,
        secret_values: Optional[list] = None,
    ):
        cfg = (config or {}).get("intervention") or {}
        # master switch: intervention gating on/off (observability stays on either way)
        self.enabled = bool(cfg.get("enabled", True))
        self.verbosity = str(cfg.get("log_verbosity") or (config or {}).get("log_verbosity") or "normal")

        self.policy = load_policy(config)
        self.state_dir = Path(state_dir)
        self.intervention_dir = self.state_dir / "intervention"
        self.shim_dir = self.intervention_dir / "bin"
        self.work_dirs = [Path(w) for w in (work_dirs or [])]
        self._log = log_fn or (lambda *a, **k: None)
        self.redactor = Redactor(secret_values)

        channel = build_channel(
            "telegram" if (ask_fn and notify_fn) else "null",
            ask_fn=ask_fn, notify_fn=notify_fn, ask_secret_fn=ask_secret_fn, log_fn=self._log,
        )
        self.gate = Gate(self.policy, channel, state_dir=self.state_dir, log_fn=self._log)
        self._watcher: Optional[ApprovalWatcher] = None
        self._wrappers_ready = False

    # ---- lifecycle ----

    def start(self) -> None:
        """Install wrappers + start the approval watcher (idempotent)."""
        if not self.enabled or self._wrappers_ready:
            return
        try:
            install_wrappers(self.shim_dir)
            watcher = ApprovalWatcher(
                self.intervention_dir, self.gate, self.work_dirs, log_fn=self._log,
                secret_sink=self.redactor.add)
            watcher.start()
            self._watcher = watcher
            self._wrappers_ready = True
            self._log(f"  [intervention] active (autonomy={self.policy.autonomy}); "
                      f"wrappers at {self.shim_dir}", "INFO")
        except Exception as e:
            self._log(f"  [intervention] failed to start, continuing without gate: {e}", "WARN")

    def stop(self) -> None:
        if self._watcher:
            self._watcher.stop()
            self._watcher = None

    # ---- hooks used by run_agent ----

    def sandbox_env(self, base_env: dict, agent_type: str) -> dict:
        """Return base_env with wrappers on PATH (no-op if not ready)."""
        if not self.enabled or not self._wrappers_ready:
            return base_env
        return _sandbox_env(base_env, self.intervention_dir, self.shim_dir, agent_type=agent_type)

    def step_logger(self, agent_type: str, log_step_fn: Callable) -> StepLogger:
        return StepLogger(
            steps_path=self.state_dir / "agent_steps.jsonl",
            log_step_fn=log_step_fn,
            redactor=self.redactor,
            verbosity=self.verbosity,
            agent_type=agent_type,
        )

    def event_handler(self, agent_type: str, log_step_fn: Callable) -> Callable:
        """Build the per-line callback passed to ``execute(on_event=...)``:
        feed the step log, then run the circuit breaker. Returns ``"ABORT"`` to
        tell ``execute`` to kill the agent when the breaker denies a bypass."""
        steplog = self.step_logger(agent_type, log_step_fn)
        breaker = (CircuitBreaker(self.policy, self.gate, self.work_dirs, self._log)
                   if self.enabled else None)

        def handle(line: str):
            evt = steplog.feed_line(line)
            if evt is not None and breaker is not None and breaker.inspect(evt):
                return "ABORT"
            return None

        return handle

    # ---- orchestrator-side gates (cloud / git push / spend) ----

    def check_action(self, action_type: str, **ctx) -> bool:
        """Gate an orchestrator-initiated action. True = proceed."""
        if not self.enabled:
            return True
        try:
            return self.gate.check_action(action_type, origin="orchestrator", **ctx)
        except Exception as e:
            self._log(f"  [intervention] action gate error ({action_type}), allowing: {e}", "WARN")
            return True

    def register_secret(self, value: str) -> None:
        """Add a secret value to the redaction set so it never appears in logs."""
        if value:
            self.redactor.add(value)
