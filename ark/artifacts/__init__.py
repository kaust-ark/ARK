"""Artifact storage seam (Phase 3, ADR-0012).

``from_config`` builds the store selected by the ``artifact_store`` config block;
``validate_config`` checks that block. Defaults to a filesystem store rooted at
the project dir, so local dev and SLURM behave exactly as before.
"""

from .base import ArtifactRef, ArtifactStore, copy_hashed
from .local import LocalArtifactStore

# Object stores (s3/gcs/azure) are accepted by config validation but built in a
# later Phase 3 PR; only ``local`` is constructable today.
VALID_TYPES = ("local", "s3", "gcs", "azure")


def validate_config(config: dict) -> None:
    """Validate the ``artifact_store`` block. Object stores require a bucket.

    Orthogonal to the compute matrix — called from ``ark.compute.validate_config``
    so existing callers validate the artifact store for free."""
    store = config.get("artifact_store") or {"type": "local"}
    stype = store.get("type", "local")
    if stype not in VALID_TYPES:
        raise ValueError(
            f"Unknown artifact_store type: {stype!r} (expected one of {list(VALID_TYPES)})."
        )
    if stype != "local" and not store.get("bucket"):
        raise ValueError(f"artifact_store type '{stype}' requires a 'bucket'.")


def from_config(config: dict, code_dir) -> ArtifactStore:
    """Build the artifact store from config, defaulting to local rooted at the
    project dir (zero behavior change for local dev / SLURM — ADR-0012)."""
    store = config.get("artifact_store") or {"type": "local"}
    stype = store.get("type", "local")
    if stype == "local":
        return LocalArtifactStore(code_dir)
    raise NotImplementedError(
        f"artifact_store type '{stype}' is not implemented yet "
        f"(object stores land in a later Phase 3 PR)."
    )


__all__ = [
    "ArtifactStore",
    "ArtifactRef",
    "LocalArtifactStore",
    "copy_hashed",
    "from_config",
    "validate_config",
    "VALID_TYPES",
]
