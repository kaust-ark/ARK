"""
Compute backend GCP integration tests.
Requires real cloud credentials (ark-gcp-key.json).
"""

import json
import os
import time
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

KEY_FILE = Path("ark-gcp-key.json")


@pytest.fixture
def project_dir(tmp_path):
    code_dir = tmp_path / "test-project"
    code_dir.mkdir()
    (code_dir / "auto_research" / "state").mkdir(parents=True)
    return code_dir


@pytest.fixture(scope="session")
def gcp_credentials():
    """Load GCP credentials once per session; skip if not present."""
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
# GCP INTEGRATION TESTS — real cloud, real SSH, real rsync
# ---------------------------------------------------------------------------

@pytest.mark.gcp
class TestGCPProvisionAndTeardown:
    """Provision a real e2-micro instance and tear it down."""

    def test_provision_returns_instance_id_and_ip(self, gcp_config, project_dir):
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            ctx = backend.setup()
            assert ctx.get("instance_id"), "No instance_id returned"
            assert ctx.get("ssh_host"), "No ssh_host (IP) returned"
            print(f"\nProvisioned: {ctx['instance_id']} @ {ctx['ssh_host']}")
        finally:
            backend.teardown()

    def test_provision_writes_state_file(self, gcp_config, project_dir):
        backend, _ = _new_backend(gcp_config, project_dir)
        state_file = project_dir / "auto_research" / "state" / "cloud_instance.yaml"
        try:
            backend.setup()
            assert state_file.exists(), "cloud_instance.yaml not created"
            state = yaml.safe_load(state_file.read_text())
            assert state.get("instance_id")
            assert state.get("instance_ip")
        finally:
            backend.teardown()

    def test_teardown_removes_state_file(self, gcp_config, project_dir):
        backend, _ = _new_backend(gcp_config, project_dir)
        state_file = project_dir / "auto_research" / "state" / "cloud_instance.yaml"
        backend.setup()
        backend.teardown()
        assert not state_file.exists(), "cloud_instance.yaml not removed after teardown"
        assert backend._instance_id is None
        assert backend._instance_ip is None

    def test_teardown_is_idempotent(self, gcp_config, project_dir):
        """Calling teardown twice (or on an unprovisioned backend) should not raise."""
        backend, _ = _new_backend(gcp_config, project_dir)
        backend.setup()
        backend.teardown()
        backend.teardown()  # second call should be a no-op


@pytest.mark.gcp
class TestGCPSSHConnectivity:
    """After provisioning, verify real SSH connectivity and remote execution."""

    def test_ssh_exec_runs_command_on_remote(self, gcp_config, project_dir):
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            output = backend._ssh_exec("echo hello-from-remote")
            assert "hello-from-remote" in output, \
                f"Unexpected SSH output: {output!r}"
        finally:
            backend.teardown()

    def test_ssh_exec_can_read_remote_files(self, gcp_config, project_dir):
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            # Write a file remotely then read it back
            backend._ssh_exec("echo test-content > /tmp/ark_test_file.txt")
            output = backend._ssh_exec("cat /tmp/ark_test_file.txt")
            assert "test-content" in output
        finally:
            backend.teardown()

    def test_get_agent_instructions_contains_ssh_details(self, gcp_config, project_dir):
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            instructions = backend.get_agent_instructions()
            assert backend._instance_ip in instructions
            assert "ssh" in instructions.lower()
            assert "/tmp/ark_experiment_done" in instructions
        finally:
            backend.teardown()


@pytest.mark.gcp
class TestGCPCodeTransfer:
    """Verify rsync transfers local code to the remote instance."""

    def test_code_transfer_copies_files_to_remote(self, gcp_config, project_dir):
        # Put a sentinel file in the code directory
        sentinel = project_dir / "integration_test_sentinel.txt"
        sentinel.write_text("ark-integration-test")

        backend, project_name = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            remote_path = f"/home/ubuntu/{project_name}/integration_test_sentinel.txt"
            output = backend._ssh_exec(f"cat {remote_path}")
            assert "ark-integration-test" in output, \
                f"Sentinel file not found on remote (got: {output!r})"
        finally:
            backend.teardown()

    def test_auto_research_dir_excluded_from_transfer(self, gcp_config, project_dir):
        """auto_research/ (state files) should not be rsync'd to remote."""
        private_file = project_dir / "auto_research" / "state" / "secret.yaml"
        private_file.write_text("should-not-be-on-remote: true")

        backend, project_name = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            remote_path = f"/home/ubuntu/{project_name}/auto_research/state/secret.yaml"
            output = backend._ssh_exec(f"test -f {remote_path} && echo EXISTS || echo MISSING")
            assert "MISSING" in output, \
                "auto_research/ was transferred to remote (should be excluded)"
        finally:
            backend.teardown()


@pytest.mark.gcp
class TestGCPWaitForCompletion:
    """wait_for_completion() responds correctly to the marker file and process state."""

    def test_returns_true_when_marker_file_created(self, gcp_config, project_dir):
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            # Simulate experiment finishing: create the marker file remotely
            backend._ssh_exec("touch /tmp/ark_experiment_done")
            result = backend.wait_for_completion(max_wait_hours=0.1)
            assert result is True, "wait_for_completion should return True when marker exists"
        finally:
            backend._ssh_exec("rm -f /tmp/ark_experiment_done")
            backend.teardown()

    def test_returns_true_when_no_processes_running(self, gcp_config, project_dir):
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            # No python/train processes running — should complete quickly
            result = backend.wait_for_completion(max_wait_hours=0.1)
            assert result is True
        finally:
            backend.teardown()

    def test_detects_running_process_then_completion(self, gcp_config, project_dir):
        """Start a background process, wait, then create the marker — verify detection."""
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            # Start a short background sleep that represents an "experiment"
            backend._ssh_exec("nohup sleep 5 > /tmp/ark_sleep.log 2>&1 &")
            # Create marker immediately (simulating the experiment writing it)
            backend._ssh_exec("touch /tmp/ark_experiment_done")
            result = backend.wait_for_completion(max_wait_hours=0.1)
            assert result is True
        finally:
            backend._ssh_exec("rm -f /tmp/ark_experiment_done /tmp/ark_sleep.log")
            backend.teardown()


@pytest.mark.gcp
class TestGCPCollectResults:
    """collect_results() rsyncs the remote results/ directory back locally."""

    def test_collect_retrieves_remote_results(self, gcp_config, project_dir):
        backend, project_name = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            remote_results_dir = f"/home/ubuntu/{project_name}/results"
            # Create a result file on the remote
            backend._ssh_exec(f"mkdir -p {remote_results_dir} && echo 'accuracy=0.95' > {remote_results_dir}/metrics.txt")

            result = backend.collect_results()
            assert result is True

            local_metrics = project_dir / "results" / "metrics.txt"
            assert local_metrics.exists(), "metrics.txt not collected from remote"
            assert "accuracy=0.95" in local_metrics.read_text()
        finally:
            backend.teardown()

    def test_collect_results_creates_local_results_dir(self, gcp_config, project_dir):
        backend, project_name = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            backend._ssh_exec(f"mkdir -p /home/ubuntu/{project_name}/results")
            backend.collect_results()
            assert (project_dir / "results").is_dir()
        finally:
            backend.teardown()


@pytest.mark.gcp
class TestGCPSetupCommands:
    """setup_commands run on the remote instance after code transfer."""

    def test_setup_commands_execute_on_remote(self, gcp_config, project_dir):
        gcp_config["compute_backend"]["setup_commands"] = [
            "echo ark-setup-ran > /tmp/ark_setup_marker.txt",
        ]
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            output = backend._ssh_exec("cat /tmp/ark_setup_marker.txt")
            assert "ark-setup-ran" in output, \
                f"setup_commands did not run (got: {output!r})"
        finally:
            backend.teardown()

    def test_multiple_setup_commands_all_run(self, gcp_config, project_dir):
        gcp_config["compute_backend"]["setup_commands"] = [
            "echo step1 > /tmp/ark_step1.txt",
            "echo step2 > /tmp/ark_step2.txt",
        ]
        backend, _ = _new_backend(gcp_config, project_dir)
        try:
            backend.setup()
            out1 = backend._ssh_exec("cat /tmp/ark_step1.txt")
            out2 = backend._ssh_exec("cat /tmp/ark_step2.txt")
            assert "step1" in out1
            assert "step2" in out2
        finally:
            backend.teardown()


@pytest.mark.gcp
class TestGCPOrphanRecovery:
    """A second backend instance with saved state reuses the existing instance."""

    def test_second_setup_reuses_provisioned_instance(self, gcp_config, project_dir):
        from ark.compute.cloud.gcp import GCPCloudBackend

        backend1, project_name = _new_backend(gcp_config, project_dir)
        try:
            ctx1 = backend1.setup()
            original_id = ctx1["instance_id"]
            original_ip = ctx1["ssh_host"]

            # Simulate a new process that picks up the same project directory
            backend2 = GCPCloudBackend(gcp_config, project_name, project_dir)
            ctx2 = backend2.setup()

            assert ctx2["instance_id"] == original_id, \
                "Second setup() provisioned a NEW instance instead of reusing"
            assert ctx2["ssh_host"] == original_ip
            print(f"\nReused orphaned instance: {original_id}")
        finally:
            backend1.teardown()


@pytest.mark.gcp
class TestGCPFullLifecycle:
    """Complete end-to-end: provision → run experiment → collect → teardown."""

    def test_full_experiment_lifecycle(self, gcp_config, project_dir):
        # Add some "experiment code" to transfer
        (project_dir / "run_experiment.sh").write_text(
            "#!/bin/bash\n"
            "mkdir -p results\n"
            "echo 'loss=0.01' > results/final_metrics.txt\n"
            "touch /tmp/ark_experiment_done\n"
        )

        backend, project_name = _new_backend(gcp_config, project_dir)
        try:
            # 1. Provision and transfer code
            ctx = backend.setup()
            assert ctx["instance_id"]
            print(f"\n[1/4] Provisioned {ctx['instance_id']} @ {ctx['ssh_host']}")

            # 2. Run the experiment remotely
            work_dir = f"/home/ubuntu/{project_name}"
            backend._ssh_exec(f"cd {work_dir} && bash run_experiment.sh")
            print("[2/4] Experiment script executed")

            # 3. Wait for completion (marker file is already present)
            done = backend.wait_for_completion(max_wait_hours=0.1)
            assert done, "wait_for_completion did not detect completion"
            print("[3/4] Completion detected")

            # 4. Collect results
            collected = backend.collect_results()
            assert collected
            metrics = project_dir / "results" / "final_metrics.txt"
            assert metrics.exists(), "Results not collected from remote"
            assert "loss=0.01" in metrics.read_text()
            print("[4/4] Results collected")

        finally:
            backend.teardown()
            state_file = project_dir / "auto_research" / "state" / "cloud_instance.yaml"
            assert not state_file.exists(), "State file not cleared after teardown"
            print("Teardown complete, state file removed")
