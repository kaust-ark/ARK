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


def publish_result_artifacts(store, cp, code_dir, *, results_dir="results",
                             log=None) -> int:
    """Publish experiment result files under ``results/`` to the control plane.

    Called each iteration after experiments run, so results are durable off the
    run's VM (they otherwise live only on that disk until an end-of-run rsync
    pull — lost if the VM dies mid-run) and can rehydrate onto a replacement or
    land in the export ZIP. Only known text/data formats under ``_RESULT_MAX_BYTES``
    are shipped; anything else is left on disk and logged. Keys are stored
    relative to the project root so a local store maps straight back. Returns the
    number of files published.
    """
    code_dir = Path(code_dir)
    root = code_dir / results_dir
    if not root.is_dir():
        return 0
    n = 0
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
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
            if log:
                log(f"result artifact skipped (>{_RESULT_MAX_BYTES // (1024*1024)}MB, "
                    f"stays on VM only): {f.relative_to(code_dir)}", "WARN")
            continue
        key = str(f.relative_to(code_dir))
        n += _publish_one(store, cp, path=f, key=key, kind="result",
                          content_type=ctype, log=log)
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
