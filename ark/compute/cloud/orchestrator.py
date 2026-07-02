import os
import json
import shlex
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

    def run_orchestrator(self, control_plane_url: str = None, control_plane_token: str = None):
        """Execute the orchestrator in a detached session, start the reaper, and save full state.

        When ``control_plane_url``/``control_plane_token`` are supplied, the remote
        orchestrator reports status, state projections, and artifacts back over the
        /v1 HTTP control-plane API (ADR-0003/0012/0013) instead of the removed
        rsync-back bridge. The URL rides on argv; the bearer token is carried only
        in the RAM-disk ``.env`` so it never appears in ``ps``/argv.
        """
        remote_work_dir = f"/home/{self.ssh_user}/{self.project_name}"
        conda_env = self.conda_env or "ark-base"
        log_rel = "logs/latest.log"  # relative; orchestrator creates the latest.log symlink
        log_file = f"{remote_work_dir}/{log_rel}"
        pid_file = f"{remote_work_dir}/orchestrator.pid"
        reaper_pid_file = f"{remote_work_dir}/reaper.pid"
        reaper_script = f"{remote_work_dir}/ark_vm_reaper.sh"

        # Sync the ark source package so PYTHONPATH=/home/{ssh_user}/ark_source resolves
        import os
        key_path = os.path.expanduser(self.ssh_key_path)
        ssh_opts = (
            f"ssh -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o LogLevel=ERROR -i {key_path}"
        )
        ark_source_root = Path(__file__).resolve().parents[3]  # repo root containing ark/
        remote_ark_source = f"/home/{self.ssh_user}/ark_source"
        try:
            self._ssh_exec(f"mkdir -p {remote_ark_source}", timeout=10)
            subprocess.run([
                "rsync", "-az", "--exclude=.git", "--exclude=__pycache__",
                "--exclude=.venv", "--exclude=*.pyc", "--exclude=projects",
                "-e", ssh_opts,
                str(ark_source_root) + "/",
                f"{self.ssh_user}@{self._instance_ip}:{remote_ark_source}/",
            ], check=True, timeout=120)
            self.log("Ark source synced to remote ark_source/")
        except Exception as e:
            self.log(f"Ark source sync failed: {e}", "WARN")

        # Sync the reaper script to the VM (Phase 6)
        local_reaper = Path(__file__).resolve().parents[3] / "scripts" / "ark_vm_reaper.sh"
        if local_reaper.exists():
            try:
                subprocess.run([
                    "rsync", "-az", "-e", ssh_opts,
                    str(local_reaper),
                    f"{self.ssh_user}@{self._instance_ip}:{reaper_script}",
                ], check=True, timeout=30)
                self._ssh_exec(f"chmod +x {reaper_script}", timeout=10)
            except Exception as e:
                self.log(f"Reaper sync failed (non-fatal): {e}", "WARN")

        # Phase 4: Sync credentials securely to /dev/shm (RAM disk)
        # Build a merged .env that combines the on-disk project .env with any
        # API key env vars injected into this process by the webapp (e.g.
        # GEMINI_API_KEY, ANTHROPIC_API_KEY). Without this merge, keys supplied
        # via the webapp's user-settings form never reach the remote orchestrator.
        _API_KEY_VARS = (
            "GEMINI_API_KEY", "GOOGLE_API_KEY",
            "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION",
            "GOOGLE_CLOUD_PROJECT",
        )
        local_env = self.code_dir / ".env"
        existing_lines = local_env.read_text().splitlines() if local_env.exists() else []
        # Track which keys are already set in the file so we only append missing ones
        existing_keys = set()
        for line in existing_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                existing_keys.add(stripped.split("=", 1)[0].strip())
        extra_lines = []
        for var in _API_KEY_VARS:
            if var not in existing_keys and os.environ.get(var):
                extra_lines.append(f"{var}={os.environ[var]}")
        # Control-plane bearer token (minted per-run, not in os.environ). Carried
        # in the RAM-disk .env so the remote orchestrator can authenticate to the
        # /v1 API — never placed on argv where it would show up in `ps`.
        if control_plane_token and "ARK_CONTROL_PLANE_TOKEN" not in existing_keys:
            extra_lines.append(f"ARK_CONTROL_PLANE_TOKEN={control_plane_token}")
        import tempfile
        merged_env_content = "\n".join(existing_lines + extra_lines)
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False, prefix="ark_remote_") as tf:
                tf.write(merged_env_content)
                merged_env_path = tf.name
            subprocess.run([
                "rsync", "-az", "-e", ssh_opts,
                merged_env_path,
                f"{self.ssh_user}@{self._instance_ip}:/dev/shm/.env",
            ], check=True, timeout=30)
            os.unlink(merged_env_path)
            if extra_lines:
                self.log(f"Synced .env with {len(extra_lines)} extra key(s): {[l.split('=')[0] for l in extra_lines]}")
        except Exception as e:
            self.log(f"Failed to sync .env to /dev/shm: {e}", "WARN")

        # Sync SSH key
        if os.path.exists(key_path):
            try:
                subprocess.run([
                    "rsync", "-az", "-e", ssh_opts,
                    key_path,
                    f"{self.ssh_user}@{self._instance_ip}:/dev/shm/ark_id_rsa",
                ], check=True, timeout=30)
                self._ssh_exec("chmod 600 /dev/shm/ark_id_rsa", timeout=10)
            except Exception as e:
                self.log(f"Failed to sync SSH key to /dev/shm: {e}", "WARN")

        # Ensure the logs dir exists on the remote
        self._ssh_exec(f"mkdir -p {remote_work_dir}/logs {remote_work_dir}/auto_research/state", timeout=10)

        max_iterations = self.config.get("max_iterations", 3)
        max_days = self.config.get("max_days", 3)

        # conda run spawns a fresh process that does not inherit shell exports,
        # so all forwarded vars must be passed explicitly via `env` after sourcing
        # .env — including the control-plane token (sourced from the RAM-disk .env).
        _forward_vars = _API_KEY_VARS + ("ARK_CONTROL_PLANE_TOKEN",)
        _env_forward_args = " ".join(
            f'{v}="${{{v}}}"'
            for v in _forward_vars
        )
        # When a control-plane URL is configured, the remote orchestrator reports
        # over HTTP (project_id must be supplied explicitly; there is no by-name
        # resolution off-box). Otherwise it runs blind — see the launch-site warning.
        _cp_args = ""
        if control_plane_url:
            _cp_args = (
                f"--control-plane-url {control_plane_url} "
                f"--project-id {self.project_name} "
            )
        start_cmd = (
            f"cd /home/{self.ssh_user}/ark_source && "
            f"set -a; [ -f /dev/shm/.env ] && source /dev/shm/.env; set +a; "
            # conda run spawns a fresh process that does not inherit shell exports,
            # so PYTHONPATH and all API keys must be injected via `env` inside the
            # conda run invocation.
            f"nohup conda run -n {conda_env} env "
            f"PYTHONPATH=/home/{self.ssh_user}/ark_source "
            f"ARK_PROJECT_DIR={remote_work_dir} "
            f"ARK_SSH_KEY_PATH=/dev/shm/ark_id_rsa "
            f"{_env_forward_args} "
            f"python -m ark.orchestrator "
            f"--project {self.project_name} "
            f"{_cp_args}"
            f"--project-dir {remote_work_dir} "
            f"--code-dir {remote_work_dir} "
            f"--iterations {max_iterations} "
            f"--max-days {max_days} "
            f"</dev/null >{log_file} 2>&1 & "
            f"echo $! > {pid_file}"
        )

        self.log(f"Starting remote orchestrator process...")
        # Wrap in `setsid sh -c '... </dev/null >/dev/null 2>&1 &'` so the SSH
        # channel sees no inherited fds and closes immediately. Without this,
        # OpenSSH waits up to its full timeout for the background process to
        # close stdout/stderr — even though the inner command already redirects
        # them — and we hit a 30s TimeoutExpired before reaching the state-file
        # write below.
        wrapped = f"setsid sh -c {shlex.quote(start_cmd)} </dev/null >/dev/null 2>&1"
        self._ssh_exec(wrapped, timeout=30)

        # Read the PID back to confirm it started.
        # conda run exits quickly after spawning Python; prefer the PID written
        # by ark.orchestrator itself ({remote_work_dir}/.pid) which tracks the real process.
        pid = None
        try:
            pid = self._ssh_exec(f"cat {pid_file}", timeout=10).strip()
            self.log(f"Remote orchestrator started with PID: {pid}")
        except Exception as e:
            self.log(f"Failed to read remote orchestrator PID: {e}", "ERROR")
            return None
        if not pid:
            self.log("Remote orchestrator PID file empty — start likely failed", "ERROR")
            return None

        # Persist the orchestrator state file ASAP so the dashboard poller
        # doesn't mark the project failed if any of the steps below (reaper
        # start, .pid handoff) raises. We rewrite it at the end with the
        # refined PID, but the early write means a partial failure no longer
        # strands the project in `failed` with a live VM.
        import yaml
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._state_file, "w") as f:
            yaml.dump({
                "instance_id": self._instance_id,
                "public_ip": self._instance_ip,
                "project_id": self.project_name,
                "orchestrator_pid": pid,
                "launched_at": datetime.utcnow().isoformat() + "Z",
                "log_file": log_rel,
            }, f, default_flow_style=False)

        # Give ark.orchestrator a moment to write its own .pid file, then prefer that
        time.sleep(3)
        ark_pid_file = f"{remote_work_dir}/.pid"
        try:
            ark_pid = self._ssh_exec(f"cat {ark_pid_file} 2>/dev/null || echo ''", timeout=10).strip()
            if ark_pid and ark_pid != pid:
                self.log(f"Using ark process PID: {ark_pid} (was {pid})")
                pid = ark_pid
        except Exception:
            pass

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
        Probe whether the remote orchestrator process is still alive.

        This is a pure liveness check over SSH (``kill -0``). Terminal *outcome*
        (done/failed/stopped) is no longer inferred from a synced ``paper_state.yaml``:
        the remote orchestrator reports it directly into the control-plane DB via the
        /v1 API (ADR-0013), so the caller reads the outcome from the DB. Returns:

        - ``'RUNNING'``   — the process answered ``kill -0``.
        - ``'STOPPED'``   — the process is gone (outcome lives in the control-plane DB;
          if the DB was never updated, the run crashed and the caller marks it failed).
        - ``'UNKNOWN'``   — no state file yet, or the probe itself errored (retry later).
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
            except Exception as e:
                # Probe failed (transient SSH/network) — don't declare it dead.
                self.log(f"Liveness probe failed for orchestrator {pid}: {e}")
                return "UNKNOWN"

            return "RUNNING" if result.returncode == 0 else "STOPPED"

        except Exception as e:
            self.log(f"Error polling orchestrator: {e}", "ERROR")
            return "UNKNOWN"

    def teardown(self):
        """Wipe remote credentials, tear down the GCP orchestrator VM, and clear state."""
        # Re-load instance info from the state file if called from a fresh object
        # (e.g. the webapp stop endpoint reconstructs the backend without calling setup())
        if not self._instance_id and self._state_file.exists():
            try:
                import yaml
                with open(self._state_file) as f:
                    state = yaml.safe_load(f) or {}
                self._instance_id = state.get("instance_id")
                self._instance_ip = state.get("public_ip") or state.get("instance_ip")
                if self._instance_id:
                    self.log(f"Loaded orchestrator VM state for teardown: {self._instance_id}")
            except Exception as e:
                self.log(f"Failed to load orchestrator state for teardown: {e}", "WARN")

        # No final rsync-back: state projections and artifacts are reported to the
        # control plane over the /v1 API + artifact store during the run
        # (ADR-0012/0013), so the VM's working dir holds nothing the control plane
        # still needs at teardown.

        # Wipe credentials from RAM disk (Phase 4 / Phase 6)
        try:
            self._ssh_exec("rm -f /dev/shm/.env /dev/shm/ark_id_rsa", timeout=10)
        except Exception:
            pass

        # Delegate actual VM termination to GCPCloudBackend
        super().teardown()
