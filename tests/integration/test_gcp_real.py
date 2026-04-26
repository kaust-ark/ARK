import os
import json
import time
import pytest
from pathlib import Path
from ark.compute.cloud import CloudBackend
from ark.compute.cloud.gcp import GCPCloudBackend

pytestmark = pytest.mark.gcp

KEY_FILE = Path("ark-gcp-key.json")

@pytest.fixture
def gcp_config():
    if not KEY_FILE.exists():
        pytest.skip(f"Service account key {KEY_FILE} not found")
    
    with open(KEY_FILE) as f:
        key_data = json.load(f)
    
    config = {
        "compute_backend": {
            "type": "cloud",
            "provider": "gcp",
            "gcp_project": key_data["project_id"],
            "gcp_service_account_json": KEY_FILE.read_text(),
            "region": "us-central1-a",
            "instance_type": "e2-micro",
            "image_id": "ubuntu-2204-lts",
            "image_project": "ubuntu-os-cloud",
            "network": "vpc",
            "ssh_user": "ubuntu",
            "owner": "integration-test-runner"
        }
    }
    return config

def test_gcp_provision_and_teardown(gcp_config, tmp_path):
    # Setup test project directory
    project_name = f"test-gcp-{int(time.time())}"
    code_dir = tmp_path / project_name
    code_dir.mkdir()
    (code_dir / "auto_research" / "state").mkdir(parents=True)
    
    backend = GCPCloudBackend(gcp_config, project_name, code_dir)
    
    instance_id = None
    try:
        print(f"\n[1/3] Provisioning GCP instance {project_name}...")
        ctx = backend.setup()
        
        instance_id = ctx.get("instance_id")
        instance_ip = ctx.get("ssh_host")
        
        assert instance_id is not None
        assert instance_ip is not None
        print(f"      Instance {instance_id} provisioned at {instance_ip}")
        
        # Verify state persistence
        state_file = code_dir / "auto_research" / "state" / "cloud_instance.yaml"
        assert state_file.exists()
        print("      State file created and verified.")
        
    finally:
        if instance_id:
            print(f"[2/3] Tearing down GCP instance {instance_id}...")
            backend.teardown()
            
            # Verify state is cleared
            state_file = code_dir / "auto_research" / "state" / "cloud_instance.yaml"
            assert not state_file.exists()
            print("      State file cleared.")
        else:
            print("      No instance to teardown.")

def test_gcloud_version():
    """Verify gcloud is accessible and working.

    Skipped on hosts without gcloud installed (e.g. CI runners, dev
    laptops without the SDK). The full GCP integration tests later in
    the file already gate on ``KEY_FILE``; this single sanity check
    needed the same kind of guard.
    """
    import shutil
    import subprocess
    if shutil.which("gcloud") is None:
        pytest.skip("gcloud CLI not installed")
    result = subprocess.run(["gcloud", "version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "Google Cloud SDK" in result.stdout
    print(f"\ngcloud version check passed:\n{result.stdout.splitlines()[0]}")
