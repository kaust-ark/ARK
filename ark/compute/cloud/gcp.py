import json
import os
import subprocess
import time
import tempfile
from pathlib import Path
from contextlib import contextmanager
from .base import CloudBackend

class GCPCloudBackend(CloudBackend):
    """GCP-specific compute backend."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cc = self._compute_config
        self.region = cc.get("region", "us-central1-a")
        self.gcp_project = cc.get("gcp_project", "")
        self.gcp_service_account_json = cc.get("gcp_service_account_json", "")

    @contextmanager
    def _gcloud_env(self):
        env = os.environ.copy()
        if not self.gcp_service_account_json:
            yield env
            return
        tf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="ark_gcp_sa_"
        )
        try:
            tf.write(self.gcp_service_account_json)
            tf.flush()
            tf.close()
            env["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] = tf.name
            yield env
        finally:
            try:
                os.unlink(tf.name)
            except Exception:
                pass

    def _provision(self):
        instance_name = f"ark-{self.project_name}-{int(time.time()) % 10000}"
        labels = self._resource_labels()
        labels_str = ",".join(f"{k}={v}" for k, v in labels.items())

        accelerator = self._compute_config.get("accelerator_type")
        image_family = self.image_id
        image_project = self._compute_config.get("image_project", "deeplearning-platform-release")
        
        if not image_family:
            image_family = "common-cu124" if accelerator else "common-cpu"
            image_project = "deeplearning-platform-release"

        if self.instance_type:
            machine_type = self.instance_type
        elif accelerator:
            accel_lower = accelerator.lower()
            if "l4" in accel_lower:
                machine_type = "g2-standard-4"
            elif "a100" in accel_lower:
                machine_type = "a2-highgpu-1g"
            elif "h100" in accel_lower:
                machine_type = "a3-highgpu-8g"
            else:
                machine_type = "n1-standard-4"
        else:
            machine_type = "n4-standard-2"

        cmd = [
            "gcloud", "compute", "instances", "create", instance_name,
            "--zone", self.region,
            "--machine-type", machine_type,
            "--image-family", image_family,
            "--image-project", image_project,
            "--labels", labels_str,
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

        if accelerator:
            count = self._compute_config.get("accelerator_count", 1)
            cmd.extend([
                "--accelerator", f"type={accelerator},count={count}",
                "--maintenance-policy", "TERMINATE",
            ])

        with self._gcloud_env() as env:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"GCP provision failed: {result.stderr}")

        data = json.loads(result.stdout)
        self._instance_id = instance_name
        for iface in data[0].get("networkInterfaces", []):
            for access in iface.get("accessConfigs", []):
                if access.get("natIP"):
                    self._instance_ip = access["natIP"]
                    break
        if not self._instance_ip:
            raise RuntimeError("GCP instance has no external IP")
        self.log(f"Instance created: {self._instance_id} ({self._instance_ip})")

    def teardown(self):
        if not self._instance_id:
            return
        self.log(f"Terminating GCP instance {self._instance_id}...")
        try:
            with self._gcloud_env() as env:
                subprocess.run([
                    "gcloud", "compute", "instances", "delete",
                    self._instance_id,
                    "--zone", self.region, "--quiet",
                ], capture_output=True, timeout=60, env=env)
            self.log(f"Instance {self._instance_id} terminated")
        except Exception as e:
            self.log(f"Failed to terminate instance: {e}", "ERROR")
        finally:
            self._instance_id = None
            self._instance_ip = None
            self._clear_instance_state()
