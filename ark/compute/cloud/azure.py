import json
import os
import subprocess
import time
from .base import CloudBackend

class AzureCloudBackend(CloudBackend):
    """Azure-specific compute backend."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cc = self._compute_config
        self.region = cc.get("region", "eastus")

    def _provision(self):
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

    def teardown(self):
        if not self._instance_id:
            return
        self.log(f"Terminating Azure instance {self._instance_id}...")
        try:
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
