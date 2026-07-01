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

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path as PathParam

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
            "experiment_compute_backend": p.experiment_compute_backend,
            "cloud_overrides": p.cloud_overrides,
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


# TRANSITIONAL (D1): removed once HITL fan-out is fully owned by the control
# plane. Until then the orchestrator's own Telegram path records answers here.
@router.post("/projects/{project_id}/decisions/{decision_id}/answer")
def answer_decision(decision_id: str, project_id: str = Depends(require_project),
                    body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    with db.get_session(_db_path()) as s:
        dec = db.get_decision(s, decision_id)
        if dec is None or dec.project_id != project_id:
            raise HTTPException(status_code=404, detail="decision not found")
        db.answer_decision(s, decision_id,
                           index=int(body.get("index", -1)),
                           text=body.get("text", ""),
                           by=body.get("by", ""), source=body.get("source", ""))
    return {"ok": True}


@router.post("/projects/{project_id}/decisions/{decision_id}/expire")
def expire_decision(decision_id: str, project_id: str = Depends(require_project)) -> dict:
    with db.get_session(_db_path()) as s:
        dec = db.get_decision(s, decision_id)
        if dec is None or dec.project_id != project_id:
            raise HTTPException(status_code=404, detail="decision not found")
        db.expire_decision(s, decision_id)
    return {"ok": True}


# ── Live output (accepted now; storage/rendering wired in later steps) ────────────

@router.post("/projects/{project_id}/events")
def append_events(project_id: str = Depends(require_project),
                  body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    # Accept and drop for now — event storage + dashboard rendering land in a
    # follow-up step of Phase 1 (removes the shared-FS log read).
    return {"ok": True, "accepted": len(body.get("lines") or [])}


@router.post("/projects/{project_id}/artifacts")
def register_artifact(project_id: str = Depends(require_project),
                      body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    # Phase 3 wires real object storage; accept the reference for now.
    return {"ok": True}
