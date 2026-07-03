"""Layer-2 orchestrator launcher seam (Phase 4).

``launcher_from_handle`` dispatches poll/cancel off the persisted handle string;
``select_launcher`` dispatches launch off the configured backend type (mirroring
``ComputeBackend.from_config``'s ``is_orchestrator`` selection)."""

from __future__ import annotations

from typing import Callable, Optional

from .base import (
    JobLauncher, LaunchSpec, PollResult, RestartResult,
    RUNNING, QUEUED, DONE, FAILED, STOPPED, GONE, UNKNOWN, ACTIVE_STATUSES,
)
from .local import LocalJobLauncher
from .slurm import SlurmJobLauncher
from .cloud import CloudVmJobLauncher


def launcher_from_handle(handle: str, log_fn: Optional[Callable] = None) -> JobLauncher:
    """Return the launcher that owns ``handle`` (``local:{pid}`` / ``cloud:{pid}``
    / bare SLURM job id) — used to poll or cancel an already-launched run."""
    if handle.startswith("local:"):
        return LocalJobLauncher()
    if handle.startswith("cloud:"):
        return CloudVmJobLauncher(log_fn)
    return SlurmJobLauncher()


def select_launcher(backend: Optional[str], *, slurm_ok: bool) -> JobLauncher:
    """Pick the non-cloud launcher for a new run: SLURM when the cluster is
    reachable, else local (the SLURM-unavailable fallback).

    Cloud / SkyPilot dispatch is not handled here — it needs config loading + an
    "is cloud configured?" probe, which the webapp's ``orchestrator_launcher_for``
    owns. Raise on anything we don't handle rather than silently returning a
    LocalJobLauncher, so a future caller that skips that guard can't quietly run a
    cloud/skypilot orchestrator on the control-plane host."""
    base = (backend or "local").split(":", 1)[0]
    if base == "slurm":
        return SlurmJobLauncher() if slurm_ok else LocalJobLauncher()
    if base == "local":
        return LocalJobLauncher()
    raise ValueError(
        f"select_launcher cannot dispatch orchestrator backend {backend!r}; "
        "cloud/skypilot launch is owned by the webapp's orchestrator_launcher_for"
    )


__all__ = [
    "JobLauncher", "LaunchSpec", "PollResult", "RestartResult",
    "RUNNING", "QUEUED", "DONE", "FAILED", "STOPPED", "GONE", "UNKNOWN",
    "ACTIVE_STATUSES",
    "LocalJobLauncher", "SlurmJobLauncher", "CloudVmJobLauncher",
    "launcher_from_handle", "select_launcher",
]
