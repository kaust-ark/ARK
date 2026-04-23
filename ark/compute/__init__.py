from .base import ComputeBackend
from .local import LocalBackend
from .slurm import SlurmBackend
from .custom import CustomBackend
from .cloud.base import CloudBackend

def from_config(config: dict, project_name: str, code_dir, log_fn=None) -> ComputeBackend:
    """Factory: build the right backend from config."""
    compute = config.get("compute_backend", {})

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
        return CloudBackend.from_config(config, project_name, code_dir, log_fn)
    elif backend_type == "custom":
        return CustomBackend(config, project_name, code_dir, log_fn)
    else:
        raise ValueError(f"Unknown compute backend: {backend_type}")

# Add factory to ComputeBackend for convenience (breaking change if moved, but we are allowed to break)
ComputeBackend.from_config = staticmethod(from_config)

__all__ = ["ComputeBackend", "LocalBackend", "SlurmBackend", "CustomBackend", "CloudBackend", "from_config"]
