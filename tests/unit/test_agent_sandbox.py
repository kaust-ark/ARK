"""The structural agent sandbox — confinement that does not need the agent's help.

The advisory sandbox asks the agent to prefix commands with ``./sandbox/run.sh``.
Counted on project 76759cf7: 14 commands executed, 0 through the helper. These
tests pin the behaviour of the replacement, where the agent-server itself runs
inside Apptainer and the prompt stops being the enforcement mechanism.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ark import sandbox as sb
from ark.engines import sdk_runtime as sr


@pytest.fixture
def structural(tmp_path):
    """ARK_AGENT_SANDBOX + SDK runtime + a present apptainer and image."""
    sif = tmp_path / "agent-server.sif"
    sif.write_text("not really a SIF, but it is a file")
    with patch.dict(os.environ, {"ARK_AGENT_SANDBOX": "apptainer",
                                 "ARK_AGENT_RUNTIME": "sdk",
                                 "ARK_AGENT_SERVER_SIF": str(sif)}), \
         patch.object(sb, "apptainer_bin", return_value="/usr/bin/apptainer"):
        yield sif


class TestActivation:
    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARK_AGENT_SANDBOX", None)
            assert sb.structural_sandbox_requested() is False
            assert sb.structural_sandbox_status()[0] is False

    def test_flag_is_case_and_space_tolerant(self):
        with patch.dict(os.environ, {"ARK_AGENT_SANDBOX": " Apptainer "}):
            assert sb.structural_sandbox_requested() is True

    def test_active_when_everything_is_present(self, structural):
        active, reason = sb.structural_sandbox_status()
        assert active is True
        assert str(structural) in reason

    def test_needs_the_sdk_runtime(self, structural):
        """The workspace is an argument to the driver's Conversation; the stock
        headless CLI has nowhere to put one, so the flag alone confines nothing."""
        with patch.dict(os.environ, {"ARK_AGENT_RUNTIME": "cli"}):
            active, reason = sb.structural_sandbox_status()
            assert active is False
            assert "sdk" in reason.lower()


class TestDegradesWhenApptainerIsAbsent:
    def test_missing_apptainer_is_reported_not_raised(self, structural):
        with patch.object(sb, "apptainer_bin", return_value=None):
            active, reason = sb.structural_sandbox_status()
        assert active is False
        assert "apptainer" in reason.lower()

    def test_missing_image_tells_you_how_to_get_it(self, tmp_path):
        """A 4 GB pull inside a phase looks exactly like a hung agent, so the
        image is never fetched on demand — the reason carries the command."""
        absent = tmp_path / "nope.sif"
        with patch.dict(os.environ, {"ARK_AGENT_SANDBOX": "apptainer",
                                     "ARK_AGENT_RUNTIME": "sdk",
                                     "ARK_AGENT_SERVER_SIF": str(absent)}), \
             patch.object(sb, "apptainer_bin", return_value="/usr/bin/apptainer"):
            active, reason = sb.structural_sandbox_status()
        assert active is False
        assert "apptainer pull" in reason and sb.AGENT_SERVER_IMAGE in reason

    def test_no_config_means_the_driver_stays_on_the_host(self, structural, tmp_path):
        with patch.object(sb, "apptainer_bin", return_value=None):
            assert sb.structural_sandbox_config(tmp_path) is None


class TestWorkspaceConfig:
    def test_project_is_bound_at_its_own_host_path(self, structural, tmp_path):
        """A conda env is not relocatable: bound at the container's default
        /workspace, every `.conda_env/bin/python` shebang points at nothing."""
        code_dir = tmp_path / "proj" / "ws"
        code_dir.mkdir(parents=True)
        cfg = sb.structural_sandbox_config(code_dir)
        assert cfg["bind"] == f"{code_dir}:{code_dir}"
        assert cfg["working_dir"] == str(code_dir)
        # ApptainerWorkspace's own mount_dir would land it here instead.
        assert not cfg["bind"].endswith(":/workspace")
        assert cfg["working_dir"] != "/workspace"

    def test_config_points_at_the_agent_server_image(self, structural, tmp_path):
        cfg = sb.structural_sandbox_config(tmp_path)
        assert cfg["sif_file"] == str(structural)
        assert cfg["kind"] == "apptainer"

    def test_image_tag_is_pinned_not_floating(self):
        """Client and server are two halves of one event protocol. On
        `:latest-python` the newer server sent an Event.parent_id our
        extra="forbid" client refused, and the phase returned empty while the
        agent worked away inside the container."""
        tag = sb.AGENT_SERVER_IMAGE.rsplit(":", 1)[1]
        assert not tag.startswith(("latest", "main")), tag

    def test_default_sif_name_matches_the_sdk_cache_convention(self, tmp_path):
        """Otherwise a hand-pulled image and an SDK-pulled one are two 4 GB
        copies of the same thing."""
        with patch.dict(os.environ, {"ARK_AGENT_SERVER_CACHE": str(tmp_path)}):
            os.environ.pop("ARK_AGENT_SERVER_SIF", None)
            expected = sb.AGENT_SERVER_IMAGE.replace(":", "_").replace("/", "_") + ".sif"
            assert sb.agent_server_sif() == tmp_path / expected


class TestPromptCoherence:
    def test_structural_lane_never_mentions_the_advisory_helper(self, structural):
        """Sending the agent after a ./sandbox/run.sh that is deliberately
        absent turns a working sandbox into a phase full of failed commands."""
        text = sb.experimenter_directive()
        assert "sandbox/run.sh" not in text
        assert "container" in text.lower()

    def test_advisory_lane_still_mandates_the_helper(self, tmp_path):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARK_AGENT_SANDBOX", None)
            with patch.object(sb, "sandbox_available", return_value=True):
                assert "./sandbox/run.sh" in sb.experimenter_directive()

    def test_no_sandbox_at_all_adds_nothing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARK_AGENT_SANDBOX", None)
            with patch.object(sb, "sandbox_available", return_value=False):
                assert sb.experimenter_directive() == ""

    def test_helper_is_not_seeded_under_the_structural_lane(self, structural, tmp_path):
        """There is no apptainer inside the container to nest a second one with;
        the helper would hit its fail-open branch and cry "NO isolation" on a
        run that is in fact isolated."""
        assert sb.write_sandbox_helper(tmp_path) is None
        assert not (tmp_path / "sandbox" / "run.sh").exists()


class TestDriverConfig:
    def _cfg(self, tmp_path):
        rt = sr.OpenHandsSDK("openrouter/x", "openrouter/x")
        with patch.object(sr, "openhands_python", return_value="/usr/bin/python3"):
            cmd = rt.build_command("do it", "stay here", tmp_path)
        path = Path(cmd[2])
        try:
            return json.loads(path.read_text())
        finally:
            path.unlink(missing_ok=True)

    def test_default_run_is_unchanged(self, tmp_path):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARK_AGENT_SANDBOX", None)
            assert self._cfg(tmp_path)["sandbox"] is None

    def test_flag_reaches_the_driver(self, structural, tmp_path):
        assert self._cfg(tmp_path)["sandbox"]["kind"] == "apptainer"

    def test_a_broken_sandbox_module_never_stops_a_phase(self, structural, tmp_path):
        """An unsandboxed phase is a far smaller failure than a phase that
        never starts."""
        with patch.object(sb, "structural_sandbox_config",
                          side_effect=RuntimeError("boom")):
            assert self._cfg(tmp_path)["sandbox"] is None
