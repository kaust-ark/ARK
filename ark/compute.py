"""Compute backend abstraction for experiment execution.

Supports: Slurm HPC (MCNodes), Local, Cloud (AWS/GCP/Azure), Custom.
The agent designs experiments & writes scripts; the system manages compute lifecycle.
"""

import json
import os
import subprocess
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path


class ComputeBackend(ABC):
    """Base class for all compute backends."""

    def __init__(self, config: dict, project_name: str, code_dir: Path, log_fn=None):
        self.config = config
        self.project_name = project_name
        self.code_dir = Path(code_dir)
        self.log = log_fn or (lambda msg, level="INFO": print(f"[{level}] {msg}"))

    @property
    def _compute_config(self) -> dict:
        return self.config.get("compute_backend", {})

    @abstractmethod
    def setup(self) -> dict:
        """Pre-experiment provisioning. Returns context dict."""
        pass

    @abstractmethod
    def get_agent_instructions(self) -> str:
        """Return instructions to inject into experimenter agent prompt."""
        pass

    @abstractmethod
    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Block until experiments complete. Returns True if completed."""
        pass

    def collect_results(self) -> bool:
        """Collect results from remote to local. Default: no-op."""
        return True

    def teardown(self):
        """Post-experiment cleanup. Default: no-op."""
        pass

    @classmethod
    def from_config(cls, config: dict, project_name: str,
                    code_dir, log_fn=None) -> "ComputeBackend":
        """Factory: build the right backend from config."""
        compute = config.get("compute_backend", {})

        # Backward compatibility: old use_slurm boolean
        if not compute:
            if config.get("use_slurm", False):
                compute = {
                    "type": "slurm",
                    "job_prefix": config.get("slurm_job_prefix",
                                             f"{project_name.upper()}_"),
                    "conda_env": config.get("conda_env", project_name),
                }
            else:
                compute = {
                    "type": "local",
                    "conda_env": config.get("conda_env", project_name),
                }

        backend_type = compute.get("type", "local")

        if backend_type == "slurm":
            return SlurmBackend(config, project_name, code_dir, log_fn)
        elif backend_type == "local":
            return LocalBackend(config, project_name, code_dir, log_fn)
        elif backend_type == "cloud":
            return CloudBackend(config, project_name, code_dir, log_fn)
        elif backend_type == "custom":
            return CustomBackend(config, project_name, code_dir, log_fn)
        else:
            raise ValueError(f"Unknown compute backend: {backend_type}")


# ============================================================
#  Slurm HPC Backend
# ============================================================

class SlurmBackend(ComputeBackend):
    """MCNodes / Slurm HPC backend."""

    @property
    def job_prefix(self) -> str:
        return (self._compute_config.get("job_prefix")
                or self.config.get("slurm_job_prefix")
                or f"{self.project_name.upper()}_")

    @property
    def conda_env(self) -> str:
        explicit = (self._compute_config.get("conda_env")
                    or self.config.get("conda_env"))
        if explicit:
            return explicit
        local_env = self.code_dir / ".env"
        if (local_env / "conda-meta").is_dir():
            return str(local_env)
        return self.project_name

    @property
    def slurm_template(self) -> str:
        return self._compute_config.get("slurm_template", "")

    def setup(self) -> dict:
        """Auto-discover cluster info via sinfo/sacctmgr."""
        ctx = {}
        try:
            result = subprocess.run(
                ["sinfo", "-o", "%P %G %c %m %a"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                ctx["cluster_info"] = result.stdout.strip()
                self.log(f"Cluster discovered: {len(result.stdout.strip().splitlines())} partitions")
        except Exception as e:
            self.log(f"sinfo discovery failed: {e}", "WARN")

        try:
            result = subprocess.run(
                ["sacctmgr", "show", "assoc",
                 f"user={os.environ.get('USER', '')}",
                 "format=Account", "-n"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                ctx["slurm_account"] = result.stdout.strip().split("\n")[0].strip()
        except Exception:
            pass

        return ctx

    def get_agent_instructions(self) -> str:
        template_section = ""
        if self.slurm_template:
            path = self.code_dir / self.slurm_template
            if path.exists():
                template_section = (
                    f"\n\n## Slurm Template\n\n"
                    f"Use this as a base for your .slurm scripts:\n"
                    f"```\n{path.read_text()}\n```"
                )

        return f"""## Compute Environment: Slurm HPC

Use Slurm to submit GPU jobs. Key settings:
- Job name prefix: `{self.job_prefix}`
- Conda environment: `{self.conda_env}`
- Activate before running: `conda activate {self.conda_env}`

Submit jobs using `sbatch`. Name all jobs with prefix `{self.job_prefix}` so the
system can track them (e.g., `#SBATCH --job-name={self.job_prefix}experiment_1`).

Save all results to the `results/` directory.{template_section}"""

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Poll squeue for jobs with matching prefix."""
        max_wait = timedelta(hours=max_wait_hours)
        start_time = datetime.now()

        while datetime.now() - start_time < max_wait:
            try:
                result = subprocess.run(
                    ["squeue", "-u", os.environ.get("USER"), "-h", "-o", "%j"],
                    capture_output=True, text=True, timeout=30,
                )
                if not result.stdout.strip():
                    self.log("No Slurm jobs found")
                    return True

                all_jobs = result.stdout.strip().split("\n")
                target_jobs = [j for j in all_jobs if j.startswith(self.job_prefix)]

                if len(target_jobs) == 0:
                    self.log(f"All {self.job_prefix}* jobs completed "
                             f"(other jobs: {len(all_jobs)})")
                    return True

                self.log(f"{len(target_jobs)} {self.job_prefix}* jobs running, "
                         f"waiting 60s...")
                time.sleep(60)

            except Exception as e:
                self.log(f"Error checking Slurm: {e}")
                time.sleep(60)

        self.log(f"Slurm wait timeout after {max_wait_hours} hours")
        return False


# ============================================================
#  Local Backend
# ============================================================

class LocalBackend(ComputeBackend):
    """Run experiments directly on the local machine."""

    @property
    def conda_env(self) -> str:
        explicit = (self._compute_config.get("conda_env")
                    or self.config.get("conda_env"))
        if explicit:
            return explicit
        # Per-project env created by the webapp at <code_dir>/.env.
        local_env = self.code_dir / ".env"
        if (local_env / "conda-meta").is_dir():
            return str(local_env)
        return self.project_name

    @property
    def gpu_count(self) -> int:
        return self._compute_config.get("gpu_count", 0)

    def setup(self) -> dict:
        ctx = {}
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                ctx["gpu_info"] = result.stdout.strip()
                ctx["gpu_count"] = len(result.stdout.strip().split("\n"))
        except Exception:
            ctx["gpu_count"] = 0
        return ctx

    def get_agent_instructions(self) -> str:
        gpu_section = ""
        if self.gpu_count > 0:
            gpu_section = f"\n- Available GPUs: {self.gpu_count}"

        return f"""## Compute Environment: Local Machine

Run experiments directly on this machine. Key settings:
- Conda environment: `{self.conda_env}`
- Activate before running: `conda activate {self.conda_env}`{gpu_section}

Run scripts directly (e.g., `python train.py`). Do NOT use sbatch/srun.
Use `nohup` or background processes for long-running tasks.
Save all results to the `results/` directory."""

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Check results/ dir for recent files."""
        scripts_dir = self.config.get("scripts_dir", "scripts")
        possible_dirs = [
            self.code_dir / "results",
            self.code_dir / scripts_dir / "results",
        ]

        results_dir = None
        for d in possible_dirs:
            if d.exists():
                results_dir = d
                break

        if not results_dir:
            self.log("Results directory not found, assuming done.", "WARN")
            return True

        current_time = time.time()
        recent_files = []
        try:
            for file in results_dir.rglob("*"):
                if file.is_file():
                    if current_time - file.stat().st_mtime < max_wait_hours * 3600:
                        recent_files.append(file.name)
        except Exception as e:
            self.log(f"Error checking results: {e}", "WARN")
            return True

        if recent_files:
            self.log(f"Found {len(recent_files)} recent result files.")
            for f in sorted(recent_files)[:5]:
                self.log(f"  - {f}")
            return True
        else:
            self.log("No recent experiment results found.", "WARN")
            return True


# ============================================================
#  Cloud Backend (AWS / GCP / Azure)
# ============================================================

class CloudBackend(ComputeBackend):
    """Cloud compute backend using CLI tools (no SDK dependencies)."""

    _MARKER_FILE = "/tmp/ark_experiment_done"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cc = self._compute_config
        self.provider = cc.get("provider", "aws")
        self.region = cc.get("region", "us-east-1")
        self.instance_type = cc.get("instance_type", "")
        self.image_id = cc.get("image_id", "")
        self.ssh_key_name = cc.get("ssh_key_name", "")
        self.ssh_key_path = cc.get("ssh_key_path", "~/.ssh/id_rsa")
        self.ssh_user = cc.get("ssh_user", "ubuntu")
        self.setup_commands = cc.get("setup_commands", [])
        self.conda_env = cc.get("conda_env", self.project_name)
        self._instance_id = None
        self._instance_ip = None
        # Persist instance state for crash recovery
        self._state_file = self.code_dir / "auto_research" / "state" / "cloud_instance.yaml"

    def _resource_labels(self) -> dict:
        """Labels to apply to all cloud resources for lifecycle management."""
        import re
        owner = self._compute_config.get("owner", "")
        # Sanitize for GCP label constraints (lowercase, alphanumeric/hyphens only)
        safe_owner = re.sub(r"[^a-z0-9\-]", "-", owner.lower())[:63].strip("-")
        safe_project = re.sub(r"[^a-z0-9\-]", "-", self.project_name.lower())[:63].strip("-")
        return {
            "managed-by": "ark",
            "project": safe_project,
            "owner": safe_owner or "unknown",
        }

    # ── Provisioning ──

    def setup(self) -> dict:
        """Provision a cloud instance, transfer code, run setup commands."""
        # Check for orphaned instance from previous run
        self._recover_orphaned_instance()
        if self._instance_id and self._instance_ip:
            self.log(f"Reusing existing instance {self._instance_id} "
                     f"({self._instance_ip})")
            return self._context_dict()

        self.log(f"Provisioning {self.provider.upper()} instance "
                 f"({self.instance_type})...", "INFO")

        if self.provider == "aws":
            self._provision_aws()
        elif self.provider == "gcp":
            self._provision_gcp()
        elif self.provider == "azure":
            self._provision_azure()
        else:
            raise ValueError(f"Unknown cloud provider: {self.provider}")

        # Wait for SSH
        self._wait_for_ssh()

        # Transfer code
        self._transfer_code()

        # Run setup commands
        for cmd in self.setup_commands:
            self.log(f"Setup: {cmd[:60]}...", "INFO")
            self._ssh_exec(cmd, timeout=600)

        # Save instance state for crash recovery
        self._save_instance_state()

        return self._context_dict()

    def _context_dict(self) -> dict:
        return {
            "ssh_host": self._instance_ip,
            "ssh_user": self.ssh_user,
            "work_dir": f"/home/{self.ssh_user}/{self.project_name}",
            "instance_id": self._instance_id,
        }

    def _provision_aws(self):
        labels = self._resource_labels()
        tag_specs = "ResourceType=instance,Tags=[" + ",".join(
            f"{{Key={k},Value={v}}}" for k, v in labels.items()
        ) + "]"
        cmd = [
            "aws", "ec2", "run-instances",
            "--image-id", self.image_id,
            "--instance-type", self.instance_type,
            "--key-name", self.ssh_key_name,
            "--region", self.region,
            "--count", "1",
            "--tag-specifications", tag_specs,
            "--output", "json",
        ]
        sg = self._compute_config.get("security_group")
        if sg:
            cmd.extend(["--security-group-ids", sg])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"AWS provision failed: {result.stderr}")

        data = json.loads(result.stdout)
        self._instance_id = data["Instances"][0]["InstanceId"]
        self.log(f"Instance created: {self._instance_id}")

        # Wait for running
        subprocess.run([
            "aws", "ec2", "wait", "instance-running",
            "--instance-ids", self._instance_id,
            "--region", self.region,
        ], timeout=300)

        # Get public IP
        ip_result = subprocess.run([
            "aws", "ec2", "describe-instances",
            "--instance-ids", self._instance_id,
            "--region", self.region,
            "--query", "Reservations[0].Instances[0].PublicIpAddress",
            "--output", "text",
        ], capture_output=True, text=True, timeout=30)
        self._instance_ip = ip_result.stdout.strip()
        self.log(f"Instance IP: {self._instance_ip}")

    def _provision_gcp(self):
        instance_name = f"ark-{self.project_name}-{int(time.time()) % 10000}"
        labels = self._resource_labels()
        labels_str = ",".join(f"{k}={v}" for k, v in labels.items())
        cmd = [
            "gcloud", "compute", "instances", "create", instance_name,
            "--zone", self.region,
            "--machine-type", self.instance_type,
            "--image-family", self.image_id,
            "--image-project", "deeplearning-platform-release",
            "--labels", labels_str,
            "--format", "json",
        ]
        accelerator = self._compute_config.get("accelerator_type")
        if accelerator:
            count = self._compute_config.get("accelerator_count", 1)
            cmd.extend([
                "--accelerator", f"type={accelerator},count={count}",
                "--maintenance-policy", "TERMINATE",
            ])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"GCP provision failed: {result.stderr}")

        data = json.loads(result.stdout)
        self._instance_id = instance_name
        # Get external IP
        for iface in data[0].get("networkInterfaces", []):
            for access in iface.get("accessConfigs", []):
                if access.get("natIP"):
                    self._instance_ip = access["natIP"]
                    break
        if not self._instance_ip:
            raise RuntimeError("GCP instance has no external IP")
        self.log(f"Instance created: {self._instance_id} ({self._instance_ip})")

    def _provision_azure(self):
        rg = self._compute_config.get("resource_group", f"ark-{self.project_name}")
        instance_name = f"ark-{self.project_name}-{int(time.time()) % 10000}"
        labels = self._resource_labels()

        # Ensure resource group exists
        subprocess.run([
            "az", "group", "create",
            "--name", rg,
            "--location", self.region,
            "--tags", *[f"{k}={v}" for k, v in labels.items()],
        ], capture_output=True, timeout=60)

        cmd = [
            "az", "vm", "create",
            "--resource-group", rg,
            "--name", instance_name,
            "--location", self.region,
            "--size", self.instance_type,
            "--image", self.image_id,
            "--admin-username", self.ssh_user,
            "--ssh-key-values", os.path.expanduser(self.ssh_key_path) + ".pub",
            "--tags", *[f"{k}={v}" for k, v in labels.items()],
            "--output", "json",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"Azure provision failed: {result.stderr}")

        data = json.loads(result.stdout)
        self._instance_id = data.get("id", instance_name)
        self._instance_ip = data.get("publicIpAddress", "")
        if not self._instance_ip:
            raise RuntimeError("Azure VM has no public IP")
        self.log(f"VM created: {instance_name} ({self._instance_ip})")

    # ── SSH & File Transfer ──

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
        raise RuntimeError(f"SSH to {self._instance_ip} timed out after "
                           f"{max_retries * delay}s")

    def _transfer_code(self):
        """rsync code to the remote instance."""
        key_path = os.path.expanduser(self.ssh_key_path)
        remote_dir = (f"{self.ssh_user}@{self._instance_ip}:"
                      f"/home/{self.ssh_user}/{self.project_name}")
        ssh_opts = (f"ssh -o StrictHostKeyChecking=no "
                    f"-o UserKnownHostsFile=/dev/null "
                    f"-o LogLevel=ERROR -i {key_path}")
        subprocess.run([
            "rsync", "-az",
            "--exclude", ".git",
            "--exclude", "__pycache__",
            "--exclude", "*.pyc",
            "--exclude", "auto_research",
            "-e", ssh_opts,
            f"{self.code_dir}/", remote_dir,
        ], check=True, timeout=300)
        self.log("Code transferred to remote instance")

    # ── Instance State Persistence (crash recovery) ──

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
        """Check for orphaned instance from a previous crashed run."""
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

    # ── Agent Instructions ──

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

    # ── Wait & Collect ──

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

                # Check for running experiment processes
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

    def collect_results(self) -> bool:
        """rsync results back from remote instance."""
        if not self._instance_ip:
            return False

        key_path = os.path.expanduser(self.ssh_key_path)
        remote_results = (f"{self.ssh_user}@{self._instance_ip}:"
                          f"/home/{self.ssh_user}/{self.project_name}/results/")
        local_results = self.code_dir / "results"
        local_results.mkdir(exist_ok=True)

        ssh_opts = (f"ssh -o StrictHostKeyChecking=no "
                    f"-o UserKnownHostsFile=/dev/null "
                    f"-o LogLevel=ERROR -i {key_path}")
        try:
            subprocess.run([
                "rsync", "-az",
                "-e", ssh_opts,
                remote_results, str(local_results) + "/",
            ], check=True, timeout=300)
            self.log("Results collected from cloud instance")
            return True
        except Exception as e:
            self.log(f"Result collection failed: {e}", "ERROR")
            return False

    def teardown(self):
        """Terminate the cloud instance."""
        if not self._instance_id:
            return

        self.log(f"Terminating {self.provider.upper()} instance "
                 f"{self._instance_id}...")

        try:
            if self.provider == "aws":
                subprocess.run([
                    "aws", "ec2", "terminate-instances",
                    "--instance-ids", self._instance_id,
                    "--region", self.region,
                ], capture_output=True, timeout=60)
            elif self.provider == "gcp":
                subprocess.run([
                    "gcloud", "compute", "instances", "delete",
                    self._instance_id,
                    "--zone", self.region, "--quiet",
                ], capture_output=True, timeout=60)
            elif self.provider == "azure":
                subprocess.run([
                    "az", "vm", "delete",
                    "--ids", self._instance_id,
                    "--yes", "--no-wait",
                ], capture_output=True, timeout=60)

            self.log(f"Instance {self._instance_id} terminated")
        except Exception as e:
            self.log(f"Failed to terminate instance: {e}", "ERROR")
        finally:
            self._instance_id = None
            self._instance_ip = None
            self._clear_instance_state()


# ============================================================
#  Custom Backend
# ============================================================

class CustomBackend(ComputeBackend):
    """Custom/other backend: user-provided instructions injected into prompt."""

    @property
    def conda_env(self) -> str:
        return (self._compute_config.get("conda_env")
                or self.config.get("conda_env")
                or self.project_name)

    def setup(self) -> dict:
        return {}

    def get_agent_instructions(self) -> str:
        instructions = self._compute_config.get("instructions", "")
        return f"""## Compute Environment: Custom Setup

{instructions}

General settings:
- Conda environment: `{self.conda_env}`
- Save all results to the `results/` directory"""

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Check for result files (same as local)."""
        scripts_dir = self.config.get("scripts_dir", "scripts")
        possible_dirs = [
            self.code_dir / "results",
            self.code_dir / scripts_dir / "results",
        ]

        results_dir = None
        for d in possible_dirs:
            if d.exists():
                results_dir = d
                break

        if not results_dir:
            self.log("No results dir found, assuming done.", "WARN")
            return True

        current_time = time.time()
        for file in results_dir.rglob("*"):
            if file.is_file():
                if current_time - file.stat().st_mtime < max_wait_hours * 3600:
                    self.log("Found recent results")
                    return True
        return True
