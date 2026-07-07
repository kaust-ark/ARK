"""Resumable-state plumbing (ADR-0013 + ADR-0012 result durability).

Covers the four pieces that let a run survive its VM dying and resume on a
replacement instead of restarting at iteration 0:

1. ``checkpoint`` / ``research_state`` are part of the projected state docs.
2. ``rehydrate_state_docs`` refills a *missing* local doc from the projection.
3. The resume pointer is reconciled newest-wins (control plane is source of
   truth; local YAML is a cache).
4. ``publish_result_artifacts`` ships experiment results off the VM.

Pure logic — no network. A tiny in-memory fake stands in for the control plane.
"""

import io

import yaml

from ark.orchestrator.state_publish import (
    _STATE_DOCS,
    publish_state_docs,
    rehydrate_state_docs,
)


class FakeCP:
    """Minimal control-plane double: an in-memory state-doc + artifact store."""

    def __init__(self):
        self.state: dict[str, dict] = {}
        self.uploaded: list[tuple[str, bytes, str]] = []
        self.registered: list[dict] = []
        # key → (bytes, kind, sha256) for the artifact catalog / download path.
        self.blobs: dict[str, tuple[bytes, str, str]] = {}

    def put_state(self, name, data):
        self.state[name] = dict(data or {})

    def get_state(self, name):
        return self.state.get(name)

    def upload_artifact(self, key, data, *, kind="", content_type=""):
        import hashlib
        self.uploaded.append((key, data, kind))
        self.blobs[key] = (data, kind, hashlib.sha256(data).hexdigest())

    def register_artifact(self, **ref):
        self.registered.append(ref)

    def list_artifacts(self):
        return [{"key": k, "kind": kind, "sha256": sha}
                for k, (_d, kind, sha) in self.blobs.items()]

    def download_artifact(self, key):
        blob = self.blobs.get(key)
        return blob[0] if blob else None


# ── Item 1: checkpoint + research_state are projected ────────────────────────────

def test_checkpoint_and_research_state_are_projected():
    assert _STATE_DOCS.get("checkpoint.yaml") == "checkpoint"
    assert _STATE_DOCS.get("research_state.yaml") == "research_state"


def test_publish_state_docs_pushes_present_docs(tmp_path):
    (tmp_path / "checkpoint.yaml").write_text(yaml.dump({"iteration": 3}))
    (tmp_path / "paper_state.yaml").write_text(yaml.dump({"reviews": []}))
    cp = FakeCP()
    n = publish_state_docs(cp, tmp_path)
    assert n == 2
    assert cp.state["checkpoint"] == {"iteration": 3}
    assert "paper_state" in cp.state


# ── Item 2: rehydrate a missing local doc from the projection ────────────────────

def test_rehydrate_fills_missing_doc(tmp_path):
    cp = FakeCP()
    cp.state["checkpoint"] = {"iteration": 5, "run_id": "r1"}
    n = rehydrate_state_docs(cp, tmp_path)
    assert n == 1
    written = yaml.safe_load((tmp_path / "checkpoint.yaml").read_text())
    assert written["iteration"] == 5


def test_rehydrate_never_overwrites_present_local(tmp_path):
    (tmp_path / "checkpoint.yaml").write_text(yaml.dump({"iteration": 9}))
    cp = FakeCP()
    cp.state["checkpoint"] = {"iteration": 1}  # older/stale projection
    n = rehydrate_state_docs(cp, tmp_path)
    assert n == 0
    # Present local file is authoritative and untouched.
    assert yaml.safe_load((tmp_path / "checkpoint.yaml").read_text())["iteration"] == 9


def test_rehydrate_skips_absent_projection(tmp_path):
    cp = FakeCP()  # nothing projected
    assert rehydrate_state_docs(cp, tmp_path) == 0
    assert not (tmp_path / "checkpoint.yaml").exists()


# ── Item 3: newest-wins reconciliation (via LocalDb round-trip below too) ────────

def test_resume_pointer_reconciliation_prefers_newer_timestamp(tmp_path, monkeypatch):
    # Build a bare Orchestrator-like object exercising only _resume_checkpoint.
    from ark.orchestrator.core import Orchestrator

    local = {"iteration": 2, "timestamp": "2026-07-07T10:00:00"}
    remote = {"iteration": 4, "timestamp": "2026-07-07T11:00:00"}

    class Stub:
        checkpoint_file = tmp_path / "checkpoint.yaml"
        cp = FakeCP()
        _checkpoint_ts = staticmethod(Orchestrator._checkpoint_ts)

        def load_checkpoint(self):
            return local

        def log(self, *a, **k):
            pass

    stub = Stub()
    stub.cp.state["checkpoint"] = remote
    got = Orchestrator._resume_checkpoint(stub)
    assert got["iteration"] == 4
    # Local cache refreshed to the newer remote.
    assert yaml.safe_load(stub.checkpoint_file.read_text())["iteration"] == 4


def test_resume_pointer_keeps_local_when_newer(tmp_path):
    from ark.orchestrator.core import Orchestrator

    local = {"iteration": 6, "timestamp": "2026-07-07T12:00:00"}
    remote = {"iteration": 3, "timestamp": "2026-07-07T09:00:00"}

    class Stub:
        checkpoint_file = tmp_path / "checkpoint.yaml"
        cp = FakeCP()
        _checkpoint_ts = staticmethod(Orchestrator._checkpoint_ts)

        def load_checkpoint(self):
            return local

        def log(self, *a, **k):
            pass

    stub = Stub()
    stub.cp.state["checkpoint"] = remote
    got = Orchestrator._resume_checkpoint(stub)
    assert got["iteration"] == 6


def test_resume_pointer_from_cp_when_no_local(tmp_path):
    from ark.orchestrator.core import Orchestrator

    remote = {"iteration": 7, "timestamp": "2026-07-07T11:00:00"}

    class Stub:
        checkpoint_file = tmp_path / "checkpoint.yaml"
        cp = FakeCP()

        def load_checkpoint(self):
            return {}

        def log(self, *a, **k):
            pass

    stub = Stub()
    stub.cp.state["checkpoint"] = remote
    got = Orchestrator._resume_checkpoint(stub)
    assert got["iteration"] == 7


# ── Item 4: publish experiment results off the VM ────────────────────────────────

def test_publish_result_artifacts_ships_known_formats(tmp_path):
    from ark.artifacts.local import LocalArtifactStore
    from ark.artifacts import publish_result_artifacts

    results = tmp_path / "results"
    (results / "sub").mkdir(parents=True)
    (results / "metrics.json").write_text('{"acc": 0.9}')
    (results / "sub" / "table.csv").write_text("a,b\n1,2\n")
    (results / "model.bin").write_bytes(b"\x00\x01")  # unknown format → skipped
    (results / "empty.json").write_text("")            # empty → skipped

    cp = FakeCP()
    store = LocalArtifactStore(tmp_path)
    n = publish_result_artifacts(store, cp, tmp_path)

    assert n == 2
    keys = {k for (k, _d, _kind) in cp.uploaded}
    assert keys == {"results/metrics.json", "results/sub/table.csv"}
    assert all(kind == "result" for (_k, _d, kind) in cp.uploaded)


def test_publish_result_artifacts_skips_oversize(tmp_path, monkeypatch):
    from ark.artifacts import publish as pub
    from ark.artifacts.local import LocalArtifactStore
    from ark.artifacts import publish_result_artifacts

    monkeypatch.setattr(pub, "_RESULT_MAX_BYTES", 8)
    results = tmp_path / "results"
    results.mkdir()
    (results / "big.json").write_text("x" * 100)
    (results / "small.json").write_text("{}")

    cp = FakeCP()
    store = LocalArtifactStore(tmp_path)
    n = publish_result_artifacts(store, cp, tmp_path)
    assert n == 1
    assert {k for (k, _d, _k) in cp.uploaded} == {"results/small.json"}


def test_publish_result_artifacts_no_results_dir(tmp_path):
    from ark.artifacts.local import LocalArtifactStore
    from ark.artifacts import publish_result_artifacts

    cp = FakeCP()
    store = LocalArtifactStore(tmp_path)
    assert publish_result_artifacts(store, cp, tmp_path) == 0


# ── (a) rehydrate result files onto a replacement VM ─────────────────────────────

def test_rehydrate_result_artifacts_refills_missing(tmp_path):
    from ark.artifacts import rehydrate_result_artifacts

    cp = FakeCP()
    cp.upload_artifact("results/metrics.json", b'{"acc": 0.9}', kind="result")
    cp.upload_artifact("results/sub/table.csv", b"a,b\n1,2\n", kind="result")
    # A non-result artifact must not be rehydrated as a result.
    cp.upload_artifact("paper/main.pdf", b"%PDF", kind="pdf")

    n = rehydrate_result_artifacts(cp, tmp_path)
    assert n == 2
    assert (tmp_path / "results" / "metrics.json").read_bytes() == b'{"acc": 0.9}'
    assert (tmp_path / "results" / "sub" / "table.csv").exists()
    assert not (tmp_path / "paper" / "main.pdf").exists()


def test_rehydrate_result_artifacts_never_overwrites_present(tmp_path):
    from ark.artifacts import rehydrate_result_artifacts

    local = tmp_path / "results" / "metrics.json"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"LOCAL-WINS")
    cp = FakeCP()
    cp.upload_artifact("results/metrics.json", b"REMOTE", kind="result")

    n = rehydrate_result_artifacts(cp, tmp_path)
    assert n == 0
    assert local.read_bytes() == b"LOCAL-WINS"


def test_rehydrate_result_artifacts_checksum_mismatch_skipped(tmp_path):
    from ark.artifacts import rehydrate_result_artifacts

    cp = FakeCP()
    cp.upload_artifact("results/m.json", b"good", kind="result")
    # Corrupt the recorded digest so the downloaded bytes fail verification.
    data, kind, _sha = cp.blobs["results/m.json"]
    cp.blobs["results/m.json"] = (data, kind, "deadbeef")

    n = rehydrate_result_artifacts(cp, tmp_path)
    assert n == 0
    assert not (tmp_path / "results" / "m.json").exists()


def test_rehydrate_result_artifacts_rejects_key_escape(tmp_path):
    from ark.artifacts import rehydrate_result_artifacts

    cp = FakeCP()
    cp.blobs["../evil.json"] = (b"x", "result", "")
    n = rehydrate_result_artifacts(cp, tmp_path)
    assert n == 0
    assert not (tmp_path.parent / "evil.json").exists()


def test_publish_then_rehydrate_round_trips_via_local_store(tmp_path):
    """publish_result_artifacts → FakeCP catalog → rehydrate onto a fresh dir."""
    from ark.artifacts.local import LocalArtifactStore
    from ark.artifacts import publish_result_artifacts, rehydrate_result_artifacts

    src = tmp_path / "vm1"
    (src / "results").mkdir(parents=True)
    (src / "results" / "out.json").write_text('{"x": 1}')
    cp = FakeCP()
    assert publish_result_artifacts(LocalArtifactStore(src), cp, src) == 1

    dst = tmp_path / "vm2"  # fresh VM, empty disk
    dst.mkdir()
    assert rehydrate_result_artifacts(cp, dst) == 1
    assert (dst / "results" / "out.json").read_text() == '{"x": 1}'


# ── get_state round-trips through the real LocalDb client + db.py helpers ─────────

def test_localdb_get_state_round_trip(tmp_path, monkeypatch):
    import website.dashboard.db as db
    from ark.controlplane import LocalDbControlPlaneClient

    monkeypatch.setattr(db, "_engine", None, raising=False)
    db_path = str(tmp_path / "webapp.db")
    with db.get_session(db_path) as s:
        user, _ = db.get_or_create_user_by_email(s, "tester@example.com")
        project = db.create_project(s, user_id=user.id, name="p")
        pid = project.id

    cp = LocalDbControlPlaneClient(db_path, pid)
    assert cp.get_state("checkpoint") is None  # absent → None
    cp.put_state("checkpoint", {"iteration": 4, "run_id": "r1"})
    got = cp.get_state("checkpoint")
    assert got == {"iteration": 4, "run_id": "r1"}
