import json
import subprocess
from .base import CloudBackend

class AWSCloudBackend(CloudBackend):
    """AWS-specific compute backend."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cc = self._compute_config
        self.region = cc.get("region", "us-east-1")

    def _provision(self):
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

    def teardown(self):
        if not self._instance_id:
            return
        self.log(f"Terminating AWS instance {self._instance_id}...")
        try:
            subprocess.run([
                "aws", "ec2", "terminate-instances",
                "--instance-ids", self._instance_id,
                "--region", self.region,
            ], capture_output=True, timeout=60)
            self.log(f"Instance {self._instance_id} terminated")
        except Exception as e:
            self.log(f"Failed to terminate instance: {e}", "ERROR")
        finally:
            self._instance_id = None
            self._instance_ip = None
            self._clear_instance_state()
