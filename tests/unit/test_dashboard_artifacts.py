"""Dashboard-side artifact/state resolution helpers (Phase 3, ADR-0012/0013).

Covers the store-resolution response helper and the state readers' fallback —
including the regression where _read_phase_status referenced a deleted variable.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from starlette.responses import FileResponse, StreamingResponse  # noqa: E402

from website.dashboard import routes  # noqa: E402


def _project(**kw):
    base = dict(id="p1", phase="", status="running", iteration=0, dev_iteration=0,
                max_iterations=8, max_dev_iterations=4, score=None, score_history=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── _read_phase_status: fallback path must not blow up (NameError regression) ──

def test_read_phase_status_fallback_no_crash(tmp_path, monkeypatch):
    # Empty project.phase forces the fallback branch that previously referenced
    # an undefined `state_dir` and raised NameError → HTTP 500.
    monkeypatch.setattr(routes, "_projected_state", lambda project, name: {})
    out = routes._read_phase_status(tmp_path, _project(phase=""))
    assert out["phase"] == "initializing"          # status == running, nothing else
    assert out["max_review_iter"] == 8


def test_read_phase_status_fallback_uses_projection(tmp_path, monkeypatch):
    docs = {"paper_state": {"reviews": [{"iteration": 3}], "status": "accepted"}}
    monkeypatch.setattr(routes, "_projected_state",
                        lambda project, name: docs.get(name, {}))
    out = routes._read_phase_status(tmp_path, _project(phase=""))
    assert out["phase"] == "accepted"
    assert out["review_iter"] == 3


# ── _serve_registered_artifact: size guard, local FileResponse, streaming ──────

def _pdf(tmp_path, size):
    p = tmp_path / "paper" / "main.pdf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_serve_none_ref_returns_none(tmp_path):
    assert routes._serve_registered_artifact(tmp_path, None, filename="main.pdf",
                                             inline=True) is None


def test_serve_small_pdf_skipped_by_min_size(tmp_path):
    _pdf(tmp_path, 500)
    ref = {"store_type": "local", "key": "paper/main.pdf",
           "content_type": "application/pdf", "size": 500}
    # Below the 10KB threshold → None so the caller 404s "not ready" (parity
    # with _find_pdf) rather than serving a broken stub.
    assert routes._serve_registered_artifact(tmp_path, ref, filename="main.pdf",
                                             inline=True, min_size=10000) is None


def test_serve_local_pdf_uses_fileresponse(tmp_path):
    _pdf(tmp_path, 20000)
    ref = {"store_type": "local", "key": "paper/main.pdf",
           "content_type": "application/pdf", "size": 20000}
    resp = routes._serve_registered_artifact(tmp_path, ref, filename="main.pdf",
                                             inline=True, min_size=10000)
    # Local file → FileResponse (Range support + fd cleanup), not a raw stream.
    assert isinstance(resp, FileResponse)
    assert resp.media_type == "application/pdf"


def test_serve_streams_when_no_local_path(tmp_path, monkeypatch):
    _pdf(tmp_path, 20000)
    ref = {"store_type": "local", "key": "paper/main.pdf",
           "content_type": "application/pdf", "size": 20000}
    # Simulate an object store: no local path, closable handle.
    import io
    closed = {"v": False}

    class _FH(io.BytesIO):
        def close(self):
            closed["v"] = True
            super().close()

    class _Store:
        def url(self, ref, **k): return None
        def fspath(self, ref): return None
        def open(self, ref): return _FH(b"y" * 20000)

    monkeypatch.setattr(routes, "_artifact_store_for", lambda pdir: _Store())
    resp = routes._serve_registered_artifact(tmp_path, ref, filename="main.pdf",
                                             inline=True, min_size=10000)
    assert isinstance(resp, StreamingResponse)
    # Draining the body iterator must close the handle (no fd leak).
    import asyncio

    async def _drain():
        async for _ in resp.body_iterator:
            pass

    asyncio.run(_drain())
    assert closed["v"] is True


def test_research_cursor_and_evidence_confinement(tmp_path):
    from website.dashboard import progress

    state = tmp_path / "auto_research/state"
    state.mkdir(parents=True)
    journal = tmp_path / progress.JOURNAL
    journal.write_bytes(b'{"id":"one","observation":"1"}\n{"id":"two"')
    cursor, initial = progress.journal(tmp_path)
    assert [e["id"] for e in initial] == ["one"]
    with journal.open("ab") as f:
        f.write(b',"observation":"2"}\n')
    end, live = progress.journal(tmp_path, after=cursor)
    assert [e["id"] for e in live] == ["two"]
    assert progress.journal(tmp_path, after=end) == (end, [])
    assert progress.journal(tmp_path, before=live[0]["offset"])[1] == initial

    for key in ("results/../config.yaml", "results/secrets.json", "paper/figures/active.svg",
                "/etc/passwd", "auto_research/state/native_project.json", "results/.env"):
        assert progress.evidence_type(key) is None
    results = tmp_path / "results"
    results.mkdir()
    (results / "link.txt").symlink_to(journal)
    (results / "dir").symlink_to(state, target_is_directory=True)
    assert not progress.file_exists(tmp_path, "results/link.txt")
    assert not progress.file_exists(tmp_path, "results/dir/research_events.jsonl")


def test_review_freshness_tracks_private_split_sources_by_content(tmp_path, monkeypatch):
    import hashlib
    from website.dashboard import progress

    chapter = "paper/sections/results.tex"
    hashes = {}
    for key, content in {"paper/main.pdf": b"built", "paper/main.tex": b"manuscript", chapter: b"before"}.items():
        path = tmp_path / key
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(content)
        hashes[key] = hashlib.sha256(content).hexdigest()
    review = {"revision": "f" * 64, "paper_pdf_sha256": hashes["paper/main.pdf"], "artifact_hashes": hashes}
    assert progress.evidence_type(chapter) is None
    assert not progress.review_summary(tmp_path, review)["stale"]
    original = progress.hash_stream
    reads = []

    def count(stream):
        reads.append(stream.fileno())
        return original(stream)

    monkeypatch.setattr(progress, "hash_stream", count)
    newest = max((tmp_path / key).stat().st_ctime_ns for key in hashes)
    monkeypatch.setattr(progress, "time_ns", lambda: newest + 3_000_000_000)
    assert not progress.review_summary(tmp_path, review)["stale"]
    reads.clear()
    assert not progress.review_summary(tmp_path, review)["stale"] and not reads
    monkeypatch.setattr(progress, "time_ns", lambda: newest)
    (tmp_path / chapter).write_bytes(b"before")
    assert not progress.review_summary(tmp_path, review)["stale"]
    assert reads
    (tmp_path / chapter).write_bytes(b"after!")
    assert progress.review_summary(tmp_path, review)["stale"]
    (tmp_path / chapter).unlink()
    assert progress.review_summary(tmp_path, review)["stale"]


def test_delivered_paper_requires_the_recorded_acceptance_and_pdf(tmp_path):
    import hashlib
    import json
    from website.dashboard import progress

    state = tmp_path / "auto_research/state"
    revision = "a" * 64
    archive = state / "reviews" / revision
    archive.mkdir(parents=True)
    pdf = archive / "main.pdf"
    pdf.write_bytes(b"rendered paper")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (state / "native_project.json").write_text(json.dumps({"state":"running", "delivery_revision":revision}))
    record = {"revision":revision, "verdict":"accept", "deliverable":True, "paper_pdf_sha256":digest,
              "artifact_hashes":{"paper/main.pdf":digest}}
    verdict = archive / "review.json"
    for changed in ({"revision":"b" * 64}, {"verdict":"revise"}, {"deliverable":False},
                    {"paper_pdf_sha256":"c" * 64}, {"artifact_hashes":{"paper/main.pdf":"d" * 64}}):
        verdict.write_text(json.dumps({**record, **changed}))
        assert progress.paper_edition(tmp_path) == ("unavailable", None)
    verdict.write_text(json.dumps(record))
    assert progress.paper_edition(tmp_path) == ("delivered", record)
    pdf.rename(archive / "other.pdf")
    pdf.symlink_to(archive / "other.pdf")
    assert progress.paper_edition(tmp_path) == ("unavailable", None)


def test_comparison_projection_preserves_signed_row_identifiers_or_omits_the_pair():
    from website.dashboard.progress import _claim_comparison

    side = {"subject": "Method A", "column": "error"}
    for value in [0, 0.5, "A", 2**53 + 1]:
        condition = {"column": "arm", "value": value}
        pair = [side, {**side, "subject": "Method B", "condition": condition}]
        display = _claim_comparison(pair)
        assert display[0] == side and display[1]["condition"]["value"] == value
        if value == 2**53 + 1:
            assert display[1]["condition"]["literal"] == "9007199254740993"
    for malformed in [None, [side], [side, {}], [side, side, side],
                      [side, {**side, "condition": {"column": "arm"}}],
                      [side, {**side, "condition": {"column": "arm", "value": False}}]]:
        assert _claim_comparison(malformed) == []
