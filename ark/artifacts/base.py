"""The artifact-storage seam (Phase 3, ADR-0012).

Binary project artifacts — the compiled PDF, an uploaded PDF, figures, the
export bundle — flow through an ``ArtifactStore`` instead of the control plane
reading the orchestrator's disk. The interface is three methods: ``put`` a blob,
``open`` it back, and ``url`` for a direct fetch link. ``url`` returns ``None``
when the caller must proxy the bytes itself — always the case for local storage,
and the case for the object store until presigned URLs are enabled.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import BinaryIO, Optional

_CHUNK = 1024 * 1024


def copy_hashed(src: BinaryIO, dst: BinaryIO) -> tuple[int, str]:
    """Stream ``src`` into ``dst`` in chunks, returning ``(size, sha256_hex)``.

    Shared by every store implementation so byte size and digest are computed in
    a single pass without buffering the whole artifact in memory."""
    h = hashlib.sha256()
    size = 0
    while True:
        chunk = src.read(_CHUNK)
        if not chunk:
            break
        dst.write(chunk)
        h.update(chunk)
        size += len(chunk)
    return size, h.hexdigest()


def hash_stream(src: BinaryIO) -> tuple[int, str]:
    """Size + sha256 of ``src`` without writing it anywhere — used to measure a
    blob that is already stored (e.g. a local file the orchestrator wrote in
    place) rather than re-copying it."""
    h = hashlib.sha256()
    size = 0
    while True:
        chunk = src.read(_CHUNK)
        if not chunk:
            break
        h.update(chunk)
        size += len(chunk)
    return size, h.hexdigest()


@dataclass(frozen=True)
class ArtifactRef:
    """A pointer to a stored artifact: everything needed to fetch it back and
    everything the control plane records about it. Serialized across ``/v1`` as a
    plain dict (``to_dict`` / ``from_dict``)."""

    store_type: str          # "local" | "s3" | "gcs" | "azure"
    key: str                 # store-relative key, e.g. "paper/main.pdf"
    content_type: str = ""
    size: int = 0
    sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ArtifactRef":
        fields = {"store_type", "key", "content_type", "size", "sha256"}
        return cls(**{k: v for k, v in d.items() if k in fields})


class ArtifactStore(ABC):
    """Read/write seam for project artifacts. See module docstring and ADR-0012."""

    #: Identifies the backend in an ``ArtifactRef`` ("local", "s3", …).
    store_type: str = ""

    @abstractmethod
    def put(self, key: str, stream: BinaryIO, *, content_type: str = "") -> ArtifactRef:
        """Store ``stream`` under ``key`` and return a reference to it."""

    def put_path(self, src, key: str, *, content_type: str = "") -> ArtifactRef:
        """Store the file at ``src`` under ``key``. Convenience over ``put``;
        overridden by local storage to skip the copy when the file is already at
        the destination path."""
        from pathlib import Path
        with open(Path(src), "rb") as fh:
            return self.put(key, fh, content_type=content_type)

    @abstractmethod
    def open(self, ref: ArtifactRef) -> BinaryIO:
        """Open the artifact for reading (binary mode)."""

    @abstractmethod
    def url(self, ref: ArtifactRef, *, expires: int = 3600) -> Optional[str]:
        """A direct fetch URL for the artifact, or ``None`` if the caller must
        proxy the bytes via ``open``. Local storage always returns ``None``."""

    def fspath(self, ref: ArtifactRef) -> Optional[str]:
        """Local filesystem path of the artifact, or ``None`` if it isn't a plain
        local file (object stores return ``None``). Lets the dashboard serve a
        local artifact with a ``FileResponse`` — preserving HTTP Range support
        and file-handle cleanup — instead of proxying it."""
        return None
