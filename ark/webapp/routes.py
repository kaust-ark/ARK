"""All API + page endpoints for ARK webapp."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import uuid
import zipfile
from pathlib import Path as _Path
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ark.webapp.routes")

MAX_PROJECTS_PER_USER = 10
MAX_ITER_PER_START = 3
_DISABLED_FLAG = _Path.home() / ".ark_webapp_disabled"

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.requests import Request

from .auth import make_token, verify_token
from .config import get_settings
from .db import (
    Project,
    User,
    create_project,
    delete_project,
    get_all_projects,
    get_or_create_user_by_email,
    get_project,
    get_projects_for_user,
    get_session,
    update_project,
)
from .jobs import cancel_job, slurm_available, slurm_state_to_status, submit_job
from .notify import send_completion_email, send_magic_link_email, send_telegram_login_link, send_telegram_notify
from .templates import copy_venue_template, has_venue_template

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"

# ── helpers ──────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    return slug.strip("-")[:48] or "project"


def _get_current_user(request: Request) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    settings = get_settings()
    with get_session(settings.db_path) as session:
        return session.get(User, user_id)


def _require_user(request: Request) -> User:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _is_admin(user: User) -> bool:
    settings = get_settings()
    if not settings.admin_emails:
        return False
    return user.email.lower() in settings.admin_emails


def _require_admin(request: Request) -> User:
    user = _require_user(request)
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _can_access_project(user: User, project: Project) -> bool:
    """Return True if user owns the project or is admin."""
    return project.user_id == user.id or _is_admin(user)


def _project_dir(settings, user_id: str, project_id: str) -> Path:
    return settings.projects_root / user_id / project_id


def _write_config_yaml(project_dir: Path, project: Project):
    """Write config.yaml that ark orchestrator will read."""
    config = {
        "project": project.name,
        "title": project.title or project.name,
        "idea": project.idea,
        "venue": project.venue,
        "venue_format": project.venue_format,
        "venue_pages": project.venue_pages,
        "mode": project.mode,
        "max_iterations": project.max_iterations,
        "language": "en",
        "code_dir": str(project_dir),
        "latex_dir": "paper",
        "figures_dir": "paper/figures",
    }
    if project.telegram_token:
        config["telegram_bot_token"] = project.telegram_token
    if project.telegram_chat_id:
        config["telegram_chat_id"] = project.telegram_chat_id
    uploaded_pdf = project_dir / "uploaded.pdf"
    if uploaded_pdf.exists():
        config["uploaded_pdf"] = str(uploaded_pdf)
    config_path = project_dir / "config.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))


def _read_project_score(project_dir: Path) -> float:
    """Try to read the latest review score from state files."""
    state_dir = project_dir / "auto_research" / "state"
    # paper_state.yaml is the authoritative source
    ps = state_dir / "paper_state.yaml"
    if ps.exists():
        try:
            d = yaml.safe_load(ps.read_text()) or {}
            score = d.get("current_score")
            if score is not None:
                return float(score)
        except Exception:
            pass
    # fallback: legacy files
    for name in ("review.yaml", "findings.yaml"):
        f = state_dir / name
        if f.exists():
            try:
                d = yaml.safe_load(f.read_text()) or {}
                score = d.get("score") or d.get("overall_score") or d.get("review_score")
                if score is not None:
                    return float(score)
            except Exception:
                pass
    return 0.0


def _read_score_history(project_dir: Path) -> list[dict]:
    """Read per-iteration score history from paper_state.yaml."""
    state_file = project_dir / "auto_research" / "state" / "paper_state.yaml"
    if not state_file.exists():
        return []
    try:
        d = yaml.safe_load(state_file.read_text()) or {}
        reviews = d.get("reviews", [])
        return [
            {"iteration": r.get("iteration", i + 1), "score": float(r.get("score", 0))}
            for i, r in enumerate(reviews)
            if r and r.get("score") is not None
        ]
    except Exception:
        return []


def _read_current_iteration(project_dir: Path) -> int:
    """Read current iteration from the last review in paper_state.yaml."""
    state_file = project_dir / "auto_research" / "state" / "paper_state.yaml"
    if not state_file.exists():
        return 0
    try:
        d = yaml.safe_load(state_file.read_text()) or {}
        reviews = d.get("reviews") or []
        if reviews:
            return int(reviews[-1].get("iteration", len(reviews)))
        return 0
    except Exception:
        return 0


def _read_paper_title(project_dir: Path) -> str:
    """Read paper title from paper/main.tex \\title{...}."""
    tex = project_dir / "paper" / "main.tex"
    if not tex.exists():
        return ""
    try:
        import re as _re
        text = tex.read_text(errors="replace")
        m = _re.search(r'\\title\{([^}]+)\}', text)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


def _read_project_model(project_dir: Path) -> str:
    """Read model name from project config.yaml, fallback to default."""
    cfg = project_dir / "config.yaml"
    if cfg.exists():
        try:
            d = yaml.safe_load(cfg.read_text()) or {}
            m = d.get("model", "")
            if m and m not in ("claude", ""):
                return m
        except Exception:
            pass
    return "claude-sonnet-4-6"


def _find_pdf(project_dir: Path) -> Optional[Path]:
    paper_dir = project_dir / "paper"
    for pdf in sorted(paper_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
        return pdf
    return None


def _check_webapp_enabled():
    if _DISABLED_FLAG.exists():
        raise HTTPException(503, "Webapp submissions are currently disabled by admin.")


def _queue_position(project_id: str, session) -> int:
    from sqlmodel import func, select as _sel
    project = get_project(session, project_id)
    if not project or project.status != "pending":
        return 0
    count = session.exec(
        _sel(func.count(Project.id))
        .where(Project.status == "pending")
        .where(Project.created_at < project.created_at)
    ).one()
    return int(count) + 1


def _write_user_update(project_dir: Path, message: str, source: str = "webapp"):
    f = project_dir / "auto_research" / "state" / "user_updates.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(f.read_text()) if f.exists() else {}
    updates = data.get("updates", [])
    from datetime import datetime as _dt
    updates.append({"consumed": False, "message": message,
                    "source": source, "timestamp": _dt.utcnow().isoformat()})
    f.write_text(yaml.dump({"updates": updates}, allow_unicode=True))


def _try_submit_or_pending(project, pdir, session, settings) -> str:
    from sqlmodel import select as _sel
    active = session.exec(
        _sel(Project).where(Project.status.in_(["queued", "running"]))
        .where(Project.id != project.id)
    ).all()
    if active:
        update_project(session, project, status="pending")
        return "pending"
    log_dir = pdir / "logs"
    log_dir.mkdir(exist_ok=True)
    job_id = submit_job(project.id, project.mode, project.max_iterations,
                        pdir, log_dir, settings) if slurm_available() else "local"
    update_project(session, project, status="queued", slurm_job_id=job_id)
    return "queued"


# ── pages ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((_STATIC_DIR / "app.html").read_text())


# ── auth ──────────────────────────────────────────────────────────────────────

@router.post("/auth/send-link")
async def auth_send_link(request: Request):
    settings = get_settings()
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    # Per-email whitelist (takes priority over domain check)
    if settings.allowed_emails:
        if email not in settings.allowed_emails:
            raise HTTPException(403, "This email address is not authorised.")
    elif settings.email_domains:
        domain = email.split("@")[-1]
        if domain not in settings.email_domains:
            raise HTTPException(403, f"Email domain not allowed. Allowed: {', '.join(settings.email_domains)}")

    token = make_token(email, settings.secret_key)
    link = f"{settings.base_url}/auth/verify?token={token}"

    print(f"\n  *** MAGIC LINK for {email} ***\n  {link}\n", flush=True)

    ok = send_magic_link_email(settings, email, link)
    if not ok:
        logger.warning(f"Email delivery failed — magic link printed to server console only")
    return JSONResponse({"ok": True})


@router.get("/auth/verify")
async def auth_verify(request: Request, token: str = ""):
    settings = get_settings()
    email = verify_token(token, settings.secret_key)
    if not email:
        return HTMLResponse(
            "<html><body><p>Login link expired or invalid. "
            "<a href='/'>Try again</a>.</p></body></html>",
            status_code=400,
        )
    with get_session(settings.db_path) as session:
        user = get_or_create_user_by_email(session, email)
        request.session["user_id"] = user.id
    return RedirectResponse("/")


@router.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@router.get("/api/me")
async def api_me(request: Request):
    user = _get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False})
    return JSONResponse({
        "authenticated": True,
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "is_admin": _is_admin(user),
    })


# ── projects API ──────────────────────────────────────────────────────────────

@router.get("/api/projects")
async def api_list_projects(request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        projects = get_all_projects(session) if _is_admin(user) else get_projects_for_user(session, user.id)
        # Refresh scores from disk
        # Pre-fetch user emails for admin view
        user_email_cache: dict[str, str] = {}
        if _is_admin(user):
            from .db import get_user as _get_user
            for p in projects:
                if p.user_id not in user_email_cache:
                    owner = _get_user(session, p.user_id)
                    user_email_cache[p.user_id] = owner.email if owner else p.user_id
        result = []
        for p in projects:
            pdir = _project_dir(settings, p.user_id, p.id)
            score = _read_project_score(pdir)
            pdf = _find_pdf(pdir)
            d = {
                "id": p.id,
                "name": p.name,
                "title": p.title,
                "idea": p.idea,
                "venue": p.venue,
                "mode": p.mode,
                "status": p.status,
                "score": score,
                "has_pdf": pdf is not None,
                "slurm_job_id": p.slurm_job_id,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
                "user_email": user_email_cache.get(p.user_id, ""),
            }
            result.append(d)
        return JSONResponse(result)


@router.post("/api/projects")
async def api_create_project(
    request: Request,
    name: str = Form(""),
    title: str = Form(""),
    idea: str = Form(""),
    venue: str = Form("NeurIPS"),
    venue_format: str = Form("neurips"),
    venue_pages: int = Form(9),
    mode: str = Form("paper"),
    max_iterations: int = Form(3),
    pdf_file: Optional[UploadFile] = File(None),
    telegram_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    comment: str = Form(""),
):
    user = _require_user(request)
    _check_webapp_enabled()
    max_iterations = min(max_iterations, MAX_ITER_PER_START)
    settings = get_settings()
    with get_session(settings.db_path) as _s:
        user_projects = get_projects_for_user(_s, user.id)
        if len(user_projects) >= MAX_PROJECTS_PER_USER:
            raise HTTPException(400, f"Max {MAX_PROJECTS_PER_USER} projects per user.")
        active = [p for p in user_projects if p.status in ("queued", "running", "pending")]
        if active:
            raise HTTPException(400, "You already have an active project. Wait for it to finish.")

    slug = _slugify(name or title or idea[:40] or "project")
    project_id = str(uuid.uuid4())[:8] + "-" + slug

    pdir = _project_dir(settings, user.id, project_id)
    pdir.mkdir(parents=True, exist_ok=True)
    log_dir = pdir / "logs"
    log_dir.mkdir(exist_ok=True)
    paper_dir = pdir / "paper"

    # Handle PDF upload
    has_pdf_upload = False
    if pdf_file and pdf_file.filename:
        upload_path = pdir / "uploaded.pdf"
        pdf_bytes = await pdf_file.read()
        with open(upload_path, "wb") as f:
            f.write(pdf_bytes)
        has_pdf_upload = True
        # Extract text as idea if none provided
        if not idea.strip():
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                idea = "\n".join(page.get_text() for page in doc).strip()[:8000]
                doc.close()
            except Exception:
                pass

    # Check if venue template is bundled
    template_available = has_venue_template(venue_format)
    if template_available:
        copy_venue_template(venue_format, paper_dir)
    else:
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "figures").mkdir(exist_ok=True)

    # Copy agent prompt templates (with variable substitution)
    agents_dir = pdir / "agents"
    agents_dir.mkdir(exist_ok=True)
    _templates_dir = Path(__file__).parent.parent / "templates" / "agents"
    if _templates_dir.exists():
        venue_name = venue or venue_format or "NeurIPS"
        for _pf in _templates_dir.glob("*.prompt"):
            _content = _pf.read_text()
            _content = _content.replace("{PROJECT_NAME}", slug)
            _content = _content.replace("{PAPER_TITLE}", title or slug)
            _content = _content.replace("{VENUE_NAME}", venue_name)
            _content = _content.replace("{VENUE_FORMAT}", venue_format or "neurips")
            _content = _content.replace("{VENUE_PAGES}", str(venue_pages))
            _content = _content.replace("{LATEX_DIR}", "paper")
            _content = _content.replace("{FIGURES_DIR}", "paper/figures")
            (agents_dir / _pf.name).write_text(_content)

    initial_status = "queued" if template_available else "waiting_template"

    with get_session(settings.db_path) as session:
        project = create_project(
            session,
            id=project_id,
            user_id=user.id,
            name=slug,
            title=title or slug,
            idea=idea,
            venue=venue,
            venue_format=venue_format,
            venue_pages=venue_pages,
            max_iterations=max_iterations,
            mode=mode,
            status=initial_status,
            has_pdf_upload=has_pdf_upload,
            telegram_token=telegram_token,
            telegram_chat_id=telegram_chat_id,
        )
        _write_config_yaml(pdir, project)

        if comment.strip():
            _write_user_update(pdir, comment.strip(), source="webapp_create")

        if not template_available:
            # Ask user to send template via Telegram, don't submit job yet
            send_telegram_notify(
                f"📦 <b>{project.name}</b> needs a <b>{venue}</b> LaTeX template.\n\n"
                f"Please reply with a direct download link (.zip) to the official {venue} author kit.\n"
                f"The system will automatically extract and set up the template.",
                bot_token=project.telegram_token,
                chat_id=project.telegram_chat_id,
            )
            return JSONResponse({
                "id": project.id,
                "name": project.name,
                "status": initial_status,
                "slurm_job_id": "",
            }, status_code=201)

        final_status = _try_submit_or_pending(project, pdir, session, settings)

        send_telegram_notify(
            f"🔬 <b>{project.name}</b> submitted ({final_status})\n"
            f"Venue: {project.venue} · {project.max_iterations} iter",
            bot_token=project.telegram_token,
            chat_id=project.telegram_chat_id,
        )

        return JSONResponse({
            "id": project.id,
            "name": project.name,
            "status": final_status,
            "slurm_job_id": project.slurm_job_id,
        }, status_code=201)


@router.get("/api/projects/{project_id}")
async def api_get_project(project_id: str, request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        pdir = _project_dir(settings, project.user_id, project_id)
        score = _read_project_score(pdir)
        pdf = _find_pdf(pdir)
        owner = session.get(User, project.user_id)
        return JSONResponse({
            "id": project.id,
            "name": project.name,
            "title": project.title,
            "idea": project.idea,
            "venue": project.venue,
            "mode": project.mode,
            "status": project.status,
            "score": score,
            "paper_title": _read_paper_title(pdir),
            "score_history": _read_score_history(pdir),
            "current_iteration": _read_current_iteration(pdir),
            "max_iterations": project.max_iterations,
            "has_pdf": pdf is not None,
            "slurm_job_id": project.slurm_job_id,
            "queue_position": _queue_position(project_id, session),
            "user_email": owner.email if owner else "",
            "model": _read_project_model(pdir),
            "environment": "ROCS Testbed" if project.slurm_job_id and project.slurm_job_id != "local" else "Local",
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
        })


@router.post("/api/projects/{project_id}/stop")
async def api_stop_project(project_id: str, request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if project.slurm_job_id and project.slurm_job_id != "local":
            cancel_job(project.slurm_job_id)
        update_project(session, project, status="stopped")
        return JSONResponse({"ok": True})


@router.post("/api/projects/{project_id}/restart")
async def api_restart_project(project_id: str, request: Request):
    user = _require_user(request)
    _check_webapp_enabled()
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if project.status not in ("stopped", "failed"):
            raise HTTPException(400, "Only stopped or failed projects can be restarted")
        active = [p for p in get_projects_for_user(session, project.user_id)
                  if p.status in ("queued", "running", "pending") and p.id != project_id]
        if active:
            raise HTTPException(400, "You already have an active project.")
        pdir = _project_dir(settings, project.user_id, project_id)
        final_status = _try_submit_or_pending(project, pdir, session, settings)
        send_telegram_notify(
            f"🔄 <b>{project.name}</b> restarted ({final_status})",
            bot_token=project.telegram_token,
            chat_id=project.telegram_chat_id,
        )
        return JSONResponse({"ok": True, "status": final_status, "slurm_job_id": project.slurm_job_id})


@router.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str, request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if project.slurm_job_id and project.slurm_job_id != "local":
            cancel_job(project.slurm_job_id)
        pdir = _project_dir(settings, project.user_id, project_id)
        delete_project(session, project_id)
    shutil.rmtree(pdir, ignore_errors=True)
    return JSONResponse({"ok": True})


@router.post("/api/projects/{project_id}/continue")
async def api_continue_project(project_id: str, request: Request):
    user = _require_user(request)
    _check_webapp_enabled()
    body = await request.json()
    additional = max(1, min(int(body.get("additional_iterations", 3)), MAX_ITER_PER_START))
    comment = (body.get("comment") or "").strip()
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if project.status != "done":
            raise HTTPException(400, "Only done projects can be continued.")
        active = [p for p in get_projects_for_user(session, project.user_id)
                  if p.status in ("queued", "running", "pending") and p.id != project_id]
        if active:
            raise HTTPException(400, "You already have an active project.")
        new_max = project.max_iterations + additional
        update_project(session, project, max_iterations=new_max)
        pdir = _project_dir(settings, project.user_id, project_id)
        _write_config_yaml(pdir, project)
        if comment:
            _write_user_update(pdir, comment, source="webapp_continue")
        final_status = _try_submit_or_pending(project, pdir, session, settings)
        return JSONResponse({"ok": True, "status": final_status, "max_iterations": new_max})


@router.get("/api/system/status")
async def api_system_status():
    """Public endpoint — returns webapp gate state (no auth required)."""
    return JSONResponse({"disabled": _DISABLED_FLAG.exists()})


@router.get("/api/admin/status")
async def api_admin_status(request: Request):
    _require_admin(request)
    return JSONResponse({"disabled": _DISABLED_FLAG.exists()})


@router.post("/api/admin/disable")
async def api_admin_disable(request: Request):
    _require_admin(request)
    _DISABLED_FLAG.touch()
    return JSONResponse({"disabled": True})


@router.post("/api/admin/enable")
async def api_admin_enable(request: Request):
    _require_admin(request)
    _DISABLED_FLAG.unlink(missing_ok=True)
    return JSONResponse({"disabled": False})


@router.post("/api/admin/killall")
async def api_admin_killall(request: Request):
    """Cancel ALL active jobs (queued/running/pending) across all users."""
    _require_admin(request)
    settings = get_settings()
    stopped = []
    with get_session(settings.db_path) as session:
        from sqlmodel import select as _sel
        active = session.exec(
            _sel(Project).where(Project.status.in_(["queued", "running", "pending"]))
        ).all()
        for p in active:
            if p.slurm_job_id and p.slurm_job_id != "local":
                cancel_job(p.slurm_job_id)
            update_project(session, p, status="stopped")
            stopped.append(p.id)
    return JSONResponse({"stopped": stopped, "count": len(stopped)})


@router.get("/api/projects/{project_id}/pdf")
async def api_get_pdf(project_id: str, request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        owner_id = project.user_id
    pdir = _project_dir(settings, owner_id, project_id)
    pdf = _find_pdf(pdir)
    if not pdf:
        raise HTTPException(404, "PDF not ready")
    return FileResponse(pdf, media_type="application/pdf",
                        filename=pdf.name, content_disposition_type="inline")


@router.get("/api/projects/{project_id}/zip")
async def api_download_zip(project_id: str, request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        owner_id = project.user_id
    pdir = _project_dir(settings, owner_id, project_id)

    buf = io.BytesIO()
    skip_exts = {".aux", ".log", ".fdb_latexmk", ".fls", ".synctex.gz",
                 ".out", ".toc", ".lof", ".lot", ".blg"}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # paper/ — LaTeX source, PDF, figures, style files (skip build artifacts)
        paper_dir = pdir / "paper"
        if paper_dir.exists():
            for f in paper_dir.rglob("*"):
                if f.is_file() and f.suffix not in skip_exts and "__pycache__" not in str(f):
                    zf.write(f, f.relative_to(pdir))

        # code directories
        for subdir in ("experiments", "scripts", "code"):
            d = pdir / subdir
            if d.exists():
                for f in d.rglob("*.py"):
                    if "__pycache__" not in str(f):
                        zf.write(f, f.relative_to(pdir))

        # results
        results_dir = pdir / "results"
        if results_dir.exists():
            for f in results_dir.rglob("*"):
                if f.is_file() and f.suffix in {".csv", ".json", ".txt", ".yaml", ".tsv"}:
                    zf.write(f, f.relative_to(pdir))

        # config + key state files
        for rel in (
            "config.yaml",
            "auto_research/state/paper_state.yaml",
            "auto_research/state/findings.yaml",
            "auto_research/state/action_plan.yaml",
            "auto_research/state/memory.yaml",
        ):
            f = pdir / rel
            if f.exists():
                zf.write(f, rel)

    buf.seek(0)
    slug = project_id.replace("/", "_")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}.zip"'},
    )


@router.get("/api/projects/{project_id}/log")
async def api_get_log(project_id: str, request: Request, lines: int = 200):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        owner_id = project.user_id
    pdir = _project_dir(settings, owner_id, project_id)
    log_dir = pdir / "logs"

    # Find the latest log file
    log_lines: list[str] = []
    log_file = ""
    for pattern in ["slurm_*.out", "orchestrator.log", "*.log"]:
        matches = sorted(log_dir.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        if matches:
            log_file = str(matches[0])
            try:
                all_lines = matches[0].read_text(errors="replace").splitlines()
                log_lines = all_lines[-lines:]
            except Exception:
                pass
            break

    return JSONResponse({"lines": log_lines, "log_file": log_file})


# ── SSE log stream ────────────────────────────────────────────────────────────

@router.get("/api/projects/{project_id}/stream")
async def api_stream_log(project_id: str, request: Request):
    user = _require_user(request)
    settings = get_settings()

    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        owner_id = project.user_id

    pdir = _project_dir(settings, owner_id, project_id)
    log_dir = pdir / "logs"

    async def event_generator():
        sent_lines = 0
        while True:
            if await request.is_disconnected():
                break

            # Find latest log file
            log_file = None
            for pattern in ["slurm_*.out", "orchestrator.log", "*.log"]:
                matches = sorted(log_dir.glob(pattern),
                                 key=lambda p: p.stat().st_mtime if p.exists() else 0,
                                 reverse=True)
                if matches:
                    log_file = matches[0]
                    break

            if log_file and log_file.exists():
                try:
                    all_lines = log_file.read_text(errors="replace").splitlines()
                    new_lines = all_lines[sent_lines:]
                    for line in new_lines:
                        payload = json.dumps({"line": line})
                        yield f"data: {payload}\n\n"
                    sent_lines += len(new_lines)
                except Exception:
                    pass

            # Also emit status
            with get_session(settings.db_path) as session:
                p = get_project(session, project_id)
                if p:
                    score = _read_project_score(pdir)
                    payload = json.dumps({"status": p.status, "score": score})
                    yield f"event: status\ndata: {payload}\n\n"

            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── venues ────────────────────────────────────────────────────────────────────

@router.get("/api/venues")
async def api_venues():
    """Return supported venues list."""
    venues = [
        # ML / AI
        {"name": "NeurIPS",    "format": "neurips",  "pages": 9},
        {"name": "ICML",       "format": "icml",     "pages": 9},
        {"name": "ICLR",       "format": "iclr",     "pages": 9},
        {"name": "ACL",        "format": "acl",      "pages": 8},
        {"name": "EMNLP",      "format": "emnlp",    "pages": 8},
        {"name": "CVPR",       "format": "cvpr",     "pages": 8},
        {"name": "MLSys",      "format": "mlsys",    "pages": 8},
        {"name": "EuroMLSys",  "format": "sigplan",  "pages": 6},
        # Systems
        {"name": "SOSP",       "format": "acm",      "pages": 14},
        {"name": "EuroSys",    "format": "acm",      "pages": 12},
        {"name": "NSDI",       "format": "usenix",   "pages": 14},
        {"name": "OSDI",       "format": "usenix",   "pages": 14},
        {"name": "USENIX ATC", "format": "usenix",   "pages": 12},
        {"name": "IEEE S&P",   "format": "ieee",     "pages": 13},
        # Networking
        {"name": "INFOCOM",    "format": "infocom",  "pages": 9},
    ]
    return JSONResponse(venues)
