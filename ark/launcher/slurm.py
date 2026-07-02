"""SLURM launcher — thin adapter over ``website.dashboard.jobs`` (a straight,
behavior-identical port of ``submit_job`` / ``poll_job`` / ``cancel_job`` /
``cancel_project_sub_jobs``). The rendered ``sbatch`` script and submission path
are unchanged; a golden test guards this (SLURM is a hard invariant)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import JobLauncher, LaunchSpec, PollResult, RestartResult, QUEUED, UNKNOWN

#: The poller only auto-restarts a cluster-cancelled job this many times before
#: giving up (matches the pre-Phase-4 ``len(log_files) < 5`` gate).
MAX_AUTO_RESTARTS = 5


class SlurmJobLauncher(JobLauncher):
    """Submit the orchestrator via ``sbatch``. Handle: the bare SLURM job id."""

    initial_status = QUEUED
    log_glob = "slurm_*.out"

    @staticmethod
    def available() -> bool:
        """True if ``sbatch`` is usable (respects ``ARK_FORCE_LOCAL``)."""
        from website.dashboard.jobs import slurm_available

        return slurm_available()

    def launch(self, spec: LaunchSpec) -> str:
        from website.dashboard.jobs import submit_job

        return submit_job(
            spec.project_id, spec.mode, spec.max_iterations,
            spec.project_dir, spec.log_dir, spec.settings,
            api_keys=spec.api_keys,
        )

    def poll(self, handle: str, project_dir: Path) -> PollResult:
        from website.dashboard.jobs import poll_job, slurm_state_to_status

        if not handle:
            return PollResult(UNKNOWN, handle)
        raw = poll_job(handle)
        return PollResult(slurm_state_to_status(raw), raw)

    def cancel(self, handle: str, project_dir: Path, on_complete=None) -> None:
        from website.dashboard.jobs import cancel_job, cancel_project_sub_jobs

        cancel_job(handle)
        # Cascade: the orchestrator submits experimenter sub-jobs under the
        # project dir. Cancel any still-queued ones so a dead pipeline doesn't
        # leak compute.
        cascaded = cancel_project_sub_jobs(Path(project_dir))
        if cascaded:
            import logging
            logging.getLogger("ark.launcher").info(
                "Stop cascade: cancelled %d sub-job(s): %s",
                len(cascaded), ",".join(cascaded),
            )
        if on_complete:
            on_complete()

    def maybe_restart(self, handle: str, spec: LaunchSpec) -> Optional[RestartResult]:
        """Resubmit a job the cluster cancelled out from under us, up to
        ``MAX_AUTO_RESTARTS`` times (counted by ``slurm_*.out`` log files)."""
        from website.dashboard.jobs import submit_job

        log_files = list((Path(spec.project_dir) / "logs").glob("slurm_*.out"))
        if len(log_files) >= MAX_AUTO_RESTARTS:
            return None
        new_handle = submit_job(
            spec.project_id, spec.mode, spec.max_iterations,
            spec.project_dir, spec.log_dir, spec.settings,
        )
        return RestartResult(new_handle, attempt=len(log_files))
