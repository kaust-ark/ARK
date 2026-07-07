"""Artifact store seam — Phase 3, ADR-0012.

Pure filesystem / factory logic; no cloud, no network.
"""

import hashlib
import io

import pytest


# ---------------------------------------------------------------------------
# LocalArtifactStore — round-trips, key mapping, path-escape guard
# ---------------------------------------------------------------------------

class TestLocalArtifactStore:
    def _store(self, tmp_path):
        from ark.artifacts.local import LocalArtifactStore
        return LocalArtifactStore(tmp_path)

    def test_put_writes_at_key_path_and_returns_ref(self, tmp_path):
        store = self._store(tmp_path)
        data = b"%PDF-1.7 hello"
        ref = store.put("paper/main.pdf", io.BytesIO(data), content_type="application/pdf")

        # Key maps directly onto <root>/key — the location already used today.
        written = tmp_path / "paper" / "main.pdf"
        assert written.read_bytes() == data
        assert ref.store_type == "local"
        assert ref.key == "paper/main.pdf"
        assert ref.content_type == "application/pdf"
        assert ref.size == len(data)
        assert ref.sha256 == hashlib.sha256(data).hexdigest()

    def test_open_round_trips_the_bytes(self, tmp_path):
        store = self._store(tmp_path)
        data = b"figure-bytes" * 1000
        ref = store.put("paper/figures/f1.png", io.BytesIO(data))
        with store.open(ref) as fh:
            assert fh.read() == data

    def test_url_is_none_for_local(self, tmp_path):
        store = self._store(tmp_path)
        ref = store.put("x.txt", io.BytesIO(b"z"))
        assert store.url(ref) is None

    def test_put_creates_missing_parent_dirs(self, tmp_path):
        store = self._store(tmp_path)
        store.put("a/b/c/deep.bin", io.BytesIO(b"1"))
        assert (tmp_path / "a" / "b" / "c" / "deep.bin").exists()

    def test_key_escaping_root_is_rejected(self, tmp_path):
        store = self._store(tmp_path)
        with pytest.raises(ValueError, match="escapes store root"):
            store.put("../evil.txt", io.BytesIO(b"nope"))

    def test_put_overwrites_same_key(self, tmp_path):
        store = self._store(tmp_path)
        store.put("paper/main.pdf", io.BytesIO(b"v1"))
        ref = store.put("paper/main.pdf", io.BytesIO(b"v2-longer"))
        assert (tmp_path / "paper" / "main.pdf").read_bytes() == b"v2-longer"
        assert ref.size == len(b"v2-longer")

    def test_put_path_same_location_measures_in_place(self, tmp_path):
        # The orchestrator writes the PDF directly to <root>/paper/main.pdf.
        # put_path must NOT re-open the destination for writing (which would
        # truncate the file it's reading) — it measures the existing file.
        store = self._store(tmp_path)
        pdf = tmp_path / "paper" / "main.pdf"
        pdf.parent.mkdir(parents=True)
        data = b"%PDF already here" * 100
        pdf.write_bytes(data)
        ref = store.put_path(pdf, "paper/main.pdf", content_type="application/pdf")
        assert pdf.read_bytes() == data          # untouched
        assert ref.size == len(data)
        assert ref.sha256 == hashlib.sha256(data).hexdigest()

    def test_put_path_from_other_location_copies(self, tmp_path):
        store = self._store(tmp_path)
        src = tmp_path / "external" / "src.pdf"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"external-bytes")
        ref = store.put_path(src, "paper/main.pdf")
        assert (tmp_path / "paper" / "main.pdf").read_bytes() == b"external-bytes"
        assert ref.key == "paper/main.pdf"


# ---------------------------------------------------------------------------
# publish_paper_artifacts — walks the produced files, put + register each
# ---------------------------------------------------------------------------

class _FakeCP:
    def __init__(self):
        self.registered = []
        self.uploaded = []

    def register_artifact(self, **ref):
        self.registered.append(ref)

    def upload_artifact(self, key, data, *, kind="", content_type=""):
        self.uploaded.append({"key": key, "kind": kind,
                              "content_type": content_type, "size": len(data)})


class TestPublishPaperArtifacts:
    def _project(self, tmp_path):
        (tmp_path / "paper" / "figures").mkdir(parents=True)
        (tmp_path / "paper" / "main.pdf").write_bytes(b"%PDF-1.7 body enough")
        (tmp_path / "paper" / "figures" / "f1.png").write_bytes(b"\x89PNGfig")
        (tmp_path / "paper" / "figures" / "notes.txt").write_bytes(b"ignored")
        return tmp_path

    def test_local_store_pushes_bytes_via_upload(self, tmp_path):
        # A local store keeps bytes only where the run executes, so publishing
        # must ship the BYTES to the control plane (upload_artifact), not a bare
        # reference it can't resolve.
        from ark.artifacts import LocalArtifactStore, publish_paper_artifacts
        pdir = self._project(tmp_path)
        cp = _FakeCP()
        n = publish_paper_artifacts(LocalArtifactStore(pdir), cp, pdir)

        kinds = sorted(r["kind"] for r in cp.uploaded)
        keys = {r["key"] for r in cp.uploaded}
        assert n == 2                                   # pdf + one figure (txt skipped)
        assert kinds == ["figure", "pdf"]
        assert "paper/main.pdf" in keys
        assert "paper/figures/f1.png" in keys
        assert all(r["size"] > 0 for r in cp.uploaded)  # real bytes shipped
        assert cp.registered == []                      # local never registers a bare ref

    def test_object_store_registers_ref_without_uploading_bytes(self, tmp_path):
        # A shared object store already holds the bytes (put_path uploaded them),
        # so publishing only registers the reference — no byte re-push.
        from ark.artifacts import publish_paper_artifacts
        from ark.artifacts.base import ArtifactRef
        pdir = self._project(tmp_path)

        class _ObjStore:
            def put_path(self, src, key, *, content_type=""):
                return ArtifactRef("s3", key, content_type, 10, "deadbeef")

        cp = _FakeCP()
        n = publish_paper_artifacts(_ObjStore(), cp, pdir)
        assert n == 2
        assert cp.uploaded == []
        assert {r["key"] for r in cp.registered} == {"paper/main.pdf",
                                                     "paper/figures/f1.png"}

    def test_pdf_untouched_when_store_roots_at_project(self, tmp_path):
        from ark.artifacts import LocalArtifactStore, publish_paper_artifacts
        pdir = self._project(tmp_path)
        original = (pdir / "paper" / "main.pdf").read_bytes()
        publish_paper_artifacts(LocalArtifactStore(pdir), _FakeCP(), pdir)
        assert (pdir / "paper" / "main.pdf").read_bytes() == original

    def test_nothing_to_publish_returns_zero(self, tmp_path):
        from ark.artifacts import LocalArtifactStore, publish_paper_artifacts
        assert publish_paper_artifacts(LocalArtifactStore(tmp_path), _FakeCP(), tmp_path) == 0

    def test_publish_failure_is_swallowed(self, tmp_path):
        from ark.artifacts import LocalArtifactStore, publish_paper_artifacts
        pdir = self._project(tmp_path)

        class _BoomCP:
            def upload_artifact(self, *a, **k):
                raise RuntimeError("control plane down")

        # An upload failure must not propagate (best-effort publishing).
        n = publish_paper_artifacts(LocalArtifactStore(pdir), _BoomCP(), pdir)
        assert n == 0


# ---------------------------------------------------------------------------
# ArtifactRef — serialization contract used across /v1
# ---------------------------------------------------------------------------

class TestArtifactRef:
    def test_to_dict_from_dict_round_trip(self):
        from ark.artifacts.base import ArtifactRef
        ref = ArtifactRef("s3", "paper/main.pdf", "application/pdf", 12, "abc")
        assert ArtifactRef.from_dict(ref.to_dict()) == ref

    def test_from_dict_ignores_unknown_fields(self):
        from ark.artifacts.base import ArtifactRef
        ref = ArtifactRef.from_dict(
            {"store_type": "local", "key": "k", "kind": "pdf", "extra": 1}
        )
        assert ref.store_type == "local" and ref.key == "k"


# ---------------------------------------------------------------------------
# ObjectArtifactStore — provider-agnostic logic against an in-memory fake client
# (no boto3/gcs/azure SDK, no network). The real clients are thin SDK wrappers.
# ---------------------------------------------------------------------------

class _FakeObjectClient:
    """In-memory stand-in for _S3Client / _GCSClient / _AzureClient."""

    def __init__(self):
        self.blobs = {}  # full key (prefix applied) -> (bytes, content_type)

    def upload(self, key, fileobj, content_type):
        self.blobs[key] = (fileobj.read(), content_type)

    def download(self, key):
        from ark.artifacts.object_store import _BlobStream
        return _BlobStream(io.BytesIO(self.blobs[key][0]))


class TestObjectArtifactStore:
    def _store(self, prefix="", client=None):
        from ark.artifacts.object_store import ObjectArtifactStore
        return ObjectArtifactStore(
            "s3", "my-bucket", prefix, client=client or _FakeObjectClient())

    def test_put_returns_ref_with_size_and_digest(self):
        client = _FakeObjectClient()
        store = self._store(client=client)
        data = b"%PDF-1.7 " + b"body" * 500
        ref = store.put("paper/main.pdf", io.BytesIO(data), content_type="application/pdf")

        assert ref.store_type == "s3"
        assert ref.key == "paper/main.pdf"        # store-relative, prefix-free
        assert ref.content_type == "application/pdf"
        assert ref.size == len(data)
        assert ref.sha256 == hashlib.sha256(data).hexdigest()
        # Uploaded bytes match, tagged with the content type.
        assert client.blobs["paper/main.pdf"] == (data, "application/pdf")

    def test_open_round_trips_the_bytes(self):
        store = self._store()
        data = b"figure-bytes" * 1000
        ref = store.put("paper/figures/f1.png", io.BytesIO(data))
        with store.open(ref) as fh:
            assert fh.read() == data

    def test_url_is_none_proxy_for_now(self):
        store = self._store()
        ref = store.put("x.txt", io.BytesIO(b"z"))
        assert store.url(ref) is None

    def test_fspath_is_none_for_object_store(self):
        from ark.artifacts.base import ArtifactRef
        store = self._store()
        # Object store has no local path -> dashboard proxies via open().
        assert store.fspath(ArtifactRef("s3", "k")) is None

    def test_prefix_applied_on_put_and_open(self):
        client = _FakeObjectClient()
        store = self._store(prefix="ark/", client=client)
        data = b"prefixed"
        ref = store.put("paper/main.pdf", io.BytesIO(data))
        # Bucket key carries the prefix; the ref stays provider-neutral.
        assert "ark/paper/main.pdf" in client.blobs
        assert ref.key == "paper/main.pdf"
        with store.open(ref) as fh:
            assert fh.read() == data

    def test_client_is_lazy(self):
        # Building the store must not build the SDK client (no boto3 import).
        from ark.artifacts.object_store import ObjectArtifactStore
        store = ObjectArtifactStore("s3", "b", client_opts={"region": "x"})
        assert store._client is None

    def test_blobstream_partial_reads_and_close(self):
        from ark.artifacts.object_store import _BlobStream
        underlying = io.BytesIO(b"abcdef")
        s = _BlobStream(underlying)
        assert s.read(3) == b"abc"
        assert s.read(-1) == b"def"     # negative size drains the rest
        s.close()
        assert underlying.closed         # close propagates to the reader

    def test_unknown_provider_rejected(self):
        from ark.artifacts.object_store import ObjectArtifactStore
        with pytest.raises(ValueError, match="unknown object store provider"):
            ObjectArtifactStore("ftp", "b")


# ---------------------------------------------------------------------------
# Factory + validation
# ---------------------------------------------------------------------------

class TestArtifactFactory:
    def test_default_is_local_rooted_at_code_dir(self, tmp_path):
        from ark.artifacts import from_config
        from ark.artifacts.local import LocalArtifactStore
        store = from_config({}, tmp_path)
        assert isinstance(store, LocalArtifactStore)
        assert store.root == tmp_path

    def test_explicit_local(self, tmp_path):
        from ark.artifacts import from_config
        from ark.artifacts.local import LocalArtifactStore
        store = from_config({"artifact_store": {"type": "local"}}, tmp_path)
        assert isinstance(store, LocalArtifactStore)

    @pytest.mark.parametrize("provider", ["s3", "gcs", "azure"])
    def test_object_store_built_from_config(self, tmp_path, provider):
        from ark.artifacts import from_config, ObjectArtifactStore
        store = from_config(
            {"artifact_store": {"type": provider, "bucket": "b", "prefix": "ark/"}},
            tmp_path,
        )
        assert isinstance(store, ObjectArtifactStore)
        assert store.store_type == provider
        assert store.bucket == "b"
        assert store.prefix == "ark"          # stripped of surrounding slashes

    def test_object_store_passes_through_client_opts(self, tmp_path):
        # Keys beyond type/bucket/prefix flow to the provider client (region,
        # endpoint_url, …) and are NOT mistaken for factory keys.
        from ark.artifacts import from_config
        store = from_config(
            {"artifact_store": {"type": "s3", "bucket": "b",
                                "region": "us-west-2", "endpoint_url": "http://minio:9000"}},
            tmp_path,
        )
        assert store._client_opts == {"region": "us-west-2",
                                      "endpoint_url": "http://minio:9000"}

    def test_object_store_without_bucket_raises(self, tmp_path):
        from ark.artifacts import from_config
        with pytest.raises(ValueError, match="requires a 'bucket'"):
            from_config({"artifact_store": {"type": "s3"}}, tmp_path)

    def test_unknown_type_raises(self, tmp_path):
        from ark.artifacts import from_config
        with pytest.raises(ValueError, match="Unknown artifact_store type"):
            from_config({"artifact_store": {"type": "quantum"}}, tmp_path)


class TestArtifactValidation:
    def test_local_ok(self):
        from ark.artifacts import validate_config
        validate_config({"artifact_store": {"type": "local"}})  # no raise

    def test_missing_block_defaults_ok(self):
        from ark.artifacts import validate_config
        validate_config({})  # no raise

    def test_unknown_type_raises(self):
        from ark.artifacts import validate_config
        with pytest.raises(ValueError, match="Unknown artifact_store type"):
            validate_config({"artifact_store": {"type": "quantum"}})

    def test_object_store_without_bucket_raises(self):
        from ark.artifacts import validate_config
        with pytest.raises(ValueError, match="requires a 'bucket'"):
            validate_config({"artifact_store": {"type": "s3"}})

    def test_object_store_with_bucket_ok(self):
        from ark.artifacts import validate_config
        validate_config({"artifact_store": {"type": "gcs", "bucket": "b"}})  # no raise

    def test_compute_validate_config_delegates_to_artifacts(self):
        """The webapp calls ark.compute.validate_config; it must catch a bad
        artifact_store block too."""
        from ark.compute import validate_config
        with pytest.raises(ValueError, match="Unknown artifact_store type"):
            validate_config({"artifact_store": {"type": "bogus"}})
