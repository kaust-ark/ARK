import os
import json
import subprocess
import tempfile
import time
from abc import abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from ..base import ComputeBackend

class CloudBackend(ComputeBackend):
    """Cloud compute backend base class with shared logic (SSH, state, etc)."""

    _MARKER_FILE = "/tmp/ark_experiment_done"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cc = self._compute_config
        self.provider = cc.get("provider", "aws")
        
        # Provider-specific region/zone defaults set in subclasses
        self.region = cc.get("region", "")
        self.instance_type = cc.get("instance_type", "")
        self.image_id = cc.get("image_id", "").strip()
        self.ssh_key_name = cc.get("ssh_key_name", "")
        self.ssh_key_path = cc.get("ssh_key_path", "~/.ssh/id_rsa")
        self.ssh_user = cc.get("ssh_user", "ubuntu")
        self.setup_commands = cc.get("setup_commands", [])
        self.conda_env = cc.get("conda_env", self.project_name)
        
        self._instance_id = None
        self._instance_ip = None
        # Persist instance state for crash recovery
        self._state_file = self.code_dir / "auto_research" / "state" / "cloud_instance.yaml"

    @classmethod
    def from_config(cls, config: dict, project_name: str, code_dir: Path, log_fn=None) -> "CloudBackend":
        """Factory: build the right cloud provider backend from config."""
        provider = config.get("compute_backend", {}).get("provider", "aws")
        if provider == "gcp":
            from .gcp import GCPCloudBackend
            return GCPCloudBackend(config, project_name, code_dir, log_fn)
        elif provider == "aws":
            from .aws import AWSCloudBackend
            return AWSCloudBackend(config, project_name, code_dir, log_fn)
        elif provider == "azure":
            from .azure import AzureCloudBackend
            return AzureCloudBackend(config, project_name, code_dir, log_fn)
        else:
            raise ValueError(f"Unknown cloud provider: {provider}")

    def _resource_labels(self) -> dict:
        """Labels to apply to all cloud resources for lifecycle management."""
        import re
        owner = self._compute_config.get("owner", "")
        safe_owner = re.sub(r"[^a-z0-9\-]", "-", owner.lower())[:63].strip("-")
        safe_project = re.sub(r"[^a-z0-9\-]", "-", self.project_name.lower())[:63].strip("-")
        return {
            "managed-by": "ark",
            "project": safe_project,
            "owner": safe_owner or "unknown",
        }

    def setup(self) -> dict:
        """Provision instance, transfer code, run setup commands."""
        # Check for orphaned instance from previous run
        self._recover_orphaned_instance()
        if self._instance_id and self._instance_ip:
            self.log(f"Reusing existing instance {self._instance_id} ({self._instance_ip})")
            return self._context_dict()

        self.log(f"Provisioning {self.provider.upper()} instance ({self.instance_type})...", "INFO")
        self._provision()

        self._wait_for_ssh()

        for cmd in self.setup_commands:
            self.log(f"Setup: {cmd[:60]}...", "INFO")
            self._ssh_exec(cmd, timeout=600)

        self._save_instance_state()
        return self._context_dict()

    @abstractmethod
    def _provision(self):
        """Provider-specific provisioning logic."""
        pass

    def _context_dict(self) -> dict:
        return {
            "ssh_host": self._instance_ip,
            "ssh_user": self.ssh_user,
            "work_dir": f"/home/{self.ssh_user}/{self.project_name}",
            "instance_id": self._instance_id,
        }

    def _ssh_cmd_base(self) -> list:
        key_path = os.path.expanduser(self.ssh_key_path)
        return [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-i", key_path,
            f"{self.ssh_user}@{self._instance_ip}",
        ]

    def _ssh_exec(self, command: str, timeout: int = 600) -> str:
        cmd = self._ssh_cmd_base() + [command]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout

    def _wait_for_ssh(self, max_retries: int = 30, delay: int = 10):
        """Poll until SSH is available."""
        self.log("Waiting for SSH readiness...")
        for i in range(max_retries):
            try:
                cmd = self._ssh_cmd_base() + ["-o", "ConnectTimeout=5", "echo ready"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    self.log("SSH ready")
                    return
            except Exception:
                pass
            time.sleep(delay)
        raise RuntimeError(f"SSH to {self._instance_ip} timed out after {max_retries * delay}s")

    def sync_to_backend(self, source_dir: str, remote_dir: str) -> bool:
        """Push local project files to the compute backend."""
        if not self._instance_ip:
            return False

        key_path = os.path.expanduser(self.ssh_key_path)
        dest = f"{self.ssh_user}@{self._instance_ip}:{remote_dir}"
        ssh_opts = (f"ssh -o StrictHostKeyChecking=no "
                    f"-o UserKnownHostsFile=/dev/null "
                    f"-o LogLevel=ERROR -i {key_path}")
        try:
            subprocess.run([
                "rsync", "-azL",
                "--exclude", ".git",
                "--exclude", "__pycache__",
                "--exclude", "*.pyc",
                "--exclude", "auto_research",
                "-e", ssh_opts,
                f"{source_dir}/", dest,
            ], check=True, timeout=300)
            self.log(f"Synced {source_dir} to remote instance")
            return True
        except Exception as e:
            self.log(f"Sync to remote failed: {e}", "ERROR")
            return False

    def _save_instance_state(self):
        import yaml
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "provider": self.provider,
            "instance_id": self._instance_id,
            "instance_ip": self._instance_ip,
            "region": self.region,
            "created_at": datetime.now().isoformat(),
        }
        with open(self._state_file, "w") as f:
            yaml.dump(state, f, default_flow_style=False)

    def _clear_instance_state(self):
        if self._state_file.exists():
            self._state_file.unlink()

    def _recover_orphaned_instance(self):
        import yaml
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file) as f:
                state = yaml.safe_load(f)
            if state and state.get("instance_id"):
                self._instance_id = state["instance_id"]
                self._instance_ip = state.get("instance_ip")
                self.log(f"Found orphaned instance: {self._instance_id}", "WARN")
        except Exception:
            pass

    def get_agent_instructions(self) -> str:
        return f"""## Compute Environment: Cloud ({self.provider.upper()})

A cloud instance has been provisioned for your experiments:
- SSH: `ssh -i {self.ssh_key_path} {self.ssh_user}@{self._instance_ip}`
- Working directory: `/home/{self.ssh_user}/{self.project_name}`
- Conda environment: `{self.conda_env}`

**Important**:
1. SSH into the instance to run experiments
2. Save results to the `results/` directory on the remote machine
3. When ALL experiments are done, run: `touch {self._MARKER_FILE}`
4. The system will automatically collect results and terminate the instance

Do NOT use sbatch/srun. Run scripts directly on the instance."""

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Poll for marker file or process completion via SSH."""
        max_wait = timedelta(hours=max_wait_hours)
        start_time = datetime.now()

        while datetime.now() - start_time < max_wait:
            try:
                output = self._ssh_exec(
                    f"test -f {self._MARKER_FILE} && echo DONE || echo RUNNING",
                    timeout=30,
                )
                if "DONE" in output:
                    self.log("Cloud experiment completed (marker file found)")
                    return True

                ps = self._ssh_exec(
                    "pgrep -af 'python|train' | grep -v pgrep | head -5",
                    timeout=30,
                )
                if not ps.strip():
                    self.log("No running experiment processes detected")
                    return True

                self.log(f"Cloud experiments running, waiting 60s...")
                time.sleep(60)
            except Exception as e:
                self.log(f"SSH check failed: {e}, retrying...", "WARN")
                time.sleep(60)

        self.log(f"Cloud wait timeout after {max_wait_hours} hours", "WARN")
        return False

    def sync_from_backend(self, remote_dir: str, dest_dir: str) -> bool:
        """Pull results from the compute backend back to the orchestrator."""
        if not self._instance_ip:
            return False

        key_path = os.path.expanduser(self.ssh_key_path)
        source = f"{self.ssh_user}@{self._instance_ip}:{remote_dir}/"
        Path(dest_dir).mkdir(exist_ok=True, parents=True)

        ssh_opts = (f"ssh -o StrictHostKeyChecking=no "
                    f"-o UserKnownHostsFile=/dev/null "
                    f"-o LogLevel=ERROR -i {key_path}")
        try:
            subprocess.run([
                "rsync", "-az",
                "-e", ssh_opts,
                source, f"{dest_dir}/",
            ], check=True, timeout=300)
            self.log(f"Synced from remote instance to {dest_dir}")
            return True
        except Exception as e:
            self.log(f"Sync from remote failed: {e}", "ERROR")
            return False

    @abstractmethod
    def teardown(self):
        """Terminate the cloud instance."""
        pass
