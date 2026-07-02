"""Local subprocess / systemd launcher — thin adapter over
``website.dashboard.jobs`` (behavior-identical to the pre-Phase-4 path).

The webapp modules are imported lazily inside each method so that importing
``ark.launcher`` stays webapp-free (these launchers only ever run inside the
control-plane process, where ``website.dashboard.jobs`` is importable)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import JobLauncher, LaunchSpec, PollResult, newest_log, UNKNOWN


class LocalJobLauncher(JobLauncher):
    """Run the orchestrator as a detached ``systemd --user`` service (or an
    in-webapp child where systemd is unavailable). Handle: ``local:{pid}``."""

    log_glob = "local_*.out"

    def launch(self, spec: LaunchSpec) -> str:
        from website.dashboard.jobs import launch_local_job

        return launch_local_job(
            spec.project_id, spec.mode, spec.max_iterations,
            spec.project_dir, spec.log_dir, spec.settings,
            api_keys=spec.api_keys,
            apply_instruction=spec.apply_instruction,
            apply_scope=spec.apply_scope,
            chat_message=spec.chat_message,
        )

    def poll(self, handle: str, project_dir: Path) -> PollResult:
        from website.dashboard.jobs import poll_local_job, slurm_state_to_status

        pid = _handle_pid(handle)
        if pid is None:
            return PollResult(UNKNOWN, handle)
        raw = poll_local_job(pid, Path(project_dir) / "logs")
        return PollResult(slurm_state_to_status(raw), raw)

    def cancel(self, handle: str, project_dir: Path, on_complete=None) -> None:
        from website.dashboard.jobs import cancel_local_job

        pid = _handle_pid(handle)
        if pid is not None:
            cancel_local_job(pid)
        if on_complete:
            on_complete()

    def read_error(self, project_dir: Path) -> str:
        """Last few meaningful lines from the newest ``local_*.out`` (empty string
        if there's no readable log). Always a string so a local failure overwrites
        any stale ``error_message`` — matching the pre-Phase-4 local branch."""
        f = newest_log(project_dir, self.log_glob)
        if f is None:
            return ""
        try:
            lines = f.read_text(errors="replace").splitlines()
            tail = [l for l in lines if l.strip()][-3:]
            return " | ".join(tail)[:300]
        except Exception:
            return ""


def _handle_pid(handle: str) -> Optional[int]:
    """Extract the pid from a ``local:{pid}`` handle, or ``None`` if malformed."""
    pid_str = handle[len("local:"):] if handle.startswith("local:") else handle
    return int(pid_str) if pid_str.isdigit() else None
