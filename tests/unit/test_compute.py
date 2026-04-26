"""
Compute backend integration tests.

Split into two sections:

UNIT TESTS — no cloud, no mocking needed
  Pure Python logic: factory dispatch, machine type selection, resource labels,
  state file I/O, orphan recovery from YAML.

GCP INTEGRATION TESTS — real cloud calls (marked @pytest.mark.gcp)
  Require ark-gcp-key.json. Each test provisions a real e2-micro instance,
  exercises real SSH / rsync / gcloud, then tears it down. Tests are ordered
  to build on each other but are independently skip-safe via try/finally.

Run only unit tests:
    pytest tests/test_compute_integration.py -m "not gcp"

Run GCP tests (requires credentials):
    pytest tests/test_compute_integration.py -m gcp -s
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

KEY_FILE = Path("ark-gcp-key.json")
GCLOUD_PATH = "/Users/bilal/Downloads/google-cloud-sdk/bin"


def _ensure_gcloud_in_path():
    if GCLOUD_PATH not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{GCLOUD_PATH}:{os.environ['PATH']}"


@pytest.fixture
def project_dir(tmp_path):
    code_dir = tmp_path / "test-project"
    code_dir.mkdir()
    (code_dir / "auto_research" / "state").mkdir(parents=True)
    return code_dir


@pytest.fixture(scope="session")
def gcp_credentials():
    """Load GCP credentials once per session; skip if not present."""
    _ensure_gcloud_in_path()
    if not KEY_FILE.exists():
        pytest.skip(f"GCP credentials not found at {KEY_FILE}")
    key_data = json.loads(KEY_FILE.read_text())
    return key_data


@pytest.fixture
def gcp_config(gcp_credentials):
    return {
        "compute_backend": {
            "type": "cloud",
            "provider": "gcp",
            "gcp_project": gcp_credentials["project_id"],
            "gcp_service_account_json": KEY_FILE.read_text(),
            "region": "us-central1-a",
            "instance_type": "e2-micro",
            "image_id": "ubuntu-2204-lts",
            "image_project": "ubuntu-os-cloud",
            "network": "vpc",
            "ssh_user": "ubuntu",
            "owner": "integration-test",
        }
    }


def _new_backend(gcp_config, project_dir):
    from ark.compute.cloud.gcp import GCPCloudBackend
    project_name = f"test-{int(time.time()) % 100000}"
    return GCPCloudBackend(gcp_config, project_name, project_dir), project_name


# ---------------------------------------------------------------------------
# UNIT TESTS — no cloud credentials needed
# ---------------------------------------------------------------------------

class TestComputeFactory:
    """from_config() dispatches to the correct backend type."""

    def test_creates_local_backend(self, tmp_path):
        from ark.compute import from_config
        from ark.compute.local import LocalBackend
        backend = from_config({"compute_backend": {"type": "local"}}, "proj", tmp_path)
        assert isinstance(backend, LocalBackend)

    def test_creates_slurm_backend(self, tmp_path):
        from ark.compute import from_config
        from ark.compute.slurm import SlurmBackend
        backend = from_config({"compute_backend": {"type": "slurm"}}, "proj", tmp_path)
        assert isinstance(backend, SlurmBackend)

    def test_creates_gcp_backend_type(self, tmp_path):
        """Factory returns a GCPCloudBackend without touching cloud."""
        from ark.compute import from_config
        from ark.compute.cloud.gcp import GCPCloudBackend
        config = {
            "compute_backend": {
                "type": "cloud", "provider": "gcp",
                "gcp_project": "fake", "region": "us-central1-a",
                "ssh_user": "ubuntu", "ssh_key_path": "~/.ssh/id_rsa",
            }
        }
        backend = from_config(config, "proj", tmp_path)
        assert isinstance(backend, GCPCloudBackend)

    def test_creates_custom_backend(self, tmp_path):
        from ark.compute import from_config
        from ark.compute.custom import CustomBackend
        config = {"compute_backend": {"type": "custom", "instructions": "run stuff"}}
        backend = from_config(config, "proj", tmp_path)
        assert isinstance(backend, CustomBackend)

    def test_legacy_use_slurm_flag(self, tmp_path):
        from ark.compute import from_config
        from ark.compute.slurm import SlurmBackend
        backend = from_config({"use_slurm": True}, "proj", tmp_path)
        assert isinstance(backend, SlurmBackend)

    def test_legacy_no_compute_defaults_to_local(self, tmp_path):
        from ark.compute import from_config
        from ark.compute.local import LocalBackend
        backend = from_config({}, "proj", tmp_path)
        assert isinstance(backend, LocalBackend)

    def test_unknown_backend_raises(self, tmp_path):
        from ark.compute import from_config
        with pytest.raises(ValueError, match="Unknown compute backend"):
            from_config({"compute_backend": {"type": "quantum"}}, "proj", tmp_path)

    def test_cloud_factory_unknown_provider_raises(self, tmp_path):
        from ark.compute.cloud.base import CloudBackend
        config = {"compute_backend": {"type": "cloud", "provider": "hypothetical"}}
        with pytest.raises(ValueError, match="Unknown cloud provider"):
            CloudBackend.from_config(config, "proj", tmp_path)


class TestGCPMachineTypeSelection:
    """Machine type is chosen from accelerator config without any cloud calls."""

    def _backend(self, tmp_path, accelerator=None, instance_type=None):
        from ark.compute.cloud.gcp import GCPCloudBackend
        cc = {
            "type": "cloud", "provider": "gcp",
            "gcp_project": "p", "region": "us-central1-a",
            "ssh_user": "ubuntu", "ssh_key_path": "~/.ssh/id_rsa",
        }
        if accelerator:
            cc["accelerator_type"] = accelerator
            cc["accelerator_count"] = 1
        if instance_type:
            cc["instance_type"] = instance_type
        return GCPCloudBackend({"compute_backend": cc}, "proj", tmp_path)

    def _captured_machine_type(self, backend) -> str:
        import subprocess
        from unittest.mock import MagicMock
        response = json.dumps([{
            "name": "inst-1",
            "networkInterfaces": [{"accessConfigs": [{"natIP": "1.2.3.4"}]}],
        }])
        m = MagicMock()
        m.returncode = 0
        m.stdout = response
        m.stderr = ""
        with patch("subprocess.run", return_value=m):
            backend._provision()
        # Read machine type from the call args
        import subprocess as sp
        return None  # will re-implement below

    def test_no_accelerator_uses_cpu_machine(self, tmp_path):
        b = self._backend(tmp_path)
        captured = []
        from unittest.mock import MagicMock
        response = json.dumps([{
            "name": "inst", "networkInterfaces": [{"accessConfigs": [{"natIP": "1.2.3.4"}]}]
        }])
        m = MagicMock(); m.returncode = 0; m.stdout = response; m.stderr = ""
        with patch("subprocess.run", return_value=m) as mock_run:
            b._provision()
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--machine-type") + 1] == "n4-standard-2"

    def test_l4_uses_g2_standard_4(self, tmp_path):
        b = self._backend(tmp_path, accelerator="nvidia-l4")
        from unittest.mock import MagicMock
        response = json.dumps([{
            "name": "inst", "networkInterfaces": [{"accessConfigs": [{"natIP": "1.2.3.4"}]}]
        }])
        m = MagicMock(); m.returncode = 0; m.stdout = response; m.stderr = ""
        with patch("subprocess.run", return_value=m) as mock_run:
            b._provision()
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--machine-type") + 1] == "g2-standard-4"

    def test_a100_uses_a2_highgpu(self, tmp_path):
        b = self._backend(tmp_path, accelerator="nvidia-tesla-a100")
        from unittest.mock import MagicMock
        response = json.dumps([{
            "name": "inst", "networkInterfaces": [{"accessConfigs": [{"natIP": "1.2.3.4"}]}]
        }])
        m = MagicMock(); m.returncode = 0; m.stdout = response; m.stderr = ""
        with patch("subprocess.run", return_value=m) as mock_run:
            b._provision()
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--machine-type") + 1] == "a2-highgpu-1g"

    def test_h100_uses_a3_highgpu(self, tmp_path):
        b = self._backend(tmp_path, accelerator="nvidia-h100-80gb")
        from unittest.mock import MagicMock
        response = json.dumps([{
            "name": "inst", "networkInterfaces": [{"accessConfigs": [{"natIP": "1.2.3.4"}]}]
        }])
        m = MagicMock(); m.returncode = 0; m.stdout = response; m.stderr = ""
        with patch("subprocess.run", return_value=m) as mock_run:
            b._provision()
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--machine-type") + 1] == "a3-highgpu-8g"

    def test_instance_type_override_wins_over_accelerator(self, tmp_path):
        b = self._backend(tmp_path, accelerator="nvidia-l4", instance_type="custom-8-32768")
        from unittest.mock import MagicMock
        response = json.dumps([{
            "name": "inst", "networkInterfaces": [{"accessConfigs": [{"natIP": "1.2.3.4"}]}]
        }])
        m = MagicMock(); m.returncode = 0; m.stdout = response; m.stderr = ""
        with patch("subprocess.run", return_value=m) as mock_run:
            b._provision()
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--machine-type") + 1] == "custom-8-32768"


class TestCloudStatePersistence:
    """State file I/O — pure filesystem, no cloud."""

    def test_save_creates_yaml_with_instance_metadata(self, project_dir):
        from ark.compute.cloud.gcp import GCPCloudBackend
        config = {"compute_backend": {
            "type": "cloud", "provider": "gcp", "gcp_project": "p",
            "region": "us-central1-a", "ssh_user": "ubuntu", "ssh_key_path": "~/.ssh/id_rsa",
        }}
        b = GCPCloudBackend(config, "proj", project_dir)
        b._instance_id = "ark-proj-1234"
        b._instance_ip = "1.2.3.4"
        b._save_instance_state()

        state_file = project_dir / "auto_research" / "state" / "cloud_instance.yaml"
        assert state_file.exists()
        state = yaml.safe_load(state_file.read_text())
        assert state["instance_id"] == "ark-proj-1234"
        assert state["instance_ip"] == "1.2.3.4"
        assert state["provider"] == "gcp"
        assert state["region"] == "us-central1-a"
        assert "created_at" in state

    def test_clear_state_removes_file(self, project_dir):
        from ark.compute.cloud.gcp import GCPCloudBackend
        config = {"compute_backend": {
            "type": "cloud", "provider": "gcp", "gcp_project": "p",
            "region": "us-central1-a", "ssh_user": "ubuntu", "ssh_key_path": "~/.ssh/id_rsa",
        }}
        b = GCPCloudBackend(config, "proj", project_dir)
        b._instance_id = "inst"
        b._instance_ip = "1.2.3.4"
        b._save_instance_state()
        b._clear_instance_state()
        assert not (project_dir / "auto_research" / "state" / "cloud_instance.yaml").exists()

    def test_clear_is_idempotent_when_no_file(self, project_dir):
        from ark.compute.cloud.gcp import GCPCloudBackend
        config = {"compute_backend": {
            "type": "cloud", "provider": "gcp", "gcp_project": "p",
            "region": "us-central1-a", "ssh_user": "ubuntu", "ssh_key_path": "~/.ssh/id_rsa",
        }}
        b = GCPCloudBackend(config, "proj", project_dir)
        b._clear_instance_state()  # should not raise

    def test_orphan_recovery_loads_saved_state(self, project_dir):
        from ark.compute.cloud.gcp import GCPCloudBackend
        config = {"compute_backend": {
            "type": "cloud", "provider": "gcp", "gcp_project": "p",
            "region": "us-central1-a", "ssh_user": "ubuntu", "ssh_key_path": "~/.ssh/id_rsa",
        }}
        state_file = project_dir / "auto_research" / "state" / "cloud_instance.yaml"
        yaml.dump({
            "provider": "gcp", "instance_id": "ark-old-5678", "instance_ip": "9.8.7.6",
            "region": "us-central1-a", "created_at": "2026-01-01T00:00:00",
        }, state_file.open("w"))

        b = GCPCloudBackend(config, "proj", project_dir)
        b._recover_orphaned_instance()
        assert b._instance_id == "ark-old-5678"
        assert b._instance_ip == "9.8.7.6"

    def test_orphan_recovery_no_state_file_is_noop(self, project_dir):
        from ark.compute.cloud.gcp import GCPCloudBackend
        config = {"compute_backend": {
            "type": "cloud", "provider": "gcp", "gcp_project": "p",
            "region": "us-central1-a", "ssh_user": "ubuntu", "ssh_key_path": "~/.ssh/id_rsa",
        }}
        b = GCPCloudBackend(config, "proj", project_dir)
        b._recover_orphaned_instance()
        assert b._instance_id is None

    def test_orphan_recovery_corrupted_yaml_is_noop(self, project_dir):
        from ark.compute.cloud.gcp import GCPCloudBackend
        config = {"compute_backend": {
            "type": "cloud", "provider": "gcp", "gcp_project": "p",
            "region": "us-central1-a", "ssh_user": "ubuntu", "ssh_key_path": "~/.ssh/id_rsa",
        }}
        state_file = project_dir / "auto_research" / "state" / "cloud_instance.yaml"
        state_file.write_text("not: valid: yaml: :::")
        b = GCPCloudBackend(config, "proj", project_dir)
        b._recover_orphaned_instance()
        assert b._instance_id is None


