import os
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from .gcp import GCPCloudBackend


class OrchestratorCloudBackend(GCPCloudBackend):
    """Cloud backend for running the orchestrator process remotely."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Override the state file to not clash with experiment instances
        self._state_file = self.code_dir / "auto_research" / "state" / "orchestrator_instance.yaml"

    @property
    def _compute_config(self) -> dict:
        return self.config.get("orchestrator_compute_backend", {})

    @classmethod
    def from_config(cls, config: dict, project_name: str, code_dir: Path, log_fn=None) -> "OrchestratorCloudBackend":
        """Factory: build the right orchestrator cloud backend from config."""
        provider = config.get("orchestrator_compute_backend", {}).get("provider", "gcp")
        if provider == "gcp":
            return cls(config, project_name, code_dir, log_fn)
        else:
            raise ValueError(f"Orchestrator cloud backend currently only supports GCP. Requested: {provider}")

    def setup(self) -> dict:
        """Provision instance (or re-attach to existing) and return context."""
        # Phase 6: Try to re-attach to an existing orchestrator VM first
        if self._try_reattach():
            return self._context_dict()
        # Fall through to normal provisioning
        return super().setup()

    def _try_reattach(self) -> bool:
        """
        Re-attachment (Phase 6): if orchestrator_instance.yaml exists, attempt
        to verify the remote VM is still reachable. If yes, skip provisioning.
        Returns True if re-attachment was successful.
        """
        import yaml
        if not self._state_file.exists():
            return False
        try:
            with open(self._state_file) as f:
                state = yaml.safe_load(f) or {}
            instance_id = state.get("instance_id")
            instance_ip = state.get("instance_ip") or state.get("public_ip")
            if not instance_id or not instance_ip:
                return False

            self._instance_id = instance_id
            self._instance_ip = instance_ip
            self.log(f"Re-attaching to existing orchestrator VM: {instance_id} ({instance_ip})")

            # Quick SSH probe to confirm it's still up
            try:
                cmd = self._ssh_cmd_base() + ["-o", "ConnectTimeout=10", "echo ok"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    self.log("Re-attachment successful — VM is reachable.")
                    return True
            except Exception:
                pass

            self.log("Re-attachment failed — VM is unreachable. Provisioning a new one.", "WARN")
            self._instance_id = None
            self._instance_ip = None
            self._clear_instance_state()
            return False
        except Exception as e:
            self.log(f"Re-attachment check error: {e}", "WARN")
            return False

    def _provision(self):
        instance_name = f"ark-orch-{self.project_name}-{int(time.time()) % 10000}"
        labels = self._resource_labels()
        labels_str = ",".join(f"{k}={v}" for k, v in labels.items())

        image_family = self.image_id or "ark-debian-base"
        image_project = self._compute_config.get("image_project", self.gcp_project)
        machine_type = self.instance_type or "n1-standard-2"

        cmd = [
            "gcloud", "compute", "instances", "create", instance_name,
            "--zone", self.region,
            "--machine-type", machine_type,
            "--image-family", image_family,
            "--image-project", image_project,
            "--labels", labels_str,
            "--scopes", "cloud-platform",  # Needed for cross-backend auth via service account
            "--format", "json",
        ]

        # Add SSH keys to metadata
        pub_key_path = Path(os.path.expanduser(self.ssh_key_path)).with_suffix(".pub")
        pub_key_content = None
        if pub_key_path.exists():
            pub_key_content = pub_key_path.read_text().strip()
        else:
            priv_key_path = Path(os.path.expanduser(self.ssh_key_path))
            if priv_key_path.exists():
                try:
                    result = subprocess.run(
                        ["ssh-keygen", "-y", "-f", str(priv_key_path)],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.returncode == 0:
                        pub_key_content = result.stdout.strip()
                except Exception:
                    pass
        if pub_key_content:
            cmd.extend(["--metadata", f"ssh-keys={self.ssh_user}:{pub_key_content}"])

        gcp_project = self.gcp_project or self._compute_config.get("gcp_project")
        if gcp_project:
            cmd.extend(["--project", gcp_project])

        network = self._compute_config.get("network")
        if network:
            cmd.extend(["--network", network])

        subnet = self._compute_config.get("subnet")
        if subnet:
            cmd.extend(["--subnet", subnet])

        with self._gcloud_env() as env:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"GCP Orchestrator provision failed: {result.stderr}")

        data = json.loads(result.stdout)
        self._instance_id = instance_name
        for iface in data[0].get("networkInterfaces", []):
            for access in iface.get("accessConfigs", []):
                if access.get("natIP"):
                    self._instance_ip = access["natIP"]
                    break
        if not self._instance_ip:
            raise RuntimeError("GCP Orchestrator instance has no external IP")
        self.log(f"Orchestrator Instance created: {self._instance_id} ({self._instance_ip})")

    def run_orchestrator(self):
        """Execute the orchestrator in a detached session, start the reaper, and save full state."""
        remote_work_dir = f"/home/{self.ssh_user}/{self.project_name}"
        conda_env = self.conda_env or "ark-base"
        log_rel = "logs/latest.log"  # relative; orchestrator creates the latest.log symlink
        log_file = f"{remote_work_dir}/{log_rel}"
        pid_file = f"{remote_work_dir}/orchestrator.pid"
        reaper_pid_file = f"{remote_work_dir}/reaper.pid"
        reaper_script = f"{remote_work_dir}/ark_vm_reaper.sh"

        # Sync the reaper script to the VM (Phase 6)
        local_reaper = Path(__file__).resolve().parents[3] / "scripts" / "ark_vm_reaper.sh"
        if local_reaper.exists():
            try:
                import os
                key_path = os.path.expanduser(self.ssh_key_path)
                ssh_opts = (
                    f"ssh -o StrictHostKeyChecking=no "
                    f"-o UserKnownHostsFile=/dev/null "
                    f"-o LogLevel=ERROR -i {key_path}"
                )
                subprocess.run([
                    "rsync", "-az", "-e", ssh_opts,
                    str(local_reaper),
                    f"{self.ssh_user}@{self._instance_ip}:{reaper_script}",
                ], check=True, timeout=30)
                self._ssh_exec(f"chmod +x {reaper_script}", timeout=10)
            except Exception as e:
                self.log(f"Reaper sync failed (non-fatal): {e}", "WARN")

        # Ensure the logs dir exists on the remote
        self._ssh_exec(f"mkdir -p {remote_work_dir}/logs {remote_work_dir}/auto_research/state", timeout=10)

        start_cmd = (
            f"cd {remote_work_dir} && "
            f"nohup conda run -n {conda_env} python -m ark.cli run {self.project_name} "
            f"> {log_file} 2>&1 & "
            f"echo $! > {pid_file}"
        )

        self.log(f"Starting remote orchestrator process...")
        self._ssh_exec(start_cmd, timeout=30)

        # Read the PID back to confirm it started
        pid = None
        try:
            pid = self._ssh_exec(f"cat {pid_file}", timeout=10).strip()
            self.log(f"Remote orchestrator started with PID: {pid}")
        except Exception as e:
            self.log(f"Failed to read remote orchestrator PID: {e}", "ERROR")
            return None

        # Start the reaper daemon after the orchestrator (Phase 6)
        if local_reaper.exists():
            reaper_cmd = (
                f"nohup bash {reaper_script} {remote_work_dir} {pid_file} "
                f"> {remote_work_dir}/reaper.log 2>&1 & "
                f"echo $! > {reaper_pid_file}"
            )
            try:
                self._ssh_exec(reaper_cmd, timeout=15)
                reaper_pid = self._ssh_exec(f"cat {reaper_pid_file}", timeout=10).strip()
                self.log(f"Reaper daemon started with PID: {reaper_pid}")
            except Exception as e:
                self.log(f"Reaper start failed (non-fatal): {e}", "WARN")

        # Save full state file (Phase 6 schema from the plan)
        import yaml
        state = {
            "instance_id": self._instance_id,
            "public_ip": self._instance_ip,
            "project_id": self.project_name,
            "orchestrator_pid": pid,
            "launched_at": datetime.utcnow().isoformat() + "Z",
            "log_file": log_rel,
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._state_file, "w") as f:
            yaml.dump(state, f, default_flow_style=False)

        return pid

    def poll_orchestrator(self) -> str:
        """
        Check if the orchestrator process is still running.
        Returns 'RUNNING', 'COMPLETED', 'FAILED', or 'UNKNOWN'.
        """
        import yaml
        if not self._state_file.exists():
            return "UNKNOWN"

        try:
            with open(self._state_file) as f:
                state = yaml.safe_load(f) or {}

            # Re-populate instance info from state file if needed (after webapp restart)
            if not self._instance_ip:
                self._instance_id = state.get("instance_id")
                self._instance_ip = state.get("public_ip") or state.get("instance_ip")

            pid = state.get("orchestrator_pid")
            if not pid:
                return "UNKNOWN"

            # Check if process is running via kill -0
            try:
                result = subprocess.run(
                    self._ssh_cmd_base() + [f"kill -0 {pid}"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    return "RUNNING"
            except Exception:
                pass

            # Process is not running — sync state and determine terminal condition
            remote_work_dir = f"/home/{self.ssh_user}/{self.project_name}"
            try:
                self.sync_from_backend(
                    f"{remote_work_dir}/auto_research/",
                    str(self.code_dir / "auto_research"),
                )
                ps = self.code_dir / "auto_research" / "state" / "paper_state.yaml"
                if ps.exists():
                    d = yaml.safe_load(ps.read_text()) or {}
                    if d.get("status") in ("accepted", "accepted_pending_cleanup"):
                        return "COMPLETED"
            except Exception as e:
                self.log(f"Failed to check completion state: {e}")

            return "FAILED"

        except Exception as e:
            self.log(f"Error polling orchestrator: {e}", "ERROR")
            return "UNKNOWN"

    def teardown(self):
        """Final sync, tear down the GCP orchestrator VM, and clear state."""
        remote_work_dir = f"/home/{self.ssh_user}/{self.project_name}"

        # Final pull before destroying (Phase 6)
        try:
            self.sync_from_backend(
                f"{remote_work_dir}/auto_research/",
                str(self.code_dir / "auto_research"),
            )
            self.sync_from_backend(
                f"{remote_work_dir}/paper/",
                str(self.code_dir / "paper"),
            )
            self.log("Final sync from orchestrator VM completed.")
        except Exception as e:
            self.log(f"Final sync failed (non-fatal): {e}", "WARN")

        # Delegate actual VM termination to GCPCloudBackend
        super().teardown()
