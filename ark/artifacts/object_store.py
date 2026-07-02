"""Object-storage artifact store — S3 / GCS / Azure Blob (Phase 3, ADR-0012).

Under BYOC the control plane and the orchestrator share no filesystem, so binary
artifacts (the compiled PDF, figures, the export bundle) live in an object store —
**preferably the user's own bucket**. The orchestrator ``put``s each blob; the
dashboard resolves it back through the same store and proxies the bytes (``url``
returns ``None`` today; presigned URLs are a later drop-in — see ADR-0012).

Design notes:

- **Lazy everything.** The provider SDK (boto3 / google-cloud-storage /
  azure-storage-blob) is imported, and the client built, only on first ``put`` /
  ``open``. Importing ``ark.artifacts`` — and building the store from config on
  the dashboard to answer a *local* project's request — therefore never requires
  a cloud SDK to be installed.
- **Credentials come from the SDK's standard chain.** No creds are threaded
  through config; the store relies on the ambient environment (``AWS_*`` /
  ``GOOGLE_APPLICATION_CREDENTIALS`` / ``AZURE_STORAGE_CONNECTION_STRING``), which
  under BYOC is the same env the cloud compute backend runs in — so artifact creds
  "default to the cloud backend's" without extra plumbing (ADR-0012). Config may
  still override endpoint/region/account for non-default deployments.
- **Uniform download stream.** ``open`` returns a ``_BlobStream`` that guarantees
  ``read``/``close`` and the context-manager protocol regardless of the provider
  SDK's native reader, so the dashboard's proxy path is provider-agnostic.
"""

from __future__ import annotations

import io
import os
import tempfile
from typing import BinaryIO, Optional

from .base import ArtifactRef, ArtifactStore, copy_hashed

# Blobs are buffered here (to compute size + sha256 before upload) up to this many
# bytes in memory, spilling to a temp file beyond it. Papers/figures are small; the
# spill only guards against a pathologically large artifact.
_SPOOL_MAX = 32 * 1024 * 1024


class _BlobStream(io.RawIOBase):
    """Adapts a provider SDK's readable object to a uniform binary stream.

    The dashboard proxies an artifact by looping ``read(1MB)`` and closing the
    handle (and elsewhere uses ``with store.open(ref) as fh``). boto3's
    ``StreamingBody``, GCS's ``BlobReader`` and Azure's ``StorageStreamDownloader``
    each expose ``read`` a little differently and don't all support the
    context-manager protocol — wrapping them here makes the proxy path
    provider-agnostic and guarantees the handle is closed exactly once.
    """

    def __init__(self, reader, *, closer=None):
        self._reader = reader
        self._closer = closer

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self._reader.read()
        return self._reader.read(size)

    def close(self) -> None:
        try:
            if self._closer is not None:
                self._closer()
            elif hasattr(self._reader, "close"):
                self._reader.close()
        finally:
            super().close()


class _S3Client:
    """boto3 wrapper. ``bucket`` is an S3 bucket; ``region``/``endpoint_url`` let a
    non-AWS S3-compatible store (MinIO, R2, …) be targeted."""

    def __init__(self, bucket: str, *, region: Optional[str] = None,
                 endpoint_url: Optional[str] = None, **_):
        import boto3  # lazy: only when an S3 store is actually used
        self.bucket = bucket
        self._c = boto3.client("s3", region_name=region or None,
                               endpoint_url=endpoint_url or None)

    def upload(self, key: str, fileobj: BinaryIO, content_type: str) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self._c.upload_fileobj(fileobj, self.bucket, key, ExtraArgs=extra)

    def download(self, key: str) -> _BlobStream:
        obj = self._c.get_object(Bucket=self.bucket, Key=key)
        return _BlobStream(obj["Body"])


class _GCSClient:
    """google-cloud-storage wrapper. ``bucket`` is a GCS bucket."""

    def __init__(self, bucket: str, *, project: Optional[str] = None, **_):
        from google.cloud import storage  # lazy
        self._bucket = storage.Client(project=project or None).bucket(bucket)

    def upload(self, key: str, fileobj: BinaryIO, content_type: str) -> None:
        self._bucket.blob(key).upload_from_file(
            fileobj, content_type=content_type or None)

    def download(self, key: str) -> _BlobStream:
        return _BlobStream(self._bucket.blob(key).open("rb"))


class _AzureClient:
    """azure-storage-blob wrapper. Here the ``bucket`` is a *container* name.

    Auth prefers a connection string (config or ``AZURE_STORAGE_CONNECTION_STRING``)
    and otherwise an ``account_url`` with the default Azure credential chain."""

    def __init__(self, bucket: str, *, account_url: Optional[str] = None,
                 connection_string: Optional[str] = None, **_):
        from azure.storage.blob import BlobServiceClient  # lazy
        conn = connection_string or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn:
            svc = BlobServiceClient.from_connection_string(conn)
        elif account_url:
            from azure.identity import DefaultAzureCredential
            svc = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        else:
            raise ValueError(
                "azure artifact_store needs a connection_string "
                "(or AZURE_STORAGE_CONNECTION_STRING) or an account_url")
        self._container = svc.get_container_client(bucket)

    def upload(self, key: str, fileobj: BinaryIO, content_type: str) -> None:
        from azure.storage.blob import ContentSettings
        settings = ContentSettings(content_type=content_type) if content_type else None
        self._container.upload_blob(
            name=key, data=fileobj, overwrite=True, content_settings=settings)

    def download(self, key: str) -> _BlobStream:
        downloader = self._container.download_blob(key)
        return _BlobStream(downloader)


_CLIENTS = {"s3": _S3Client, "gcs": _GCSClient, "azure": _AzureClient}


class ObjectArtifactStore(ArtifactStore):
    """Stores artifacts in an object store (S3/GCS/Azure). See module docstring.

    ``key`` in an :class:`ArtifactRef` stays store-relative and provider-neutral
    (e.g. ``paper/main.pdf``) — the same value a ``LocalArtifactStore`` records —
    so a project can move between backends without rewriting refs. The optional
    ``prefix`` is a bucket-internal namespace applied on the way in and out, never
    part of the stored key.
    """

    def __init__(self, provider: str, bucket: str, prefix: str = "", *,
                 client=None, client_opts: Optional[dict] = None):
        if provider not in _CLIENTS:
            raise ValueError(f"unknown object store provider: {provider!r}")
        self.store_type = provider
        self.bucket = bucket
        self.prefix = (prefix or "").strip("/")
        self._client = client                       # injectable for tests
        self._client_opts = client_opts or {}

    @property
    def client(self):
        """The provider client, built (and its SDK imported) on first use."""
        if self._client is None:
            self._client = _CLIENTS[self.store_type](self.bucket, **self._client_opts)
        return self._client

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, stream: BinaryIO, *, content_type: str = "") -> ArtifactRef:
        # Buffer once to compute size + sha256 (the control plane records both),
        # then upload from the start of the buffer.
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX, mode="w+b") as buf:
            size, digest = copy_hashed(stream, buf)
            buf.seek(0)
            self.client.upload(self._full_key(key), buf, content_type)
        return ArtifactRef(store_type=self.store_type, key=key,
                           content_type=content_type, size=size, sha256=digest)

    def open(self, ref: ArtifactRef) -> BinaryIO:
        return self.client.download(self._full_key(ref.key))

    def url(self, ref: ArtifactRef, *, expires: int = 3600) -> Optional[str]:
        # Proxy for now; presigned URLs are the planned drop-in (ADR-0012).
        return None
