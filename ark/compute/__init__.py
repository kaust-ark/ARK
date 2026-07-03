from .base import ComputeBackend
from .local import LocalBackend
from .slurm import SlurmBackend
from .custom import CustomBackend
from .cloud.base import CloudBackend
from .cloud.orchestrator import OrchestratorCloudBackend

# Layer-2 orchestrator launcher types (`orchestrator_compute_backend.type`).
VALID_ORCHESTRATOR_TYPES = frozenset({"local", "slurm", "cloud", "skypilot"})
# Layer-1 experiment backend types (`experiment_compute_backend.type`).
VALID_EXPERIMENT_TYPES = frozenset({"local", "slurm", "cloud", "custom", "skypilot"})

# Invalid (orchestrator, experiment) pairs. The invariant is that the
# orchestrator must be able to reach whatever runs the experiments: a cloud VM
# orchestrator can't drive an on-prem SLURM cluster it has no network path to.
# `skypilot` provisions in a cloud too, so it shares the cloud↔slurm restriction
# (folded Phases 5+6, ADR-0010).
INVALID_COMPUTE_MATRIX = frozenset({
    ("cloud", "slurm"),
    ("skypilot", "slurm"),
})


def validate_config(config: dict):
    """Validate the Layer-2 × Layer-1 compute backend combination (Phase 4)."""
    orch_config = config.get("orchestrator_compute_backend", {"type": "local"})
    exp_config = config.get("experiment_compute_backend") or config.get("compute_backend", {})

    orch_type = orch_config.get("type", "local")
    exp_type = exp_config.get("type", "local")

    if orch_type not in VALID_ORCHESTRATOR_TYPES:
        raise ValueError(
            f"Unknown orchestrator_compute_backend type: {orch_type!r} "
            f"(valid: {sorted(VALID_ORCHESTRATOR_TYPES)})"
        )
    if exp_type not in VALID_EXPERIMENT_TYPES:
        raise ValueError(
            f"Unknown experiment_compute_backend type: {exp_type!r} "
            f"(valid: {sorted(VALID_EXPERIMENT_TYPES)})"
        )

    if (orch_type, exp_type) in INVALID_COMPUTE_MATRIX:
        raise ValueError(
            f"Invalid configuration: orchestrator '{orch_type}' cannot drive "
            f"experiments on '{exp_type}' (no network path between them)."
        )

    # Artifact store block (Phase 3, ADR-0012) — orthogonal to the compute matrix.
    from ark.artifacts import validate_config as _validate_artifact_store
    _validate_artifact_store(config)

def from_config(config: dict, project_name: str, code_dir, log_fn=None, is_orchestrator=False) -> ComputeBackend:
    """Factory: build the right backend from config."""
    if is_orchestrator:
        compute = config.get("orchestrator_compute_backend", {"type": "local"})
    else:
        compute = config.get("experiment_compute_backend") or config.get("compute_backend", {})

    # Backward compatibility: old use_slurm boolean
    if not compute:
        if config.get("use_slurm", False):
            compute = {
                "type": "slurm",
                "job_prefix": config.get("slurm_job_prefix", f"{project_name.upper()}_"),
                "conda_env": config.get("conda_env", project_name),
            }
        else:
            compute = {
                "type": "local",
                "conda_env": config.get("conda_env", project_name),
            }

    backend_type = compute.get("type", "local")

    if backend_type == "slurm":
        return SlurmBackend(config, project_name, code_dir, log_fn)
    elif backend_type == "local":
        return LocalBackend(config, project_name, code_dir, log_fn)
    elif backend_type == "cloud":
        if is_orchestrator:
            return OrchestratorCloudBackend.from_config(config, project_name, code_dir, log_fn)
        else:
            return CloudBackend.from_config(config, project_name, code_dir, log_fn)
    elif backend_type == "custom":
        return CustomBackend(config, project_name, code_dir, log_fn)
    elif backend_type == "skypilot":
        # Layer 1 (experiments) is a ComputeBackend, built here. The Layer-2
        # skypilot orchestrator is a JobLauncher (SkyPilotVmJobLauncher,
        # ark/launcher/skypilot.py), *not* a ComputeBackend — it is constructed by
        # the webapp's orchestrator_launcher_for, never through this factory. So an
        # orchestrator-path skypilot compute build is a wiring error: fail loudly
        # rather than silently return the wrong backend.
        if is_orchestrator:
            raise NotImplementedError(
                "skypilot orchestrator runs via the SkyPilotVmJobLauncher "
                "(ark/launcher/skypilot.py), not the compute factory — this path "
                "should never be reached (folded Phases 5+6, ADR-0010)"
            )
        from .skypilot import SkyPilotBackend
        return SkyPilotBackend(config, project_name, code_dir, log_fn)
    else:
        raise ValueError(f"Unknown compute backend: {backend_type}")

# Add factory to ComputeBackend for convenience (breaking change if moved, but we are allowed to break)
ComputeBackend.from_config = staticmethod(from_config)

__all__ = ["ComputeBackend", "LocalBackend", "SlurmBackend", "CustomBackend", "CloudBackend", "from_config"]
