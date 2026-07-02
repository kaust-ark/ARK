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

    def test_object_store_not_implemented_yet(self, tmp_path):
        from ark.artifacts import from_config
        with pytest.raises(NotImplementedError, match="not implemented yet"):
            from_config({"artifact_store": {"type": "s3", "bucket": "b"}}, tmp_path)


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
