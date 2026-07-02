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

from pathlib import Path

# key (relative to the project dir) → content type for common figure formats
_FIGURE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
}


def _publish_one(store, cp, *, path: Path, key: str, kind: str,
                 content_type: str, log=None) -> bool:
    try:
        ref = store.put_path(path, key, content_type=content_type)
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
