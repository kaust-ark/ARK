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
        elif backend_type == "kubernetes":
            return KubernetesBackend(config, project_name, code_dir, log_fn)
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
        cmd = [
            "aws", "ec2", "run-instances",
            "--image-id", self.image_id,
            "--instance-type", self.instance_type,
            "--key-name", self.ssh_key_name,
            "--region", self.region,
            "--count", "1",
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
        cmd = [
            "gcloud", "compute", "instances", "create", instance_name,
            "--zone", self.region,
            "--machine-type", self.instance_type,
            "--image-family", self.image_id,
            "--image-project", "deeplearning-platform-release",
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

        # Ensure resource group exists
        subprocess.run([
            "az", "group", "create",
            "--name", rg,
            "--location", self.region,
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


# ============================================================
#  Kubernetes (EKS) Backend
# ============================================================

def _build_k8s_client(kubeconfig_b64: str | None = None):
    """
    Build a kubernetes (BatchV1Api, CoreV1Api) tuple.
    Priority: explicit kubeconfig_b64 > in-cluster config (IRSA) > ~/.kube/config.
    """
    from kubernetes import client as k8s_client_lib, config as k8s_config
    if kubeconfig_b64:
        import base64, tempfile, os
        raw = base64.b64decode(kubeconfig_b64).decode()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(raw)
            tmp_path = f.name
        try:
            k8s_config.load_kube_config(config_file=tmp_path)
        finally:
            os.unlink(tmp_path)
    else:
        try:
            k8s_config.load_incluster_config()   # pod-mounted SA token (IRSA)
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()        # dev: ~/.kube/config
    return (
        k8s_client_lib.BatchV1Api(),
        k8s_client_lib.CoreV1Api(),
    )


def _poll_k8s_job_status(batch_v1, namespace: str, job_name: str) -> str:
    """Return RUNNING, COMPLETED, or FAILED."""
    try:
        job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
    except Exception:
        return "UNKNOWN"
    for c in (job.status.conditions or []):
        if c.type == "Complete" and c.status == "True":
            return "COMPLETED"
        if c.type == "Failed" and c.status == "True":
            return "FAILED"
    return "RUNNING"


class KubernetesBackend(ComputeBackend):
    """Run ARK orchestrator as a Kubernetes Job (EKS MVP, platform-agnostic)."""

    def __init__(self, config: dict, project_name: str, code_dir: Path,
                 log_fn=None, k8s_client=None):
        super().__init__(config, project_name, code_dir, log_fn)
        self._batch_v1 = k8s_client   # pre-built BatchV1Api; injected by webapp
        cc = self._compute_config
        self.namespace: str = cc.get("namespace", "ark-jobs")
        self.job_name: str = cc.get("job_name", "")
        self.image: str = cc.get("image", "")
        self.pvc_name: str = cc.get("pvc_name", "ark-data-pvc")
        self.service_account: str = cc.get("service_account", "")
        self.cpu_request: str = cc.get("cpu_request", "2")
        self.cpu_limit: str = cc.get("cpu_limit", "4")
        self.memory_request: str = cc.get("memory_request", "8Gi")
        self.memory_limit: str = cc.get("memory_limit", "16Gi")
        self.gpu_count: int = int(cc.get("gpu_count", 0))
        self.node_selector: dict = cc.get("node_selector", {})
        self.tolerations: list = cc.get("tolerations", [])
        self.env_vars: dict = cc.get("env_vars", {})
        self.project_dir: Path = Path(cc.get("project_dir", str(code_dir)))

    def setup(self) -> dict:
        if not self.job_name:
            raise RuntimeError("job_name must be set before calling setup()")
        if not self.image:
            raise RuntimeError("K8S_JOB_IMAGE is not configured")
        if not self._batch_v1:
            raise RuntimeError("Kubernetes client not initialised")

        job = self._build_job_manifest()
        self._batch_v1.create_namespaced_job(namespace=self.namespace, body=job)
        self.log(f"K8s Job created: {self.namespace}/{self.job_name}")
        return {"k8s_job": f"{self.namespace}/{self.job_name}"}

    def _build_job_manifest(self):
        from kubernetes import client as V1

        resources = V1.V1ResourceRequirements(
            requests={"cpu": self.cpu_request, "memory": self.memory_request},
            limits={"cpu": self.cpu_limit, "memory": self.memory_limit},
        )
        if self.gpu_count > 0:
            resources.requests["nvidia.com/gpu"] = str(self.gpu_count)
            resources.limits["nvidia.com/gpu"] = str(self.gpu_count)

        env = []
        for k, v in self.env_vars.items():
            env.append(V1.V1EnvVar(name=k, value=v))

        # Volumes: only needed for co-located EFS optimization; default path uses S3
        volumes, mounts = [], []
        pvc_name = self._compute_config.get("pvc_name", "")
        if pvc_name:
            volumes.append(V1.V1Volume(
                name="ark-data",
                persistent_volume_claim=V1.V1PersistentVolumeClaimVolumeSource(
                    claim_name=pvc_name,
                ),
            ))
            mounts.append(V1.V1VolumeMount(name="ark-data", mount_path="/data"))

        container = V1.V1Container(
            name="ark-orchestrator",
            image=self.image,
            # No args: all config is via env vars consumed by job-entrypoint.sh
            env=env,
            resources=resources,
            volume_mounts=mounts or None,
            image_pull_policy="Always",
        )

        pod_spec = V1.V1PodSpec(
            restart_policy="Never",     # Jobs must not auto-restart; webapp handles retries
            containers=[container],
            volumes=volumes or None,
            service_account_name=self.service_account or None,
            node_selector=self.node_selector or None,
            tolerations=[
                V1.V1Toleration(**t) for t in self.tolerations
            ] if self.tolerations else None,
        )

        job_spec = V1.V1JobSpec(
            template=V1.V1PodTemplateSpec(
                metadata=V1.V1ObjectMeta(
                    labels={"app": "ark-job", "ark-project": self.project_name[:48]},
                ),
                spec=pod_spec,
            ),
            backoff_limit=0,                  # Never retry; return FAILED immediately
            ttl_seconds_after_finished=3600,  # auto-cleanup pods after 1h
        )

        return V1.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=V1.V1ObjectMeta(
                name=self.job_name,
                namespace=self.namespace,
                labels={"managed-by": "ark-webapp"},
            ),
            spec=job_spec,
        )

    def get_agent_instructions(self) -> str:
        return (
            f"## Compute Environment: Kubernetes (EKS)\\n\\n"
            f"Running inside k8s Job pod {self.job_name} (namespace: {self.namespace}).\\n"
            f"ARK_FORCE_LOCAL=1 — run all experiments directly, no sbatch/srun.\\n"
            f"Project data is on shared EFS at /data/projects/. Write results to results/."
        )

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        import time
        max_wait = max_wait_hours * 3600
        start = time.time()
        while time.time() - start < max_wait:
            status = _poll_k8s_job_status(self._batch_v1, self.namespace, self.job_name)
            if status == "COMPLETED":
                return True
            if status == "FAILED":
                return False
            time.sleep(30)
        self.log(f"K8s job wait timeout after {max_wait_hours}h", "WARN")
        return False

    def collect_results(self) -> bool:
        sentinel = self.project_dir / "auto_research" / "state" / "paper_state.yaml"
        if not sentinel.exists():
            self.log("paper_state.yaml not found — results may be incomplete", "WARN")
        return True   # non-fatal; webapp renders whatever exists

    def teardown(self):
        if not self.job_name or not self._batch_v1:
            return
        try:
            self._batch_v1.delete_namespaced_job(
                name=self.job_name,
                namespace=self.namespace,
                body={"propagationPolicy": "Foreground"},  # also deletes pods
            )
            self.log(f"K8s Job {self.job_name} deleted")
        except Exception as e:
            self.log(f"Failed to delete K8s Job {self.job_name}: {e}", "WARN")
