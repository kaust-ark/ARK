"""Publish a project's produced artifacts through the store + control plane.

The orchestrator calls :func:`publish_paper_artifacts` after an iteration
compiles the paper: each blob is ``put`` into the store (a no-op copy for local
storage, an upload for object storage) and its reference registered with the
control plane. On the local/shared-FS transport registration is what lets the
dashboard resolve the PDF through the store instead of scanning disk; on the
object-store/remote transport it is the *only* way the dashboard learns the blob
exists. Every step is best-effort — a publish failure must never break a run.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

# key (relative to the project dir) → content type for common figure formats
_FIGURE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
}

# Experiment result formats worth shipping to the control plane so they survive
# the run's VM and can rehydrate / land in the export ZIP. Extension → content
# type; anything not listed (binaries, checkpoints, huge dumps) is left on disk.
_RESULT_TYPES = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".log": "text/plain",
}

# Per-file cap for result publishing: results ride the same JSON/bytes /v1 path
# as everything else, so a runaway dump must not stall the run. Skips are logged
# (never silently dropped) so it's clear the blob stayed on the VM only.
_RESULT_MAX_BYTES = 25 * 1024 * 1024

# Whole-walk budgets. Results are published synchronously, per iteration and
# again at run end, and one project's results/ held 650,215 files (46 GB): the
# end-of-run publish registered 78,719 of them over three silent hours, until
# the stuck-run watchdog killed the finished run and filed its paper as failed
# (a247437e, 2026-09-17). The export ZIP walked the same tree, in memory, on
# the event loop: 6.5 minutes per download, the dashboard frozen for everyone
# meanwhile, and Cloudflare's 100 s timeout showing the owner an error every
# time. Files are walked in sorted order, so what lands is a stable sample of
# the results; the rest stays in the project dir on disk, where it was all
# along. Progress is logged so the run is visibly alive.
_RESULT_MAX_FILES = 5000
_RESULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_RESULT_TIME_BUDGET_S = 600.0
_RESULT_PROGRESS_EVERY_S = 60.0


def _publish_one(store, cp, *, path: Path, key: str, kind: str,
                 content_type: str, log=None) -> bool:
    try:
        ref = store.put_path(path, key, content_type=content_type)
        # A `local` store keeps the bytes only where the run executes (the VM for
        # a remote run), so a bare reference is unresolvable by the control plane
        # — push the bytes to it instead. Object stores (s3/gcs/azure) are shared,
        # so registering the reference is enough (and avoids re-uploading blobs
        # already in the bucket). See ControlPlaneClient.upload_artifact.
        if ref.store_type == "local":
            cp.upload_artifact(key=key, data=path.read_bytes(),
                               kind=kind, content_type=content_type)
        else:
            cp.register_artifact(kind=kind, **ref.to_dict())
        return True
    except Exception as e:  # noqa: BLE001 — publishing is best-effort
        if log:
            log(f"artifact publish failed for {key}: {e}", "WARN")
        return False


def publish_paper_artifacts(store, cp, code_dir, *, latex_dir="paper",
                            figures_dir="paper/figures", log=None) -> int:
    """Publish the compiled PDF, an uploaded PDF, and figures if present.

    ``code_dir`` is the project root; keys are stored relative to it so a local
    store maps them straight back onto the existing on-disk layout. Returns the
    number of artifacts published.
    """
    code_dir = Path(code_dir)
    n = 0

    pdf = code_dir / latex_dir / "main.pdf"
    if pdf.exists() and pdf.stat().st_size > 0:
        n += _publish_one(store, cp, path=pdf, key=f"{latex_dir}/main.pdf",
                          kind="pdf", content_type="application/pdf", log=log)

    uploaded = code_dir / "uploaded.pdf"
    if uploaded.exists() and uploaded.stat().st_size > 0:
        n += _publish_one(store, cp, path=uploaded, key="uploaded.pdf",
                          kind="uploaded_pdf", content_type="application/pdf", log=log)

    fig_root = code_dir / figures_dir
    if fig_root.is_dir():
        for fig in sorted(fig_root.rglob("*")):
            if not fig.is_file():
                continue
            ctype = _FIGURE_TYPES.get(fig.suffix.lower())
            if not ctype:
                continue
            key = str(fig.relative_to(code_dir))
            n += _publish_one(store, cp, path=fig, key=key, kind="figure",
                              content_type=ctype, log=log)

    return n


def _walk_files(root: Path):
    """Files under ``root`` in sorted order, streamed: ``os.walk`` reads
    directory entries without a stat per file and never holds the whole tree."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


class ResultWalk:
    """The result files worth keeping under ``root``, in sorted order, bounded.

    Iterating yields ``(path, size, content_type)`` for each file of a known
    result format (``_RESULT_TYPES``) under ``_RESULT_MAX_BYTES``, and stops at
    the first budget spent: ``max_files`` files, ``max_total_bytes`` bytes, or
    ``time_budget_s`` seconds of walking. Afterwards ``stopped`` names that
    budget (``""`` when the tree was walked to the end), ``taken``/``total_bytes``
    say what was yielded, and ``seen`` how many files the walk looked at. Both
    the control-plane publish and the export ZIP consume it, so both stay
    bounded by one definition of "a result".
    """

    def __init__(self, root, *, max_files=_RESULT_MAX_FILES,
                 max_total_bytes=_RESULT_MAX_TOTAL_BYTES,
                 time_budget_s=_RESULT_TIME_BUDGET_S,
                 progress_every_s=_RESULT_PROGRESS_EVERY_S,
                 log=None, label="results publish"):
        self.root = Path(root)
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.time_budget_s = time_budget_s
        self.progress_every_s = progress_every_s
        self.log = log
        self.label = label
        self.taken = 0
        self.total_bytes = 0
        self.seen = 0
        self.skipped_large = 0
        self.stopped = ""

    def _say(self, msg, level):
        if self.log:
            self.log(msg, level)

    def _spent(self, started, now):
        if self.taken >= self.max_files:
            return "file cap"
        if self.total_bytes >= self.max_total_bytes:
            return "byte cap"
        if now - started >= self.time_budget_s:
            return "time budget"
        return ""

    def __iter__(self):
        if not self.root.is_dir():
            return
        started = last_report = time.monotonic()
        for f in _walk_files(self.root):
            self.seen += 1
            now = time.monotonic()
            if now - last_report >= self.progress_every_s:
                last_report = now
                self._say(f"{self.label}: {self.taken} file(s), "
                          f"{self.total_bytes // (1024 * 1024)} MB so far "
                          f"({self.seen} seen, {int(now - started)}s)", "INFO")
            self.stopped = self._spent(started, now)
            if self.stopped:
                self._say(f"{self.label} stopped at {self.taken} file(s) / "
                          f"{self.total_bytes // (1024 * 1024)} MB after "
                          f"{int(now - started)}s ({self.stopped}); the rest stays "
                          f"in the project's {self.root.name}/ on disk", "WARN")
                return
            ctype = _RESULT_TYPES.get(f.suffix.lower())
            if not ctype:
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            if size > _RESULT_MAX_BYTES:
                self.skipped_large += 1
                self._say(f"result file skipped (>{_RESULT_MAX_BYTES // (1024*1024)}MB, "
                          f"left on disk): {f.relative_to(self.root.parent)}", "WARN")
                continue
            yield f, size, ctype
            self.taken += 1
            self.total_bytes += size


def publish_result_artifacts(store, cp, code_dir, *, results_dir="results",
                             log=None, max_files=_RESULT_MAX_FILES,
                             max_total_bytes=_RESULT_MAX_TOTAL_BYTES,
                             time_budget_s=_RESULT_TIME_BUDGET_S,
                             progress_every_s=_RESULT_PROGRESS_EVERY_S) -> int:
    """Publish experiment result files under ``results/`` to the control plane.

    Called each iteration after experiments run, so results are durable off the
    run's VM (they otherwise live only on that disk until an end-of-run rsync
    pull — lost if the VM dies mid-run) and can rehydrate onto a replacement or
    land in the export ZIP. Only known text/data formats under ``_RESULT_MAX_BYTES``
    are shipped; anything else is left on disk and logged. Keys are stored
    relative to the project root so a local store maps straight back.

    Bounded per call by a :class:`ResultWalk` (``max_files``, ``max_total_bytes``,
    ``time_budget_s``; the cap is logged once), and progress is logged every
    ``progress_every_s`` so a long publish reads as a live run, not a stuck one.
    Returns the number of files published.
    """
    code_dir = Path(code_dir)
    walk = ResultWalk(code_dir / results_dir, max_files=max_files,
                      max_total_bytes=max_total_bytes, time_budget_s=time_budget_s,
                      progress_every_s=progress_every_s, log=log)
    n = 0
    for f, _size, ctype in walk:
        key = str(f.relative_to(code_dir))
        if _publish_one(store, cp, path=f, key=key, kind="result",
                        content_type=ctype, log=log):
            n += 1
    return n


def rehydrate_result_artifacts(cp, code_dir, *, log=None) -> int:
    """Refill *missing* experiment result files from the control plane.

    The read side of :func:`publish_result_artifacts`: when a run's VM dies and
    a replacement is provisioned with an empty disk, this pulls the result files
    the predecessor published back under the project dir so the writer/analysis
    agents (and the resume path) see them again — parallel to state-doc
    rehydration. Only writes a file that is *absent* locally (a present file is
    the authoritative working copy) and only under the project root (a key that
    escapes it is skipped). Bytes are verified against the registered sha256 when
    present. Returns the number of files rehydrated. Best-effort throughout.
    """
    code_dir = Path(code_dir)
    root = code_dir.resolve()
    try:
        arts = cp.list_artifacts() or []
    except Exception as e:  # noqa: BLE001 — rehydration is best-effort
        if log:
            log(f"result rehydrate listing failed: {e}", "WARN")
        return 0

    n = 0
    for a in arts:
        if not isinstance(a, dict) or a.get("kind") != "result":
            continue
        key = (a.get("key") or "").strip()
        if not key:
            continue
        dest = (code_dir / key).resolve()
        if dest != root and root not in dest.parents:
            if log:
                log(f"result rehydrate skipped (key escapes project): {key}", "WARN")
            continue
        if dest.exists():
            continue
        try:
            data = cp.download_artifact(key)
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"result rehydrate download failed for {key}: {e}", "WARN")
            continue
        if not data:
            continue
        want = (a.get("sha256") or "").strip()
        if want and hashlib.sha256(data).hexdigest() != want:
            if log:
                log(f"result rehydrate checksum mismatch, skipped: {key}", "WARN")
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            n += 1
            if log:
                log(f"rehydrated result {key} from control plane", "INFO")
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"result rehydrate write failed for {key}: {e}", "WARN")
    return n
