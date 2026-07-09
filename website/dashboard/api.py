"""The /v1 control-plane HTTP API — the network boundary for remote orchestrators.

Every endpoint is a thin wrapper over the same ``website.dashboard.db`` helpers
the in-process LocalDb client calls, so the two transports stay behavior-identical
(see CONTROL_PLANE_BOUNDARY.md). Access is gated by a per-project bearer token
(``auth.make_job_token`` / ``verify_job_token``): the token is scoped to exactly
one project and every route asserts it matches the path's project_id.

Mounted on the OUTER app at /v1 (no session cookie, no browser auth) by
``create_app``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import (APIRouter, Body, Depends, Header, HTTPException,
                     Path as PathParam, Request)
from fastapi.responses import Response

from . import db
from .auth import verify_job_token
from .config import get_settings

router = APIRouter(prefix="/v1", tags=["control-plane"])

# Fields a run may report about ITSELF. Whitelisted so a (possibly leaked) job
# token can never rewrite identity/ownership columns (id, user_id, name, …).
# activity / control_state / autonomy_level have their own endpoints.
_REPORTABLE_FIELDS = frozenset({
    "status", "pid", "phase", "iteration", "dev_iteration", "dev_status",
    "score", "score_history", "language", "checkpoint_data",
    "total_cost_usd", "total_input_tokens", "total_output_tokens",
    "total_agent_calls", "pdf_path", "has_pdf_upload", "error_message",
})


def _db_path() -> str:
    return get_settings().db_path


def require_project(project_id: str = PathParam(...),
                    authorization: Optional[str] = Header(None)) -> str:
    """Auth dependency: valid bearer token scoped to THIS project_id."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    tok_pid = verify_job_token(token, get_settings().secret_key)
    if not tok_pid:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    if tok_pid != project_id:
        raise HTTPException(status_code=403, detail="token not scoped to this project")
    return project_id


# ── Bootstrap / reads ─────────────────────────────────────────────────────────

@router.get("/projects/{project_id}")
def get_project_view(project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        p = db.get_project(s, project_id)
        if not p:
            raise HTTPException(status_code=404, detail="project not found")
        return {
            "id": p.id,
            "name": p.name,
            "autonomy_level": p.autonomy_level,
            "status": p.status,
            "phase": p.phase,
            "model": p.model,
            "model_variant": p.model_variant,
            "language": p.language,
            "paper_accept_threshold": p.paper_accept_threshold,
            "max_iterations": p.max_iterations,
            "max_dev_iterations": p.max_dev_iterations,
            "max_days": p.max_days,
            "figure_generation": p.figure_generation,
            "orchestrator_compute_backend": p.orchestrator_compute_backend,
            "orchestrator_instance_type": getattr(p, "orchestrator_instance_type", "") or "",
            "experiment_compute_backend": p.experiment_compute_backend,
            "iteration": p.iteration,
            "checkpoint_data": p.checkpoint_data,
        }


@router.get("/projects/{project_id}/autonomy")
def get_autonomy(project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        p = db.get_project(s, project_id)
        return {"level": (p.autonomy_level if p else None) or None}


# ── Status / progress (writes) ──────────────────────────────────────────────────

@router.post("/projects/{project_id}/status")
def report_status(project_id: str = Depends(require_project),
                  fields: dict[str, Any] = Body(default_factory=dict)) -> dict:
    allowed = {k: v for k, v in (fields or {}).items() if k in _REPORTABLE_FIELDS}
    if allowed:
        with db.get_session(_db_path()) as s:
            p = db.get_project(s, project_id)
            if p:
                db.update_project(s, p, **allowed)
    return {"ok": True, "applied": sorted(allowed)}


@router.post("/projects/{project_id}/activity")
def set_activity(project_id: str = Depends(require_project),
                 text: str = Body("", embed=True)) -> dict:
    with db.get_session(_db_path()) as s:
        db.set_activity(s, project_id, text)
    return {"ok": True}


@router.post("/projects/{project_id}/control-state")
def set_control_state(project_id: str = Depends(require_project),
                      state: str = Body("", embed=True)) -> dict:
    with db.get_session(_db_path()) as s:
        db.set_control_state(s, project_id, state)
    return {"ok": True}


@router.post("/projects/{project_id}/autonomy")
def set_autonomy(project_id: str = Depends(require_project),
                 level: str = Body("", embed=True)) -> dict:
    with db.get_session(_db_path()) as s:
        db.set_autonomy(s, project_id, level)
    return {"ok": True}


# ── Commands (peek + ack, D2) ────────────────────────────────────────────────────

@router.get("/projects/{project_id}/commands")
def list_commands(project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        return {"commands": db.list_pending_commands(s, project_id)}


@router.post("/projects/{project_id}/commands/{command_id}/ack")
def ack_command(command_id: str, project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        db.mark_command_consumed(s, command_id)
    return {"ok": True}


# ── Conversation thread (write) ──────────────────────────────────────────────────

@router.post("/projects/{project_id}/messages")
def append_message(project_id: str = Depends(require_project),
                   body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    with db.get_session(_db_path()) as s:
        db.add_message(s, project_id,
                       body.get("role", "agent"), body.get("text", ""),
                       kind=body.get("kind", "message"), meta=body.get("meta"))
    return {"ok": True}


# ── Decisions ─────────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/decisions")
def open_decision(project_id: str = Depends(require_project),
                  body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    deadline = None
    raw_deadline = body.get("deadline_at")
    if raw_deadline:
        try:
            deadline = datetime.fromisoformat(raw_deadline)
        except (ValueError, TypeError):
            deadline = None
    with db.get_session(_db_path()) as s:
        decision_id = db.create_pending_decision(
            s, project_id,
            body.get("question", "Decision needed"),
            list(body.get("options") or []),
            kind=body.get("kind", "decision"),
            context=body.get("context", ""),
            default_index=int(body.get("default_index", 0)),
            timeout_action=body.get("timeout_action", "proceed_default"),
            deadline_at=deadline,
        )
    return {"decision_id": decision_id}


@router.get("/projects/{project_id}/decisions/{decision_id}")
def get_decision(decision_id: str, project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        dec = db.get_decision(s, decision_id)
        if dec is None or dec.project_id != project_id:
            raise HTTPException(status_code=404, detail="decision not found")
        return {
            "id": dec.id,
            "status": dec.status,
            "answer_index": dec.answer_index if dec.answer_index is not None else -1,
            "answer_text": dec.answer_text or "",
            "source": dec.source or "",
        }


# Note: answering + expiring decisions are owned by the control-plane HITL engine
# (website.dashboard.hitl) and the webapp routes — not exposed to the orchestrator
# (D1). The orchestrator only opens (above) and polls (GET) decisions.


# ── Live output (accepted now; storage/rendering wired in later steps) ────────────

@router.post("/projects/{project_id}/events")
def append_events(project_id: str = Depends(require_project),
                  body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    with db.get_session(_db_path()) as s:
        stored = db.append_events(s, project_id, body.get("lines") or [])
    return {"ok": True, "stored": stored}


@router.post("/projects/{project_id}/artifacts")
def register_artifact(project_id: str = Depends(require_project),
                      body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    """Register a stored artifact reference (Phase 3, ADR-0012). The orchestrator
    uploads the bytes to the artifact store, then posts the resulting
    ``ArtifactRef`` (+ ``kind``) here so the dashboard can resolve it."""
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="artifact 'key' is required")
    with db.get_session(_db_path()) as s:
        row = db.register_artifact(
            s, project_id,
            kind=body.get("kind", ""),
            key=key,
            store_type=body.get("store_type", "local"),
            content_type=body.get("content_type", ""),
            size=int(body.get("size", 0) or 0),
            sha256=body.get("sha256", ""),
        )
        return {"ok": True, "id": row.id}


# Reject an absurd upload before buffering it (a paper PDF is well under this).
_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


def _project_store(settings, owner_id: str, project_id: str, *, create: bool = False):
    """Build the artifact store the dashboard reads through for a project
    (config.yaml-selected, defaulting to a local store rooted at the project
    dir), mirroring ``routes._artifact_store_for``. Returns ``(store, pdir)``."""
    from pathlib import Path
    import yaml
    pdir = Path(settings.projects_root) / owner_id / project_id
    if create:
        pdir.mkdir(parents=True, exist_ok=True)
    try:
        from ark.artifacts import from_config as _afc
        cfg = {}
        cfg_file = pdir / "config.yaml"
        if cfg_file.exists():
            cfg = yaml.safe_load(cfg_file.read_text()) or {}
        return _afc(cfg, pdir), pdir
    except Exception:
        from ark.artifacts import LocalArtifactStore
        return LocalArtifactStore(pdir), pdir


@router.post("/projects/{project_id}/artifacts/upload")
async def upload_artifact(request: Request,
                          project_id: str = Depends(require_project),
                          key: str = "", kind: str = "",
                          content_type: str = "") -> dict:
    """Receive artifact BYTES from a remote orchestrator and persist them into
    the control plane's OWN artifact store, then register the reference.

    This is the ``local``-store transport for runs with no shared FS or object
    store (the common case): the orchestrator's produced file lives only on its
    VM, so it POSTs the raw bytes here. We write them where the dashboard serves
    from (``projects_root/<owner>/<project>/<key>``, mirroring
    ``routes._artifact_store_for``) so ``GET /pdf`` resolves the registered
    reference. Object-store runs never hit this path — their bytes are already in
    the shared bucket, so they use ``register_artifact`` instead."""
    key = (key or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="artifact 'key' is required")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=422, detail="empty artifact body")
    if len(data) > _MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="artifact too large")

    settings = get_settings()
    with db.get_session(settings.db_path) as s:
        p = db.get_project(s, project_id)
        if not p:
            raise HTTPException(status_code=404, detail="project not found")
        owner_id = p.user_id

    import io
    store, _pdir = _project_store(settings, owner_id, project_id, create=True)
    ref = store.put(key, io.BytesIO(data), content_type=content_type or "")
    with db.get_session(settings.db_path) as s:
        row = db.register_artifact(
            s, project_id, kind=kind or "", key=key,
            store_type=ref.store_type, content_type=content_type or "",
            size=ref.size, sha256=ref.sha256,
        )
        if kind == "pdf":
            p = db.get_project(s, project_id)
            if p:
                db.update_project(s, p, pdf_path=key)
    return {"ok": True, "id": row.id, "size": ref.size, "sha256": ref.sha256}


@router.get("/projects/{project_id}/artifacts")
def list_artifacts(project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        return {"artifacts": db.list_artifacts(s, project_id)}


@router.get("/projects/{project_id}/artifacts/download")
def download_artifact(project_id: str = Depends(require_project),
                      key: str = "") -> Response:
    """Return stored artifact BYTES by key, so a replacement VM can rehydrate
    result files its (now-dead) predecessor produced (ADR-0012). The mirror of
    the upload path: resolve the registered reference and stream it back from
    the same store the dashboard serves through."""
    key = (key or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="artifact 'key' is required")
    settings = get_settings()
    with db.get_session(settings.db_path) as s:
        row = db.get_artifact(s, project_id, key)
        if not row:
            raise HTTPException(status_code=404, detail="artifact not found")
        p = db.get_project(s, project_id)
        if not p:
            raise HTTPException(status_code=404, detail="project not found")
        owner_id = p.user_id
        content_type = row.content_type or "application/octet-stream"
        store_type = row.store_type or "local"

    from ark.artifacts import ArtifactRef
    store, _pdir = _project_store(settings, owner_id, project_id)
    ref = ArtifactRef(store_type=store_type, key=key, content_type=content_type)
    try:
        with store.open(ref) as fh:
            data = fh.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="artifact bytes missing")
    except Exception:
        raise HTTPException(status_code=500, detail="artifact read failed")
    return Response(content=data, media_type=content_type)


# ── State projection (Phase 3, ADR-0013) ─────────────────────────────────────────

@router.put("/projects/{project_id}/state/{name}")
def put_state(name: str, project_id: str = Depends(require_project),
              body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    """Project a state document (paper_state, action_plan, …) to the control
    plane. Body is ``{"data": {...}}`` (or the document itself)."""
    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    if not isinstance(data, dict):
        data = {}
    with db.get_session(_db_path()) as s:
        db.put_state_doc(s, project_id, name, data)
    return {"ok": True}


@router.get("/projects/{project_id}/state/{name}")
def get_state(name: str, project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        doc = db.get_state_doc(s, project_id, name)
    if doc is None:
        raise HTTPException(status_code=404, detail="state doc not found")
    return {"name": name, "data": doc}


@router.get("/projects/{project_id}/state")
def list_state(project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        return {"state": db.list_state_docs(s, project_id)}
