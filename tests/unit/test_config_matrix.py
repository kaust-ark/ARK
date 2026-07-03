"""Phase 4 — Layer-2 (orchestrator) × Layer-1 (experiment) config matrix.

`validate_config` must accept every reachable combination and reject the ones
where the orchestrator can't reach its experiments (cloud VM ↔ on-prem SLURM),
plus reject unknown backend types."""

import pytest

from ark.compute import (
    validate_config, VALID_ORCHESTRATOR_TYPES, VALID_EXPERIMENT_TYPES,
    INVALID_COMPUTE_MATRIX,
)


def _cfg(orch, exp):
    return {
        "orchestrator_compute_backend": {"type": orch},
        "experiment_compute_backend": {"type": exp},
    }


@pytest.mark.parametrize("orch", sorted(VALID_ORCHESTRATOR_TYPES))
@pytest.mark.parametrize("exp", sorted(VALID_EXPERIMENT_TYPES))
def test_full_matrix(orch, exp):
    cfg = _cfg(orch, exp)
    if (orch, exp) in INVALID_COMPUTE_MATRIX:
        with pytest.raises(ValueError):
            validate_config(cfg)
    else:
        validate_config(cfg)  # must not raise


def test_cloud_orchestrator_with_slurm_experiments_rejected():
    """The invariant that predates Phase 4 — kept and now matrix-driven."""
    with pytest.raises(ValueError, match="cannot drive"):
        validate_config(_cfg("cloud", "slurm"))


def test_skypilot_orchestrator_with_slurm_experiments_rejected():
    """SkyPilot provisions in a cloud too — same no-network-path-to-on-prem-SLURM
    restriction as `cloud` (folded Phases 5+6, ADR-0010)."""
    with pytest.raises(ValueError, match="cannot drive"):
        validate_config(_cfg("skypilot", "slurm"))


def test_skypilot_is_a_valid_type_on_both_layers():
    validate_config(_cfg("skypilot", "skypilot"))  # must not raise
    validate_config(_cfg("skypilot", "cloud"))
    validate_config(_cfg("cloud", "skypilot"))


def test_unknown_orchestrator_type_rejected():
    with pytest.raises(ValueError, match="orchestrator_compute_backend"):
        validate_config(_cfg("nope", "local"))


def test_unknown_experiment_type_rejected():
    with pytest.raises(ValueError, match="experiment_compute_backend"):
        validate_config(_cfg("local", "nope"))


def test_defaults_to_local_local():
    validate_config({})  # empty config → local/local, valid


def test_legacy_compute_backend_key_honored():
    # experiment backend can come from the legacy `compute_backend` key
    validate_config({
        "orchestrator_compute_backend": {"type": "cloud"},
        "compute_backend": {"type": "cloud"},
    })
    with pytest.raises(ValueError):
        validate_config({
            "orchestrator_compute_backend": {"type": "cloud"},
            "compute_backend": {"type": "slurm"},
        })
