"""Filesystem-backed artifact store — the default for local dev and SLURM on a
shared mount (ADR-0012)."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Optional

from .base import ArtifactRef, ArtifactStore, copy_hashed, hash_stream


class LocalArtifactStore(ArtifactStore):
    """Stores artifacts on the local filesystem, rooted at ``root``.

    Keys map directly onto paths under ``root`` — so ``paper/main.pdf`` resolves
    to ``<root>/paper/main.pdf``, the exact location the orchestrator already
    writes to. Routing the local/SLURM path through the store therefore leaves
    its on-disk behavior unchanged while still exercising the seam. ``url``
    returns ``None``: the dashboard proxies these bytes.
    """

    store_type = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def _resolve(self, key: str) -> Path:
        """Resolve ``key`` under ``root``, refusing paths that escape it."""
        root = self.root.resolve()
        path = (root / key).resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"artifact key escapes store root: {key!r}")
        return path

    def put(self, key: str, stream: BinaryIO, *, content_type: str = "") -> ArtifactRef:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            size, digest = copy_hashed(stream, f)
        return ArtifactRef(store_type=self.store_type, key=key,
                           content_type=content_type, size=size, sha256=digest)

    def put_path(self, src, key: str, *, content_type: str = "") -> ArtifactRef:
        # The orchestrator writes many artifacts (the PDF, figures) directly to
        # their final path under the project dir. When ``src`` already resolves to
        # ``<root>/key`` we must NOT re-open the destination for writing (that
        # would truncate the file we're reading) — just measure it in place.
        src = Path(src)
        dest = self._resolve(key)
        if src.resolve() == dest:
            with open(dest, "rb") as fh:
                size, digest = hash_stream(fh)
            return ArtifactRef(store_type=self.store_type, key=key,
                               content_type=content_type, size=size, sha256=digest)
        return super().put_path(src, key, content_type=content_type)

    def open(self, ref: ArtifactRef) -> BinaryIO:
        return open(self._resolve(ref.key), "rb")

    def url(self, ref: ArtifactRef, *, expires: int = 3600) -> Optional[str]:
        return None
