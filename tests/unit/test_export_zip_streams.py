"""The project export is a streamed, bounded ZIP.

One project's results/ held 650,215 files. Its owner clicked "download ZIP"
eleven times over three days (2026-09-19..21): each attempt walked the whole
tree with rglob+stat over NFS, built the archive in memory, on the event
loop, for 6.5 minutes, while Cloudflare showed the owner an error at 100 s and
every other dashboard user waited behind it. Now the archive is written as it
downloads, one member at a time, paper first, with the results sample bounded
by the same ResultWalk the control plane uses, and a note when it is a sample.
"""
import io
import zipfile
from pathlib import Path

from ark.artifacts.publish import ResultWalk
from website.dashboard import routes as R


def _project(tmp_path, n_results=6) -> Path:
    pdir = tmp_path / "proj"
    (pdir / "paper").mkdir(parents=True)
    (pdir / "paper" / "main.tex").write_text("\\documentclass{article}")
    (pdir / "paper" / "main.pdf").write_bytes(b"%PDF-1.4 " + b"x" * 2000)
    (pdir / "paper" / "main.aux").write_text("build junk")
    (pdir / "results").mkdir()
    for i in range(n_results):
        (pdir / "results" / f"r{i:02d}.json").write_text('{"i": %d}' % i)
    (pdir / "results" / "weights.bin").write_bytes(b"\0" * 10)
    (pdir / "config.yaml").write_text("title: t\n")
    (pdir / "auto_research" / "state").mkdir(parents=True)
    (pdir / "auto_research" / "state" / "findings.yaml").write_text("disk: 1\n")
    return pdir


def _bundle(members):
    chunks = list(R._stream_zip(members))
    zf = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    assert zf.testzip() is None
    return chunks, zf


def test_bundle_is_a_valid_zip_streamed_member_by_member(tmp_path):
    pdir = _project(tmp_path)
    chunks, zf = _bundle(R._export_members(pdir, {}, None))
    names = zf.namelist()
    assert "paper/main.tex" in names and "paper/main.pdf" in names
    assert "paper/main.aux" not in names
    assert "config.yaml" in names
    assert "auto_research/state/findings.yaml" in names
    assert [n for n in names if n.startswith("results/")] == [
        f"results/r{i:02d}.json" for i in range(6)]
    assert "results/EXPORT_NOTE.txt" not in names       # nothing was left out
    # Streamed: at least one chunk per member, and the paper leaves first.
    assert len(chunks) >= len(names)
    assert names.index("paper/main.pdf") < names.index("results/r00.json")


def test_results_sample_is_bounded_and_says_so(tmp_path):
    pdir = _project(tmp_path, n_results=10)
    walk = ResultWalk(pdir / "results", max_files=3)
    _chunks, zf = _bundle(R._export_members(pdir, {}, None, results_walk=walk))
    results = [n for n in zf.namelist() if n.startswith("results/")]
    assert results == ["results/r00.json", "results/r01.json", "results/r02.json",
                       "results/EXPORT_NOTE.txt"]
    note = zf.read("results/EXPORT_NOTE.txt").decode()
    assert "3 result files" in note and "file cap" in note
    assert "kept on the server" in note


def test_state_doc_projection_wins_over_disk(tmp_path):
    pdir = _project(tmp_path)
    _chunks, zf = _bundle(R._export_members(pdir, {"findings": {"db": 2}}, None))
    assert zf.read("auto_research/state/findings.yaml").decode() == "db: 2\n"


def test_a_member_that_vanished_is_skipped_not_fatal(tmp_path):
    pdir = _project(tmp_path, n_results=2)
    members = [("paper/main.tex", pdir / "paper" / "main.tex"),
               ("results/gone.json", pdir / "results" / "gone.json"),
               ("config.yaml", pdir / "config.yaml")]
    _chunks, zf = _bundle(iter(members))
    assert zf.namelist() == ["paper/main.tex", "config.yaml"]


def test_export_walk_budget_fits_inside_the_edge_timeout():
    assert R._EXPORT_RESULTS_TIME_BUDGET_S < 100


def test_endpoint_returns_a_streaming_response_without_building_first(tmp_path, monkeypatch):
    """The handler must hand Starlette a generator, not a finished buffer: the
    body is produced in the threadpool while it downloads."""
    import asyncio
    from types import SimpleNamespace
    pdir = _project(tmp_path)
    monkeypatch.setattr(R, "get_settings", lambda: SimpleNamespace(db_path="x"))

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(R, "get_session", lambda p: _S())
    monkeypatch.setattr(R, "get_project", lambda s, pid: SimpleNamespace(user_id="u1"))
    monkeypatch.setattr(R, "_can_read_project", lambda req, p: True)
    monkeypatch.setattr(R, "list_state_docs", lambda s, pid: {})
    monkeypatch.setattr(R, "_latest_artifact_ref", lambda s, pid, kind: None)
    monkeypatch.setattr(R, "_project_dir", lambda settings, uid, pid: pdir)
    touched = []
    real = R._export_members

    def spy(*a, **k):
        touched.append(1)
        yield from real(*a, **k)
    monkeypatch.setattr(R, "_export_members", spy)

    resp = asyncio.run(R.api_download_zip("p1", request=None))
    assert resp.media_type == "application/zip"
    assert 'filename="p1.zip"' in resp.headers["content-disposition"]
    assert touched == [], "the bundle was built before the response was returned"
