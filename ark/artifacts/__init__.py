"""Artifact storage seam (Phase 3, ADR-0012).

``from_config`` builds the store selected by the ``artifact_store`` config block;
``validate_config`` checks that block. Defaults to a filesystem store rooted at
the project dir, so local dev and SLURM behave exactly as before.
"""

from .base import ArtifactRef, ArtifactStore, copy_hashed, hash_stream
from .local import LocalArtifactStore
from .object_store import ObjectArtifactStore
from .publish import publish_paper_artifacts

VALID_TYPES = ("local", "s3", "gcs", "azure")

# Object-store providers the factory can construct (all of VALID_TYPES but local).
_CLIENTS_SUPPORTED = ("s3", "gcs", "azure")

# Config keys consumed by the factory itself; everything else in the block is
# passed through to the object-store client (region, endpoint_url, project,
# account_url, connection_string, …).
_FACTORY_KEYS = ("type", "bucket", "prefix")


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
    project dir (zero behavior change for local dev / SLURM — ADR-0012).

    For ``s3``/``gcs``/``azure`` the store is object-backed; ``code_dir`` is
    unused (blobs live in the bucket). The provider SDK is imported lazily on
    first use, so building the store here never requires a cloud SDK."""
    store = config.get("artifact_store") or {"type": "local"}
    stype = store.get("type", "local")
    if stype == "local":
        return LocalArtifactStore(code_dir)
    if stype in _CLIENTS_SUPPORTED:
        bucket = store.get("bucket")
        if not bucket:
            raise ValueError(f"artifact_store type '{stype}' requires a 'bucket'.")
        client_opts = {k: v for k, v in store.items() if k not in _FACTORY_KEYS}
        return ObjectArtifactStore(
            stype, bucket, store.get("prefix", ""), client_opts=client_opts)
    raise ValueError(
        f"Unknown artifact_store type: {stype!r} (expected one of {list(VALID_TYPES)})."
    )


__all__ = [
    "ArtifactStore",
    "ArtifactRef",
    "LocalArtifactStore",
    "ObjectArtifactStore",
    "copy_hashed",
    "hash_stream",
    "publish_paper_artifacts",
    "from_config",
    "validate_config",
    "VALID_TYPES",
]
