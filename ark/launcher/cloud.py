"""Cloud-VM launcher — wraps ``OrchestratorCloudBackend`` behind the
`JobLauncher` seam (Phase 4). Consolidates the cloud launch/poll/teardown logic
that was previously inlined across ``website/dashboard/routes.py`` and
``app.py``, so cloud dispatch works exactly like local/slurm: by config type on
launch, by the ``cloud:{pid}`` handle on poll/cancel.

Behavior is identical to the pre-Phase-4 cloud path: same credential
provisioning, same project/source rsync, same control-plane transport wiring,
same crash-safety-net poll semantics, same VM teardown."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from .base import JobLauncher, LaunchSpec, PollResult, RUNNING, GONE, UNKNOWN

# Failures (unreachable VM, failed teardown) log at ERROR/WARNING under the same
# logger the pre-Phase-4 inline paths used, so existing ERROR-keyed alerting on
# leaked-VM / stuck-run incidents keeps firing. ``self.log`` stays the caller's
# info-level operational callback (passed through to the backend).
_log = logging.getLogger("website.dashboard")


class CloudVmJobLauncher(JobLauncher):
    """Run the orchestrator on a VM in the user's cloud. Handle: ``cloud:{pid}``.

    The remote run shares no filesystem/DB with the control plane, so it reports
    status/state/artifacts home over the /v1 API (Phase 1/3); poll here is a pure
    SSH liveness probe and the DB is authoritative for terminal outcome.

    ``initial_status`` = RUNNING (base default)."""

    def __init__(self, log_fn: Optional[Callable] = None):
        self.log = log_fn or _log.info

    # ── launch ───────────────────────────────────────────────────────────────
    def launch(self, spec: LaunchSpec) -> str:
        from ark.compute import validate_config
        from ark.compute.cloud.orchestrator import OrchestratorCloudBackend
        from website.dashboard.jobs import (
            control_plane_transport, provision_claude_session, provision_gemini_session,
        )

        config = spec.config
        if config is None:
            raise RuntimeError("CloudVmJobLauncher.launch requires spec.config (config.yaml)")
        pdir = Path(spec.project_dir)

        # Write credentials into the project dir so they are rsynced to the VM.
        # The finally covers the whole launch so a setup/sync/validate failure can't
        # leave the plaintext .env (API keys / cloud creds) behind in the project dir.
        env_file_created = self._provision_remote_credentials(
            pdir, spec.api_keys or {},
            provision_claude_session, provision_gemini_session,
        )
        try:
            validate_config(config)
            orch_backend = OrchestratorCloudBackend.from_config(
                config, spec.project_id, pdir, log_fn=self.log
            )
            orch_backend.setup()

            remote_work_dir = f"/home/{orch_backend.ssh_user}/{spec.project_id}"
            if not orch_backend.sync_to_backend(str(pdir), remote_work_dir):
                raise RuntimeError("Failed to sync project directory to Orchestrator VM")

            # Sync the ARK codebase so the VM runs the live code.
            ark_code_root = str(Path(__file__).resolve().parents[2])
            remote_ark_dir = f"/home/{orch_backend.ssh_user}/ark_source"
            if not orch_backend.sync_to_backend(ark_code_root, remote_ark_dir):
                raise RuntimeError("Failed to sync ARK source to Orchestrator VM")

            # Control-plane transport: the VM shares no FS/DB, so it must report over
            # the /v1 API. Without a configured URL the run is blind — warn loudly.
            cp_url, cp_token = control_plane_transport(spec.project_id, spec.settings)
            if not cp_url:
                _log.warning(
                    f"Cloud orchestrator {spec.project_id} launched without a control-plane "
                    f"URL (settings.control_plane_url unset): the remote run cannot report "
                    f"status/state/artifacts back and the dashboard will not see progress."
                )

            pid = orch_backend.run_orchestrator(
                control_plane_url=cp_url, control_plane_token=cp_token
            )
        finally:
            if env_file_created:
                (pdir / ".env").unlink(missing_ok=True)

        if not pid:
            raise RuntimeError("Failed to start remote orchestrator process")
        return f"cloud:{pid}"

    @staticmethod
    def _provision_remote_credentials(pdir, api_keys, provision_claude, provision_gemini) -> bool:
        """Write claude/gemini/gcp creds + a ``.env`` into the project dir for the
        rsync to ``/dev/shm`` on the VM. Returns whether we created ``.env`` (so the
        caller can wipe it after launch). The ``.env`` carries the shared provider
        keys (``api_keys_to_env``); GitHub PAT and the file-based GCP creds are
        local-launch-only (the VM uses its instance service account for GCP)."""
        if not api_keys:
            return False
        from website.dashboard.jobs import api_keys_to_env

        provision_claude(pdir, api_keys)
        provision_gemini(pdir, api_keys)
        gcp_json = api_keys.get("gcp_service_account_json")
        if gcp_json:
            gcp_creds_path = pdir / ".gcp_credentials.json"
            gcp_creds_path.write_text(gcp_json)
            gcp_creds_path.chmod(0o600)
        env_lines = [f"{k}={v}" for k, v in api_keys_to_env(api_keys).items()]
        if env_lines:
            env_file = pdir / ".env"
            if not env_file.exists():
                env_file.write_text("\n".join(env_lines) + "\n")
                env_file.chmod(0o600)
                return True
        return False

    # ── poll ─────────────────────────────────────────────────────────────────
    def poll(self, handle: str, project_dir: Path) -> PollResult:
        import yaml
        from ark.compute.cloud.orchestrator import OrchestratorCloudBackend

        pdir = Path(project_dir)
        config_file = pdir / "config.yaml"
        if not config_file.exists():
            return PollResult(UNKNOWN, "no-config")
        try:
            config = yaml.safe_load(config_file.read_text())
            orch = OrchestratorCloudBackend.from_config(config, pdir.name, pdir, log_fn=self.log)

            # Launcher heartbeat for the VM reaper (Phase 6).
            heartbeat_file = pdir / "auto_research" / "state" / "launcher_heartbeat"
            heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_file.touch()

            raw = orch.poll_orchestrator()
        except Exception as e:  # transient SSH/config error — retry next cycle
            _log.error(f"Cloud orchestrator poll failed for {pdir.name}: {e}")
            return PollResult(UNKNOWN, "poll-error")

        # RUNNING → alive. STOPPED → process gone (DB is authoritative for the
        # outcome; poller applies the crash-safety-net). UNKNOWN → indeterminate.
        state = {"RUNNING": RUNNING, "STOPPED": GONE}.get(raw, UNKNOWN)
        return PollResult(state, raw)

    # ── cancel ───────────────────────────────────────────────────────────────
    def cancel(self, handle: str, project_dir: Path, on_complete=None) -> None:
        """Tear down the orchestrator VM (and any experiment VM) in a background
        thread — the PID lives on the remote VM, so we kill the VM, not a local
        process.

        Teardown reads the project's config.yaml / *_instance.yaml, so ``on_complete``
        (e.g. the delete endpoint's rmtree) must run only *after* teardown finishes,
        or it would delete those files out from under the teardown and leak the VM.
        The two teardowns therefore run sequentially in one thread and on_complete
        fires last."""
        pdir = Path(project_dir)
        project_id = pdir.name

        def _run():
            self._teardown_orchestrator_vm(pdir, project_id)
            self._teardown_experiment_vm(pdir, project_id)
            if on_complete:
                try:
                    on_complete()
                except Exception as e:
                    _log.error(f"cancel on_complete failed for {project_id}: {e}", exc_info=True)

        threading.Thread(target=_run, daemon=True).start()

    def _teardown_orchestrator_vm(self, pdir: Path, project_id: str) -> None:
        import yaml
        from ark.compute.cloud.orchestrator import OrchestratorCloudBackend

        config_file = pdir / "config.yaml"
        if not config_file.exists():
            _log.warning(f"Cannot teardown cloud orchestrator for {project_id}: config.yaml missing")
            return
        try:
            config = yaml.safe_load(config_file.read_text())
            OrchestratorCloudBackend.from_config(
                config, project_id, pdir, log_fn=self.log
            ).teardown()
        except Exception as e:
            _log.error(f"Failed to teardown cloud orchestrator for {project_id}: {e}", exc_info=True)

    def _teardown_experiment_vm(self, pdir: Path, project_id: str) -> None:
        """Tear down a Layer-1 experiment VM if one was provisioned. Not strictly
        a Layer-2 launcher concern, but stopping a cloud project has always torn
        both down together — preserved here for parity."""
        import yaml
        from ark.compute.cloud.base import CloudBackend

        config_file = pdir / "config.yaml"
        state_file = pdir / "auto_research" / "state" / "cloud_instance.yaml"
        if not state_file.exists() or not config_file.exists():
            return
        try:
            config = yaml.safe_load(config_file.read_text())
            CloudBackend.from_config(config, project_id, pdir, log_fn=self.log).teardown()
        except Exception as e:
            _log.error(f"Failed to teardown cloud experiment VM for {project_id}: {e}", exc_info=True)
