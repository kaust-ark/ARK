"""All API + page endpoints for ARK webapp."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path as _Path
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ark.webapp.routes")

MAX_PROJECTS_PER_USER = 10
MAX_ITER_PER_START = 3
from ark.paths import get_ark_root as _get_ark_root
_DISABLED_FLAG = None  # lazy


def _disabled_flag() -> _Path:
    global _DISABLED_FLAG
    if _DISABLED_FLAG is None:
        _DISABLED_FLAG = _get_ark_root() / "ark_webapp" / "disabled"
    return _DISABLED_FLAG

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.requests import Request

from authlib.integrations.starlette_client import OAuth as _OAuth

from .auth import make_token, verify_token
from .config import get_settings

# Lazy-initialized Google OAuth client
_google_oauth: _OAuth | None = None


def _get_google_oauth() -> _OAuth | None:
    """Return authlib OAuth client if Google credentials are configured."""
    global _google_oauth
    if _google_oauth is not None:
        return _google_oauth
    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        return None
    _google_oauth = _OAuth()
    _google_oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return _google_oauth
from .db import (
    Feedback,
    Project,
    User,
    create_feedback,
    create_project,
    delete_project,
    get_all_feedbacks,
    get_all_projects,
    get_feedbacks_for_user,
    get_or_create_user_by_email,
    get_project,
    get_projects_for_user,
    get_session,
    get_user,
    update_project,
)
from .jobs import cancel_job, cancel_local_job, launch_local_job, slurm_available, slurm_state_to_status, submit_job
from .notify import send_completion_email, send_magic_link_email, send_telegram_login_link, send_telegram_notify, send_welcome_email
from .templates import copy_venue_template, has_venue_template

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"

# ── helpers ──────────────────────────────────────────────────────────────────

_SLUG_ADJECTIVES = ["Red", "Blue", "Swift", "Calm", "Bold", "Keen", "Warm", "Bright"]
_SLUG_NOUNS = ["Comet", "Orbit", "Spark", "Quasar", "Nova", "Prism", "Pulse", "Ridge"]

def _random_slug() -> str:
    return f"{random.choice(_SLUG_ADJECTIVES)}-{random.choice(_SLUG_NOUNS)}"


def _extract_and_validate_template(zip_bytes: bytes, paper_dir: Path) -> str | None:
    """Extract a user-uploaded ZIP template into paper_dir and try to compile.

    Returns None on success, or an error message string on failure.
    """
    import subprocess
    import tempfile

    # Extract ZIP
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception:
        return "Invalid ZIP file. Please upload a valid .zip archive."

    # Security: reject entries with path traversal
    for info in zf.infolist():
        if info.filename.startswith("/") or ".." in info.filename:
            return "ZIP file contains unsafe paths. Please repack without absolute or '..' paths."

    # Determine if files are inside a single top-level directory
    top_dirs = {n.split("/")[0] for n in zf.namelist() if "/" in n}
    names = [n for n in zf.namelist() if not n.endswith("/")]
    has_wrapper_dir = len(top_dirs) == 1 and all(n.startswith(list(top_dirs)[0] + "/") for n in names)
    prefix = list(top_dirs)[0] + "/" if has_wrapper_dir else ""

    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = info.filename[len(prefix):] if prefix and info.filename.startswith(prefix) else info.filename
        if not rel:
            continue
        dest = paper_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src_f, open(dest, "wb") as dst_f:
            dst_f.write(src_f.read())

    # Also extract any nested .zip style files (e.g., NeurIPS styles)
    for nested_zip in paper_dir.glob("*.zip"):
        try:
            with zipfile.ZipFile(nested_zip) as nzf:
                for info in nzf.infolist():
                    if info.is_dir():
                        continue
                    fname = Path(info.filename).name
                    if Path(fname).suffix.lower() in (".sty", ".cls", ".bst"):
                        dst = paper_dir / fname
                        if not dst.exists():
                            with nzf.open(info) as sf, open(dst, "wb") as df:
                                df.write(sf.read())
        except Exception:
            pass

    # Find main .tex file
    tex_files = list(paper_dir.glob("*.tex"))
    main_tex = None
    for tf in tex_files:
        if tf.name == "main.tex":
            main_tex = tf
            break
    if not main_tex:
        # Try to find one with \documentclass
        for tf in tex_files:
            content = tf.read_text(errors="ignore")
            if r"\documentclass" in content:
                main_tex = tf
                break
    if not main_tex:
        return "No LaTeX main file found. ZIP must contain a .tex file with \\documentclass."

    # Rename to main.tex if needed
    if main_tex.name != "main.tex":
        target = paper_dir / "main.tex"
        if not target.exists():
            main_tex.rename(target)
            main_tex = target

    # Ensure figures directory exists
    (paper_dir / "figures").mkdir(exist_ok=True)

    # Ensure references.bib exists
    if not (paper_dir / "references.bib").exists():
        (paper_dir / "references.bib").write_text("")

    # Try compilation (quick pdflatex pass — just check for fatal errors)
    try:
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
            cwd=str(paper_dir),
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            # Extract the actual error from log
            log_text = result.stdout.decode(errors="replace")
            # Find the first "! " error line
            error_lines = []
            for line in log_text.splitlines():
                if line.startswith("! "):
                    error_lines.append(line)
                    if len(error_lines) >= 3:
                        break
            error_msg = "\n".join(error_lines) if error_lines else "Unknown LaTeX error"
            return f"Template compilation failed:\n{error_msg}\n\nPlease fix the template and re-upload."
    except FileNotFoundError:
        # pdflatex not installed — skip validation, trust the user
        logger.warning("pdflatex not found, skipping template validation")
    except subprocess.TimeoutExpired:
        return "Template compilation timed out (>60s). Please simplify the template."

    # Clean up aux files from test compilation
    for ext in (".aux", ".log", ".out", ".pdf", ".toc", ".bbl", ".blg", ".fls", ".fdb_latexmk"):
        for f in paper_dir.glob(f"*{ext}"):
            f.unlink(missing_ok=True)

    return None


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


def _write_config_yaml(project_dir: Path, project: Project, model: str = "claude-sonnet-4-6"):
    """Write config.yaml that ark orchestrator will read."""
    # Map webapp model value to orchestrator model backend
    MODEL_MAP = {
        "claude-sonnet-4-6": ("claude", "claude-sonnet-4-6"),
        "claude-opus-4-6": ("claude", "claude-opus-4-6"),
        "claude-haiku-4-5": ("claude", "claude-haiku-4-5"),
        "gemini": ("gemini", ""),
    }
    model_backend, model_variant = MODEL_MAP.get(model, ("claude", "claude-sonnet-4-6"))

    config = {
        "project": project.name,
        "title": project.title or project.name,
        "idea": project.idea,
        "venue": project.venue,
        "venue_format": project.venue_format,
        "venue_pages": project.venue_pages,
        "mode": project.mode,
        "model": model_backend,
        "model_variant": model_variant,
        "max_iterations": project.max_iterations,
        "language": "en",
        "code_dir": str(project_dir),
        "latex_dir": "paper",
        "figures_dir": "paper/figures",
        "figure_generation": "nano_banana",
        "nano_banana_model": "pro",
    }
    if project.telegram_token:
        config["telegram_bot_token"] = project.telegram_token
    if project.telegram_chat_id:
        config["telegram_chat_id"] = project.telegram_chat_id
    uploaded_pdf = project_dir / "uploaded.pdf"
    if uploaded_pdf.exists():
        config["uploaded_pdf"] = str(uploaded_pdf)
    # Build goal_anchor from title + venue + idea
    title = project.title or project.name
    venue_name = project.venue or project.venue_format or "NeurIPS"
    anchor_parts = ["## Goal Anchor\n"]
    anchor_parts.append(f"**Paper Title**: {title}")
    anchor_parts.append(f"**Target Venue**: {venue_name} ({project.venue_format}, {project.venue_pages} pages)\n")
    if project.idea:
        anchor_parts.append(f"**Research Idea**:\n{project.idea}")
    config["goal_anchor"] = "\n".join(anchor_parts)
    config_path = project_dir / "config.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))


def _clean_project_state(project_dir: Path, keep_deep_research: bool = True):
    """Remove all generated state/results for a fresh restart.

    Preserves: config.yaml, uploaded.pdf, venue template files (.cls/.sty/.bst),
    agent prompts, and optionally deep_research.md/pdf.
    """
    # Clean auto_research/state/
    state_dir = project_dir / "auto_research" / "state"
    if state_dir.exists():
        for f in state_dir.iterdir():
            if keep_deep_research and f.name.startswith("deep_research"):
                continue
            if f.is_file():
                f.unlink()

    # Clean auto_research/logs/
    logs_dir = project_dir / "auto_research" / "logs"
    if logs_dir.exists():
        shutil.rmtree(logs_dir, ignore_errors=True)
        logs_dir.mkdir(exist_ok=True)

    # Clean results/ and experiments/
    for dirname in ("results", "experiments"):
        d = project_dir / dirname
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(exist_ok=True)

    # Clean paper/ — keep venue template files, remove generated content
    paper_dir = project_dir / "paper"
    if paper_dir.exists():
        keep_exts = {".cls", ".sty", ".bst"}
        for f in paper_dir.iterdir():
            if f.is_dir():
                if f.name == "figures":
                    shutil.rmtree(f, ignore_errors=True)
                    f.mkdir(exist_ok=True)
            elif f.suffix not in keep_exts:
                f.unlink()

    # Clean scripts/ (generated figure scripts)
    scripts_dir = project_dir / "scripts"
    if scripts_dir.exists():
        shutil.rmtree(scripts_dir, ignore_errors=True)

    # Remove .git (will be re-initialized)
    git_dir = project_dir / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir, ignore_errors=True)


def _write_user_instructions(project_dir: Path, message: str, source: str = "webapp_create"):
    """Write a persistent instruction to user_instructions.yaml."""
    state_dir = project_dir / "auto_research" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    instructions_file = state_dir / "user_instructions.yaml"
    data = {}
    if instructions_file.exists():
        data = yaml.safe_load(instructions_file.read_text()) or {}
    entries = data.get("instructions", [])
    entries.append({
        "message": message,
        "source": source,
        "timestamp": datetime.now().isoformat(),
    })
    data["instructions"] = entries
    instructions_file.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))


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
    """Read model variant from project config.yaml, fallback to default."""
    cfg = project_dir / "config.yaml"
    if cfg.exists():
        try:
            d = yaml.safe_load(cfg.read_text()) or {}
            # Prefer model_variant (e.g., "claude-sonnet-4-6"), fall back to model backend
            variant = d.get("model_variant", "")
            if variant:
                return variant
            backend = d.get("model", "")
            if backend == "gemini":
                return "gemini"
        except Exception:
            pass
    return "claude-sonnet-4-6"


def _find_pdf(project_dir: Path) -> Optional[Path]:
    paper_dir = project_dir / "paper"
    for pdf in sorted(paper_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
        return pdf
    return None


def _check_webapp_enabled():
    if _disabled_flag().exists():
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
    if slurm_available():
        job_id = submit_job(project.id, project.mode, project.max_iterations,
                            pdir, log_dir, settings)
        update_project(session, project, status="queued", slurm_job_id=job_id)
        return "queued"
    else:
        job_id = launch_local_job(project.id, project.mode, project.max_iterations,
                                  pdir, log_dir, settings)
        update_project(session, project, status="running", slurm_job_id=job_id)
        return "running"


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
        user, is_new = get_or_create_user_by_email(session, email)
        request.session["user_id"] = user.id
        if is_new:
            asyncio.get_event_loop().run_in_executor(
                None, send_welcome_email, settings, email, user.name, settings.base_url,
            )
    return RedirectResponse("/")


@router.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


_GOOGLE_REDIRECT_URI = "https://kaust-ark.github.io/oauth-callback"


@router.get("/auth/google")
async def auth_google(request: Request):
    oauth = _get_google_oauth()
    if not oauth:
        raise HTTPException(400, "Google login is not configured on this server.")
    return await oauth.google.authorize_redirect(
        request, _GOOGLE_REDIRECT_URI, prompt="select_account"
    )


@router.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    oauth = _get_google_oauth()
    if not oauth:
        raise HTTPException(400, "Google login is not configured on this server.")
    settings = get_settings()
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        logger.warning(f"Google OAuth error: {exc}")
        return RedirectResponse("/?google_error=1")

    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        return RedirectResponse("/?google_error=1")

    # Apply same allow-list checks as magic link
    denied = False
    if settings.allowed_emails:
        if email not in settings.allowed_emails:
            denied = True
    elif settings.email_domains:
        if email.split("@")[-1] not in settings.email_domains:
            denied = True

    if denied:
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Access Denied — ARK</title>
  <style>
    body {{ font-family: sans-serif; display: flex; align-items: center; justify-content: center;
           min-height: 100vh; margin: 0; background: #f0fdfa; }}
    .card {{ background: #fff; border-radius: 16px; padding: 48px 52px; max-width: 420px;
             box-shadow: 0 4px 24px rgba(0,0,0,.08); text-align: center; }}
    h2 {{ color: #991b1b; margin-bottom: 12px; }}
    p {{ color: #555; line-height: 1.6; }}
    a {{ color: #0d9488; }}
    .back {{ margin-top: 24px; display: inline-block; color: #0d9488; font-size: .9rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Access Denied</h2>
    <p>Your Google account (<strong>{email}</strong>) is not authorized to access ARK.</p>
    <p>To request access, contact<br/>
       <a href="mailto:jihao.xin@kaust.edu.sa">jihao.xin@kaust.edu.sa</a></p>
    <a class="back" href="/">← Back to login</a>
  </div>
</body>
</html>""",
            status_code=403,
        )

    with get_session(settings.db_path) as session:
        user, is_new = get_or_create_user_by_email(session, email)
        request.session["user_id"] = user.id
        if is_new:
            asyncio.get_event_loop().run_in_executor(
                None, send_welcome_email, settings, email, user.name, settings.base_url,
            )
    return RedirectResponse("/")


@router.get("/auth/google/enabled")
async def auth_google_enabled():
    """Frontend polls this to know whether to show Google button."""
    return JSONResponse({"enabled": _get_google_oauth() is not None})


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
        "telegram_token": user.telegram_token or "",
        "telegram_chat_id": user.telegram_chat_id or "",
    })


# ── projects API ──────────────────────────────────────────────────────────────

@router.get("/api/projects")
async def api_list_projects(request: Request, scope: str = "mine"):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        if scope == "all" and _is_admin(user):
            projects = get_all_projects(session)
        else:
            projects = get_projects_for_user(session, user.id)
        # Refresh scores from disk
        # Pre-fetch user emails for admin view
        user_email_cache: dict[str, str] = {}
        if _is_admin(user):
            for p in projects:
                if p.user_id not in user_email_cache:
                    owner = get_user(session, p.user_id)
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
    title: str = Form(""),
    idea: str = Form(""),
    venue: str = Form("NeurIPS"),
    venue_format: str = Form("neurips"),
    venue_pages: int = Form(9),
    mode: str = Form("paper"),
    max_iterations: int = Form(3),
    pdf_file: Optional[UploadFile] = File(None),
    template_zip: Optional[UploadFile] = File(None),
    model: str = Form("claude-sonnet-4-6"),
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

    project_id = _random_slug()
    # Ensure uniqueness — append -2, -3, … if the ID already exists
    with get_session(settings.db_path) as _s:
        base_id = project_id
        counter = 2
        while _s.get(Project, project_id):
            project_id = f"{base_id}-{counter}"
            counter += 1

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

    # Handle custom template upload
    if venue_format == "custom" and template_zip and template_zip.filename:
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "figures").mkdir(exist_ok=True)
        zip_bytes = await template_zip.read()
        tpl_result = _extract_and_validate_template(zip_bytes, paper_dir)
        if tpl_result is not None:
            # Cleanup on failure
            shutil.rmtree(pdir, ignore_errors=True)
            raise HTTPException(400, tpl_result)
        template_available = True
    elif venue_format == "custom":
        raise HTTPException(400, "Please upload a template ZIP file for the Customized venue.")
    else:
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
            _content = _content.replace("{PROJECT_NAME}", project_id)
            _content = _content.replace("{PAPER_TITLE}", title or project_id)
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
            name=project_id,
            title=title or project_id,
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
        # Persist telegram fields on user for auto-fill on next project
        if telegram_token or telegram_chat_id:
            db_user = get_user(session, user.id)
            if db_user:
                if telegram_token:
                    db_user.telegram_token = telegram_token
                if telegram_chat_id:
                    db_user.telegram_chat_id = telegram_chat_id
                session.add(db_user)
                session.commit()
        _write_config_yaml(pdir, project, model=model)

        if comment.strip():
            _write_user_update(pdir, comment.strip(), source="webapp_create")
            _write_user_instructions(pdir, comment.strip(), source="webapp_create")

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
            "venue_format": project.venue_format,
            "venue_pages": project.venue_pages,
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
            "telegram_token": project.telegram_token,
            "telegram_chat_id": project.telegram_chat_id,
            "has_deep_research": (pdir / "auto_research" / "state" / "deep_research.md").exists(),
            "environment": "ROCS Testbed" if project.slurm_job_id and project.slurm_job_id != "local" else "Local",
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
        })


@router.patch("/api/projects/{project_id}")
async def api_patch_project(project_id: str, request: Request):
    user = _require_user(request)
    body = await request.json()
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if "title" in body:
            update_project(session, project, title=body["title"])
        return JSONResponse({"ok": True})


@router.post("/api/projects/{project_id}/stop")
async def api_stop_project(project_id: str, request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if project.slurm_job_id:
            if project.slurm_job_id.startswith("local:"):
                pid_str = project.slurm_job_id[len("local:"):]
                if pid_str.isdigit():
                    cancel_local_job(int(pid_str))
            else:
                cancel_job(project.slurm_job_id)
        update_project(session, project, status="stopped")
        return JSONResponse({"ok": True})


@router.post("/api/projects/{project_id}/restart")
async def api_restart_project(project_id: str, request: Request):
    user = _require_user(request)
    _check_webapp_enabled()
    settings = get_settings()

    # Parse JSON body (new restart dialog sends settings)
    try:
        body = await request.json()
    except Exception:
        body = {}

    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if project.status not in ("stopped", "failed", "done"):
            raise HTTPException(400, "Only stopped, failed, or done projects can be restarted")
        active = [p for p in get_projects_for_user(session, project.user_id)
                  if p.status in ("queued", "running", "pending") and p.id != project_id]
        if active:
            raise HTTPException(400, "You already have an active project.")
        pdir = _project_dir(settings, project.user_id, project_id)

        # Update project fields from request body
        if body.get("idea"):
            project.idea = body["idea"]
        if body.get("venue"):
            project.venue = body["venue"]
        if body.get("venue_format"):
            project.venue_format = body["venue_format"]
        if "venue_pages" in body:
            project.venue_pages = int(body["venue_pages"])
        if "max_iterations" in body:
            project.max_iterations = max(1, min(3, int(body["max_iterations"])))
        if "telegram_token" in body:
            project.telegram_token = body["telegram_token"]
        if "telegram_chat_id" in body:
            project.telegram_chat_id = body["telegram_chat_id"]
        project.score = 0.0

        # Clean up project state for fresh restart
        redo_deep_research = body.get("redo_deep_research", False)
        _clean_project_state(pdir, keep_deep_research=not redo_deep_research)

        # Rewrite config.yaml with updated settings
        model = body.get("model", "claude-sonnet-4-6")
        _write_config_yaml(pdir, project, model=model)

        # Write instructions if provided
        comment = body.get("comment", "").strip()
        if comment:
            _write_user_instructions(pdir, comment, source="webapp_restart")

        session.add(project)
        session.commit()
        session.refresh(project)

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
        if project.slurm_job_id:
            if project.slurm_job_id.startswith("local:"):
                pid_str = project.slurm_job_id[len("local:"):]
                if pid_str.isdigit():
                    cancel_local_job(int(pid_str))
            else:
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
        if project.status not in ("done", "stopped", "failed"):
            raise HTTPException(400, "Only done, stopped, or failed projects can be continued.")
        active = [p for p in get_projects_for_user(session, project.user_id)
                  if p.status in ("queued", "running", "pending") and p.id != project_id]
        if active:
            raise HTTPException(400, "You already have an active project.")
        new_max = project.max_iterations + additional
        update_project(session, project, max_iterations=new_max)
        pdir = _project_dir(settings, project.user_id, project_id)
        # Use requested model, or fall back to existing
        model = body.get("model") or _read_project_model(pdir) or "claude-sonnet-4-6"
        _write_config_yaml(pdir, project, model=model)
        if comment:
            _write_user_update(pdir, comment, source="webapp_continue")
            _write_user_instructions(pdir, comment, source="webapp_continue")
        final_status = _try_submit_or_pending(project, pdir, session, settings)
        return JSONResponse({"ok": True, "status": final_status, "max_iterations": new_max})


@router.get("/api/system/status")
async def api_system_status():
    """Public endpoint — returns webapp gate state (no auth required)."""
    return JSONResponse({"disabled": _disabled_flag().exists()})


@router.get("/api/admin/status")
async def api_admin_status(request: Request):
    _require_admin(request)
    return JSONResponse({"disabled": _disabled_flag().exists()})


@router.post("/api/admin/disable")
async def api_admin_disable(request: Request):
    _require_admin(request)
    _disabled_flag().touch()
    return JSONResponse({"disabled": True})


@router.post("/api/admin/enable")
async def api_admin_enable(request: Request):
    _require_admin(request)
    _disabled_flag().unlink(missing_ok=True)
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
            if p.slurm_job_id:
                if p.slurm_job_id.startswith("local:"):
                    pid_str = p.slurm_job_id[len("local:"):]
                    if pid_str.isdigit():
                        cancel_local_job(int(pid_str))
                else:
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
        {"name": "EuroMLSys",  "format": "euromlsys",  "pages": 6},
        # Systems
        {"name": "SOSP",       "format": "sosp",     "pages": 14},
        {"name": "EuroSys",    "format": "sosp",     "pages": 12},
        {"name": "NSDI",       "format": "osdi",     "pages": 14},
        {"name": "OSDI",       "format": "osdi",     "pages": 14},
        {"name": "USENIX ATC", "format": "osdi",     "pages": 12},
        {"name": "IEEE S&P",   "format": "neurips",  "pages": 13},
        # Networking
        {"name": "INFOCOM",    "format": "infocom",  "pages": 9},
    ]
    return JSONResponse(venues)


# ── feedback ───────────────────────────────────────────────────────────────────

@router.post("/api/feedback")
async def api_create_feedback(request: Request):
    user = _require_user(request)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "Message is required")
    project_id = (body.get("project_id") or "").strip()
    settings = get_settings()
    with get_session(settings.db_path) as session:
        # Validate project_id belongs to user if provided
        if project_id:
            proj = get_project(session, project_id)
            if not proj or (proj.user_id != user.id and not _is_admin(user)):
                raise HTTPException(400, "Invalid project")
        fb = create_feedback(session, user_id=user.id, project_id=project_id, message=message)
        return JSONResponse({"id": fb.id, "created_at": fb.created_at.isoformat()}, status_code=201)


@router.get("/api/feedback")
async def api_list_feedback(request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        if _is_admin(user):
            feedbacks = get_all_feedbacks(session)
        else:
            feedbacks = get_feedbacks_for_user(session, user.id)
        # Build user email cache for admin
        user_cache: dict[str, str] = {}
        result = []
        for fb in feedbacks:
            if _is_admin(user) and fb.user_id not in user_cache:
                u = get_user(session, fb.user_id)
                user_cache[fb.user_id] = u.email if u else fb.user_id
            # Resolve project title
            proj_title = ""
            if fb.project_id:
                p = get_project(session, fb.project_id)
                proj_title = (p.title or p.name) if p else ""
            result.append({
                "id": fb.id,
                "message": fb.message,
                "project_id": fb.project_id,
                "project_title": proj_title,
                "user_email": user_cache.get(fb.user_id, ""),
                "created_at": fb.created_at.isoformat(),
            })
        return JSONResponse(result)
