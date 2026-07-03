"""Layer-1 SkyPilot experiment backend (folded Phases 5+6, ADR-0010, PR2).

This is a *fresh implementation* of the ``ComputeBackend`` seam on SkyPilot's
task model — **not** an adapter over ``CloudBackend`` (SSH + rsync + gcloud). An
experiment run maps onto SkyPilot as:

- ``setup()``      → build a ``sky.Task`` (code as ``workdir``, deps as the
                     ``setup:`` block, cloud/accelerators/spot as ``Resources``)
                     and ``sky.launch`` it onto a named cluster;
- the experimenter → SSHes into the cluster (SkyPilot writes an ``ssh <cluster>``
                     alias) and runs experiments, touching a completion marker;
- ``wait_for_completion`` → polls that marker over the SSH alias;
- ``sync_from_backend``   → rsyncs results back over the SSH alias;
- ``teardown()``   → ``sky.down`` (or reuse the cluster's autostop, PR4).

The GCP ``cloud`` path stays default and untouched; ``type: skypilot`` is
additive and default-off. The ``sky`` SDK is imported lazily (``_sky.load_sky``)
so importing this module never requires the ``skypilot`` extra — only launching
a cluster does.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from .base import ComputeBackend
from ._sky import (
    load_sky, block_on_request, resolve_request_value, resolve_cloud,
    build_resources, cluster_name, setup_script,
)

# Where SkyPilot lands a Task's ``workdir`` on the remote head node — relative
# to $HOME, so it resolves correctly for any cloud's default SSH user.
_REMOTE_WORKDIR = "sky_workdir"


class SkyPilotBackend(ComputeBackend):
    """Run experiments on a SkyPilot-provisioned cluster (any cloud or K8s)."""

    # Completion signal the experimenter touches when all experiments finish;
    # mirrors the CloudBackend marker contract so the agent-facing protocol is
    # identical across backends.
    _MARKER_FILE = "/tmp/ark_experiment_done"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cc = self._compute_config
        # Resource selection — all optional; SkyPilot infers/optimizes the rest.
        self.cloud = (cc.get("cloud") or "").strip()          # aws/gcp/azure/kubernetes; "" → auto
        self.region = (cc.get("region") or "").strip()
        self.accelerators = (cc.get("accelerators") or "").strip()  # e.g. "A100:1"
        self.instance_type = (cc.get("instance_type") or "").strip()
        self.use_spot = bool(cc.get("use_spot", False))
        self.disk_size = cc.get("disk_size")                  # GB, optional
        self.image_id = (cc.get("image_id") or "").strip()
        self.setup_commands = cc.get("setup_commands", [])
        self.conda_env = cc.get("conda_env", self.project_name)
        self.cluster_name = cc.get("cluster_name") or self._default_cluster_name()

        # Persist the cluster name so a crashed/re-run orchestrator can reuse or
        # tear down the same cluster (mirrors CloudBackend's state file).
        self._state_file = self.code_dir / "auto_research" / "state" / "skypilot_cluster.yaml"
        self._launched = False

    def _default_cluster_name(self) -> str:
        # Stable DNS-ish name per project (shared sanitizer with the Layer-2
        # launcher) so a re-run reconnects to (not duplicates) the cluster.
        return cluster_name("ark-", self.project_name)

    # ------------------------------------------------------------------ setup

    def setup(self) -> dict:
        """Provision (or reuse) the cluster and sync code onto it."""
        self._recover_cluster_state()

        sky = load_sky()
        if self._cluster_is_up(sky):
            self.log(f"Reusing existing SkyPilot cluster '{self.cluster_name}'")
            self._launched = True
            atexit.register(self.teardown)
            return self._context_dict()

        # Orchestrator-autonomous, billable action — gate it when the
        # orchestrator has wired an intervention check (no-op otherwise). Reuses
        # the existing "cloud_provision" action so one policy covers both paths.
        check = getattr(self, "_intervention_check", None)
        if check is not None and not check(
                "cloud_provision", provider=f"skypilot:{self.cloud or 'auto'}",
                instance_type=self.instance_type or self.accelerators or "default"):
            raise RuntimeError("SkyPilot provisioning denied by intervention policy")

        task = self._build_task(sky)
        self.log(
            f"Launching SkyPilot cluster '{self.cluster_name}' "
            f"(cloud={self.cloud or 'auto'}, accelerators={self.accelerators or 'none'}, "
            f"spot={self.use_spot})...",
            "INFO",
        )
        self._launch(sky, task)
        self._launched = True
        self._save_cluster_state()
        atexit.register(self.teardown)
        return self._context_dict()

    def _build_task(self, sky):
        task = sky.Task(
            name=self.cluster_name,
            setup=self._setup_script() or None,
            # A no-op run: the cluster comes up idle and the experimenter drives
            # experiments interactively over SSH (mirroring the cloud model),
            # rather than baking one command into the task.
            run="echo 'ARK SkyPilot cluster ready'",
            workdir=str(self.code_dir),
        )
        task.set_resources(self._build_resources(sky))
        return task

    def _setup_script(self) -> str:
        return setup_script(self.setup_commands)

    def _build_resources(self, sky):
        # Resource shaping is shared with the Layer-2 launcher (``_sky``); reads
        # the same keys off this backend's ``experiment_compute_backend`` block.
        return build_resources(sky, self._compute_config)

    def _resolve_cloud(self, sky):
        """Map this backend's config cloud string to a SkyPilot Cloud object."""
        return resolve_cloud(sky, self.cloud)

    def _launch(self, sky, task):
        # Provision + run setup. retry_until_up rides out transient capacity
        # errors (spot pre-emption, quota races) the way the GCP path retries.
        result = sky.launch(task, cluster_name=self.cluster_name, retry_until_up=True)
        # SkyPilot's client/server API (newer releases) returns an async request
        # id; block on it. Older releases run synchronously and return a tuple.
        block_on_request(sky, result)

    # ------------------------------------------------------------ agent-facing

    def get_agent_instructions(self) -> str:
        return f"""## Compute Environment: SkyPilot cluster (`{self.cluster_name}`)

A SkyPilot cluster has been provisioned for your experiments. SkyPilot has
written an SSH alias, so you can reach it directly by cluster name:
- SSH: `ssh {self.cluster_name}`
- Run a command remotely: `ssh {self.cluster_name} '<command>'`
- Working directory (your synced code): `~/{_REMOTE_WORKDIR}`
- Conda environment: `{self.conda_env}`

**Important**:
1. SSH into the cluster to run experiments (do NOT use sbatch/srun).
2. Save results to `~/{_REMOTE_WORKDIR}/results/` on the remote cluster.
3. When ALL experiments are done, run: `ssh {self.cluster_name} 'touch {self._MARKER_FILE}'`
4. The system will then collect results and tear the cluster down."""

    # ----------------------------------------------------------------- polling

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Poll the completion marker over SkyPilot's SSH alias."""
        max_wait = timedelta(hours=max_wait_hours)
        start = datetime.now()
        while datetime.now() - start < max_wait:
            try:
                out = self._ssh_exec(
                    f"test -f {self._MARKER_FILE} && echo DONE || echo RUNNING", timeout=30)
                if "DONE" in out:
                    self.log("SkyPilot experiment completed (marker file found)")
                    return True

                ps = self._ssh_exec(
                    "pgrep -af 'python|train' | grep -v pgrep "
                    "| grep -v networkd-dispatcher | grep -v unattended-upgrade "
                    "| head -5", timeout=30)
                if not ps.strip():
                    # Processes gone but no marker: either the marker write is
                    # racing this check, or the experiment crashed. Re-check
                    # after a short grace; if still absent, fail fast so the
                    # caller doesn't consume garbage results.
                    time.sleep(5)
                    recheck = self._ssh_exec(
                        f"test -f {self._MARKER_FILE} && echo DONE || echo CRASHED", timeout=30)
                    if "DONE" in recheck:
                        self.log("SkyPilot experiment completed (marker found after process exit)")
                        return True
                    self.log(
                        "Experiment processes exited without writing completion "
                        "marker — treating as crash", "ERROR")
                    return False

                self.log(f"SkyPilot experiments running (processes):\n{ps.strip()}")
                time.sleep(60)
            except Exception as e:
                self.log(f"SSH check failed: {e}, retrying...", "WARN")
                time.sleep(60)

        self.log(f"SkyPilot wait timeout after {max_wait_hours} hours", "WARN")
        return False

    # -------------------------------------------------------------------- sync

    def _context_dict(self) -> dict:
        return {
            "cluster_name": self.cluster_name,
            "work_dir": _REMOTE_WORKDIR,
            "ssh_host": self.cluster_name,
        }

    # Single source of the SSH hardening flags, shared by the interactive `ssh`
    # command (list form) and rsync's `-e` transport (string form) so the two
    # paths can never drift apart.
    _SSH_OPTS = (
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    )

    def _ssh_base(self) -> list:
        # SkyPilot maintains an ~/.ssh config alias for the cluster name, so a
        # bare `ssh <cluster>` (and rsync -e ssh) authenticates with no key/IP
        # plumbing on our side.
        return ["ssh", *self._SSH_OPTS, self.cluster_name]

    def _ssh_opts_str(self) -> str:
        return "ssh " + " ".join(self._SSH_OPTS)

    def _ssh_exec(self, command: str, timeout: int = 600) -> str:
        result = subprocess.run(
            self._ssh_base() + [command], capture_output=True, text=True, timeout=timeout)
        return result.stdout

    def sync_to_backend(self, source_dir: str, remote_dir: str) -> bool:
        """Push local project files onto the cluster over the SSH alias.

        SkyPilot's ``workdir`` already synced the code at launch; this keeps it
        fresh for anything that changed between provisioning and the run."""
        if not self._launched:
            return False
        dest = f"{self.cluster_name}:{remote_dir}/"
        try:
            # `--rsync-path` creates any missing parents on the remote in the
            # same session as the transfer — no extra SSH round-trip, and safe
            # for a nested remote_dir (rsync alone only creates the final leaf).
            subprocess.run([
                "rsync", "-az",
                "--rsync-path", f"mkdir -p {remote_dir} && rsync",
                "--exclude", ".git", "--exclude", "__pycache__",
                "--exclude", "*.pyc", "--exclude", "projects", "--exclude", ".env",
                "-e", self._ssh_opts_str(),
                f"{source_dir}/", dest,
            ], check=True, timeout=300)
            self.log(f"Synced {source_dir} to cluster '{self.cluster_name}'")
            return True
        except Exception as e:
            self.log(f"Sync to cluster failed: {e}", "ERROR")
            return False

    def sync_from_backend(self, remote_dir: str, dest_dir: str) -> bool:
        """Pull results off the cluster over the SSH alias."""
        if not self._launched:
            return False
        source = f"{self.cluster_name}:{remote_dir}/"
        Path(dest_dir).mkdir(exist_ok=True, parents=True)
        try:
            subprocess.run([
                "rsync", "-az", "-e", self._ssh_opts_str(), source, f"{dest_dir}/",
            ], check=True, timeout=300)
            self.log(f"Synced results from cluster '{self.cluster_name}' to {dest_dir}")
            return True
        except Exception as e:
            self.log(f"Sync from cluster failed: {e}", "ERROR")
            return False

    # ---------------------------------------------------------------- teardown

    def teardown(self):
        if not self._launched:
            return
        self.log(f"Tearing down SkyPilot cluster '{self.cluster_name}'...")
        try:
            sky = load_sky()
            result = sky.down(self.cluster_name)
            block_on_request(sky, result)
            self.log(f"Cluster '{self.cluster_name}' torn down")
        except Exception as e:
            self.log(f"Failed to tear down cluster '{self.cluster_name}': {e}", "ERROR")
        finally:
            self._launched = False
            self._clear_cluster_state()

    # ------------------------------------------------------------- cluster state

    def _cluster_is_up(self, sky) -> bool:
        """Best-effort: True if a cluster of this name already reports UP."""
        status_fn = getattr(sky, "status", None)
        if status_fn is None:
            return False
        try:
            records = status_fn(cluster_names=[self.cluster_name])
            records = resolve_request_value(sky, records)
            for rec in records or []:
                status = rec.get("status")
                # SkyPilot exposes status as an enum; compare by name to avoid
                # importing its internal ClusterStatus type.
                if getattr(status, "name", str(status)).upper().endswith("UP"):
                    return True
        except Exception:
            pass
        return False

    def _save_cluster_state(self):
        import yaml
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "cluster_name": self.cluster_name,
            "cloud": self.cloud,
            "created_at": datetime.now().isoformat(),
        }
        with open(self._state_file, "w") as f:
            yaml.dump(state, f, default_flow_style=False)

    def _clear_cluster_state(self):
        if self._state_file.exists():
            try:
                self._state_file.unlink()
            except Exception:
                pass

    def _recover_cluster_state(self):
        import yaml
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file) as f:
                state = yaml.safe_load(f)
            if state and state.get("cluster_name"):
                self.cluster_name = state["cluster_name"]
                self.log(f"Found persisted SkyPilot cluster: {self.cluster_name}", "WARN")
        except Exception:
            pass
