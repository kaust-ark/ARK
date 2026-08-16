"""
Compute backend factory tests — pure Python logic, no cloud.

The native cloud backend (AWS/GCP/Azure VM provisioning) was removed; SkyPilot is
the only cloud path now, and its backend is exercised in test_skypilot_backend.py.
"""

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# UNIT TESTS — factory dispatch
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

    def test_creates_skypilot_backend(self, tmp_path):
        from ark.compute import from_config
        from ark.compute.skypilot import SkyPilotBackend
        backend = from_config({"compute_backend": {"type": "skypilot"}}, "proj", tmp_path)
        assert isinstance(backend, SkyPilotBackend)

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

    def test_retired_cloud_backend_rejected(self, tmp_path):
        """The native `cloud` backend was removed — it must no longer resolve."""
        from ark.compute import from_config
        with pytest.raises(ValueError, match="Unknown compute backend"):
            from_config({"compute_backend": {"type": "cloud", "provider": "gcp"}},
                        "proj", tmp_path)

    def test_skypilot_orchestrator_build_is_a_wiring_error(self, tmp_path):
        """The skypilot orchestrator runs via SkyPilotVmJobLauncher, not the
        compute factory — an orchestrator-path build must fail loudly."""
        from ark.compute import from_config
        with pytest.raises(NotImplementedError):
            from_config({"orchestrator_compute_backend": {"type": "skypilot"}},
                        "proj", tmp_path, is_orchestrator=True)


class TestCloudBackendRequiresConfiguration:
    """A cloud backend must be refused where no cloud is configured.

    The orchestrator's "☁️ Cloud" radio was never gated on operator settings
    (the per-cloud experiment chips always were), so on a deployment with no
    cloud project and no `sky` installed it stayed selectable. The failure then
    landed after the project row existed, as a missing optional dependency
    minutes into a "running" project. Reject it at the write path instead.
    """

    def _reject(self, value, configured):
        from unittest.mock import patch
        from website.dashboard import routes
        with patch.object(routes, "_any_cloud_configured", return_value=configured):
            routes._reject_unknown_backend(value, frozenset({"local", "slurm", "skypilot"}),
                                           "orchestrator")

    def test_cloud_is_refused_when_nothing_is_configured(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            self._reject("skypilot", configured=False)
        assert e.value.status_code == 400
        assert "not configured" in e.value.detail

    def test_the_compound_selector_is_refused_too(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            self._reject("skypilot:gcp", configured=False)

    def test_cloud_is_admitted_once_configured(self):
        self._reject("skypilot:gcp", configured=True)      # must not raise

    def test_local_and_slurm_are_never_touched_by_this_gate(self):
        self._reject("local", configured=False)
        self._reject("slurm", configured=False)
