"""Local subprocess / systemd launcher — thin adapter over
``website.dashboard.jobs`` (behavior-identical to the pre-Phase-4 path).

The webapp modules are imported lazily inside each method so that importing
``ark.launcher`` stays webapp-free (these launchers only ever run inside the
control-plane process, where ``website.dashboard.jobs`` is importable)."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Optional

from .base import JobLauncher, LaunchSpec, PollResult, newest_log, UNKNOWN


class LocalJobLauncher(JobLauncher):
    """Run the orchestrator as a detached ``systemd --user`` service (or an
    in-webapp child where systemd is unavailable).

    Handle: ``local:{host}:{pid}``. A pid only means something on the node that
    spawned it, so the handle carries that node and this launcher refuses to
    judge, or signal, a pid that belongs to another one (legacy ``local:{pid}``
    handles are read as this node's)."""

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

        pid = handle_pid(handle)
        if pid is None:
            return PollResult(UNKNOWN, handle)
        if not handle_is_local(handle):
            # From any other node "no such process" reads as "the run died":
            # that is how a second control plane marks healthy runs failed.
            return PollResult(UNKNOWN, f"foreign-host:{handle_host(handle)}")
        raw = poll_local_job(pid, Path(project_dir) / "logs")
        return PollResult(slurm_state_to_status(raw), raw)

    def cancel(self, handle: str, project_dir: Path, on_complete=None) -> None:
        from website.dashboard.jobs import cancel_local_job

        pid = handle_pid(handle)
        if pid is not None and handle_is_local(handle):
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


def local_handle(pid: int) -> str:
    """The handle for a process spawned on this node."""
    return f"local:{socket.gethostname()}:{pid}"


def _handle_parts(handle: str) -> tuple[Optional[str], Optional[int]]:
    """``(host, pid)`` of a ``local:{host}:{pid}`` handle. Legacy ``local:{pid}``
    gives ``(None, pid)``; anything malformed gives ``(None, None)``."""
    body = handle[len("local:"):] if handle.startswith("local:") else handle
    host, _, pid_str = body.rpartition(":")
    if not pid_str.isdigit():
        return None, None
    return (host or None), int(pid_str)


def handle_pid(handle: str) -> Optional[int]:
    return _handle_parts(handle)[1]


def handle_host(handle: str) -> Optional[str]:
    return _handle_parts(handle)[0]


def handle_is_local(handle: str) -> bool:
    """True when this node may judge the handle's pid: it names this host, or it
    predates host-qualified handles."""
    host = handle_host(handle)
    return host is None or host == socket.gethostname()
