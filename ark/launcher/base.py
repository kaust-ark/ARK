"""Layer-2 orchestrator launcher seam (Phase 4).

`JobLauncher` parallels `ComputeBackend` (Layer 1, `ark/compute/base.py`): it
abstracts *launching the orchestrator process* so that `local` / `slurm` /
`skypilot` (and later `k8s`) become a config switch rather than ad-hoc branching
in the webapp. The concrete launchers are thin adapters over the battle-tested
functions in `website/dashboard/jobs.py` and SkyPilot's SDK so behavior is
identical to the pre-Phase-4 code paths.

A launcher returns an **opaque handle** string (today: ``local:{pid}`` /
``skypilot:{cluster}`` / a bare SLURM job id) which is what the webapp persists as
``Project.slurm_job_id``. `launcher_from_handle` maps a handle back to the
launcher that owns it, so poll/cancel dispatch purely off the stored handle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# ── Normalized poll states ───────────────────────────────────────────────────
# Every launcher's poll() maps its backend-specific status onto one of these so
# the webapp poller can drive a single, unified transition path.
RUNNING = "running"
QUEUED = "queued"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
# The process vanished but the launcher has no authoritative *outcome* — the
# control-plane DB is the source of truth (the remote orchestrator self-reports
# its terminal status over /v1). The poller treats this as a crash-safety-net:
# mark failed only if the DB still shows the run active.
GONE = "gone"
# Transient / indeterminate (probe error, no state file yet). Leave the project
# as-is and retry on the next cycle.
UNKNOWN = "unknown"

#: Statuses the poller considers "still active" in the control-plane DB.
ACTIVE_STATUSES = ("queued", "running", "pending", "initializing")


@dataclass
class LaunchSpec:
    """Everything a launcher needs to start an orchestrator run.

    Mirrors the argument list the pre-Phase-4 ``submit_job`` /
    ``launch_local_job`` / cloud launch path took. ``apply_instruction`` /
    ``apply_scope`` / ``chat_message`` are only honoured by the local launcher
    (they were only ever passed to ``launch_local_job``); other launchers ignore
    them. ``config`` carries the parsed ``config.yaml`` and is only needed by the
    cloud launcher.
    """

    project_id: str
    mode: str
    max_iterations: int
    project_dir: Path
    log_dir: Path
    settings: object
    api_keys: Optional[dict] = None
    apply_instruction: str = ""
    apply_scope: str = "edit"
    chat_message: str = ""
    config: Optional[dict] = None


@dataclass
class PollResult:
    """Normalized outcome of a single poll. ``raw`` keeps the backend-specific
    state string for logging."""

    state: str
    raw: str = ""


@dataclass
class RestartResult:
    """Returned by `JobLauncher.maybe_restart` when a launcher auto-restarts an
    involuntarily-stopped run (SLURM cluster preemption). ``attempt`` is the
    restart attempt count, used in the operator notification."""

    handle: str
    attempt: int


def newest_log(project_dir: Path, pattern: str) -> Optional[Path]:
    """Newest file matching ``<project_dir>/logs/<pattern>``, or ``None``."""
    logs = sorted(
        (Path(project_dir) / "logs").glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return logs[0] if logs else None


class JobLauncher(ABC):
    """Launch / poll / cancel the orchestrator process for one project."""

    #: DB status to set immediately after a successful launch. SLURM enqueues
    #: (``queued``); local/cloud start running right away.
    initial_status: str = RUNNING

    #: Glob for this backend's orchestrator log under ``<project_dir>/logs`` (the
    #: stuck watchdog watches its mtime). ``None`` ⇒ no local log to watch (cloud).
    log_glob: Optional[str] = None

    @abstractmethod
    def launch(self, spec: LaunchSpec) -> str:
        """Start the orchestrator; return the opaque handle to persist."""

    @abstractmethod
    def poll(self, handle: str, project_dir: Path) -> PollResult:
        """Report the run's current state, normalized onto the module constants."""

    @abstractmethod
    def cancel(self, handle: str, project_dir: Path,
               on_complete: Optional[Callable] = None) -> None:
        """Stop the run (and any sub-jobs / VMs it owns).

        ``on_complete`` runs after the cancel work has finished reading whatever it
        needs from ``project_dir`` — synchronously for local/SLURM, and only after
        the (async) cloud-VM teardown has read the project's config/state for the
        cloud launcher. The delete endpoint passes ``rmtree`` here so the project
        dir isn't removed out from under an in-flight teardown."""

    # ── optional hooks (no-op defaults) ──────────────────────────────────────
    def maybe_restart(self, handle: str, spec: LaunchSpec) -> Optional[RestartResult]:
        """Auto-restart after an *involuntary* stop, if this backend supports it
        (SLURM does; local/cloud do not). Returns the new handle + attempt count,
        or ``None`` to let the poller handle the stop normally."""
        return None

    def latest_log_mtime(self, project_dir: Path) -> Optional[float]:
        """Newest orchestrator log mtime for the stuck watchdog, or ``None`` if
        this backend has no local log to watch (``log_glob`` unset ⇒ cloud)."""
        if not self.log_glob:
            return None
        f = newest_log(project_dir, self.log_glob)
        return f.stat().st_mtime if f else None

    def read_error(self, project_dir: Path) -> Optional[str]:
        """Crash tail to store on a failed transition, or ``None`` if this backend
        has no local log to read (SLURM/cloud). ``None`` means "leave the existing
        ``error_message`` untouched"; a string (possibly empty) means "set it".
        Only the local launcher writes a log the control plane can read."""
        return None
