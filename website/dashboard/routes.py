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
import os
import subprocess

logger = logging.getLogger("website.dashboard.routes")

MAX_PROJECTS_PER_USER = 10
MAX_ITER_PER_START = 5
MAX_CONCURRENT_PER_USER = 3
MAX_CONCURRENT_GLOBAL = 10
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

from .auth import make_token, make_share_token, verify_token, verify_share_token
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
    ShareAlias,
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
    get_share_alias,
    get_user,
    update_project,
)
from .crypto import encrypt_text, decrypt_text
from .jobs import (
    cancel_job,
    cancel_local_job,
    launch_cloud_job,
    launch_local_job,
    poll_local_job,
    project_env_prefix,
    project_env_ready,
    slurm_available,
    slurm_state_to_status,
    submit_job,
)
from .notify import send_completion_email, send_magic_link_email, send_telegram_login_link, send_telegram_notify, send_welcome_email
from .templates import copy_venue_template, has_venue_template

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Jinja2 templates for server-rendered pages (so we can inject app_base).
from starlette.templating import Jinja2Templates
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _app_base() -> str:
    """URL path prefix, e.g. '/dashboard'. Sourced from constants.py."""
    from .constants import DASHBOARD_PREFIX
    return DASHBOARD_PREFIX


def _home_path() -> str:
    """Same-origin path to the app index (honors /dashboard prefix)."""
    return _app_base() + "/"


def _absolute_url(path: str) -> str:
    """Build an external URL: BASE_URL + DASHBOARD_PREFIX + path.

    Used for URLs delivered outside the browser request context:
    magic link emails, OAuth redirect URIs, Telegram notifications.
    `path` should start with '/'.
    """
    return f"{get_settings().base_url}{_app_base()}{path}"

# ── helpers ──────────────────────────────────────────────────────────────────


def _pname(p) -> str:
    """Human-readable project label: title if set, else slug name."""
    return p.title if p.title else p.name


def _slugify(text: str, max_len: int = 48) -> str:
    """Convert text to a URL-safe slug."""
    import re as _re
    slug = text.lower().strip()
    slug = _re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = _re.sub(r'[\s_]+', '-', slug)
    slug = _re.sub(r'-+', '-', slug).strip('-')
    return slug[:max_len] if slug else "project"


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


def _share_project_grant(request: Request) -> str | None:
    """If the session is a valid project-share session, return the granted project_id.

    Re-verifies the grant every call so expired/rotated-secret tokens stop
    working immediately instead of hanging on the 7-day session cookie.

    Supports two session shapes:
      - signed token: session carries `share_token` (a JWT-like string). We
        verify it against the current secret_key every call.
      - alias: session carries `share_alias`. We re-read the DB row every
        call so deleting or expiring the alias revokes live sessions.

    User-share links don't seat this state — they set user_id via /share/<ref>
    and the visitor becomes that user for the duration of the normal login
    session. Only project-share keeps a read-only grant in the session.
    """
    expected_kind = request.session.get("share_kind")
    expected_id = request.session.get("share_id")
    if expected_kind != "project" or not expected_id:
        return None

    alias = request.session.get("share_alias")
    if alias:
        with get_session(get_settings().db_path) as session:
            row = get_share_alias(session, alias)
            if (row and row.kind == "project" and row.ident == expected_id
                    and row.expires_at > datetime.utcnow()):
                return expected_id
        for k in ("share_alias", "share_kind", "share_id", "share_project_id"):
            request.session.pop(k, None)
        return None

    token = request.session.get("share_token")
    if not token:
        return None
    verified = verify_share_token(token, get_settings().secret_key)
    if verified != ("project", expected_id):
        for k in ("share_token", "share_kind", "share_id", "share_project_id"):
            request.session.pop(k, None)
        return None
    return expected_id


def _can_read_project(request: Request, project: Project) -> bool:
    """Read access: owner, admin, or a project-share grant for this exact project."""
    if _share_project_grant(request) == project.id:
        return True
    user = _get_current_user(request)
    return bool(user and _can_access_project(user, project))


def _project_dir(settings, user_id: str, project_id: str) -> Path:
    return settings.projects_root / user_id / project_id


async def _summarize_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF and summarize into a structured research idea via Claude."""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        raw_text = "\n".join(page.get_text() for page in doc).strip()
        doc.close()
    except Exception:
        return ""
    if not raw_text:
        return ""
    # Truncate raw text to ~30k chars for Claude context
    raw_text = raw_text[:30000]
    prompt = f"""You are a research assistant. Read the following academic paper text and produce a detailed, structured research idea summary. Include:

1. **Research Problem**: What problem does this paper address?
2. **Core Approach**: What is the proposed method/framework?
3. **Key Contributions**: List the main contributions (3-5 bullet points)
4. **Technical Details**: Important algorithms, architectures, or techniques
5. **Evaluation**: How is the work evaluated? What benchmarks/datasets?

Keep the summary detailed but concise (1500-2500 chars). Write in the same language as the paper.
Do NOT include paper metadata (authors, affiliations, page numbers).
Output ONLY the summary, no preamble.

---
{raw_text}"""
    try:
        import subprocess as sp
        result = sp.run(
            ["claude", "-p", prompt, "--no-session-persistence", "--output-format", "text",
             "--model", "claude-haiku-4-5"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:8000]
    except Exception:
        pass
    # Fallback: raw truncated text
    return raw_text[:8000]


def _write_config_yaml(project_dir: Path, project: Project, model: str = "claude-sonnet-4-6", compute_backend: dict = None):
    """Write config.yaml that ark orchestrator will read."""
    # Map webapp model value to orchestrator model backend.
    MODEL_MAP = {
        "claude-sonnet-4-6": ("claude", "claude-sonnet-4-6"),
        "claude-opus-4-7": ("claude", "claude-opus-4-7"),
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
        "max_dev_iterations": project.max_dev_iterations,
        "language": "en",
        "code_dir": str(project_dir),
        "latex_dir": "paper",
        "figures_dir": "paper/figures",
        "figure_generation": "nano_banana",
        "nano_banana_model": "pro",
        # Webapp projects are multi-tenant; do NOT auto-create a GitHub repo
        # under the host user's gh account for every new project. Git is still
        # initialized locally so writer-diff verification and commit history
        # work within the project directory.
        "auto_github_remote": False,
    }
    if compute_backend:
        config["compute_backend"] = compute_backend
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


def _build_cloud_config(user_obj, settings, per_project_overrides=None) -> dict | None:
    """Return compute_backend dict if cloud is configured, else None."""
    provider = settings.cloud_provider  # "" means disabled
    if not provider:
        return None
    keys = _get_user_keys(user_obj)
    # Validate that required credentials exist for this provider
    if provider == "aws" and not keys.get("aws_access_key_id"):
        return None
    if provider == "gcp" and not keys.get("gcp_service_account_json"):
        return None
    if provider == "azure" and not keys.get("azure_subscription_id"):
        return None

    owner_email = getattr(user_obj, "email", "") or ""
    cfg = {
        "type": "cloud",
        "provider": provider,
        "region": settings.cloud_region,
        "instance_type": settings.cloud_instance_type,
        "image_id": settings.cloud_image_id,
        "ssh_key_name": settings.cloud_ssh_key_name,
        "ssh_key_path": settings.cloud_ssh_key_path,
        "ssh_user": settings.cloud_ssh_user,
        "conda_env": settings.cloud_conda_env,
        "owner": owner_email,
    }
    if provider == "aws" and settings.cloud_security_group:
        cfg["security_group"] = settings.cloud_security_group
    if provider == "gcp":
        cfg["gcp_project"] = keys.get("gcp_project") or settings.cloud_gcp_project
        if settings.cloud_gcp_zone:
            cfg["region"] = settings.cloud_gcp_zone  # GCP uses zone, not region
    if provider == "azure":
        cfg["resource_group"] = settings.cloud_azure_resource_group
        cfg["location"] = settings.cloud_azure_location
    if per_project_overrides:
        cfg.update(per_project_overrides)
    return cfg


def _substitute_agent_templates(project_dir: Path, project_id: str, title: str,
                                 venue_name: str, venue_format: str, venue_pages: int):
    """Copy agent prompt templates into <project_dir>/agents/, substituting
    project-specific variables. Used by both create and restart flows.
    """
    agents_dir = project_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    templates_dir = Path(__file__).parent.parent.parent / "ark" / "templates" / "agents"
    if not templates_dir.exists():
        return
    for pf in templates_dir.glob("*.prompt"):
        content = pf.read_text()
        content = content.replace("{PROJECT_NAME}", project_id)
        content = content.replace("{PAPER_TITLE}", title or project_id)
        content = content.replace("{VENUE_NAME}", venue_name)
        content = content.replace("{VENUE_FORMAT}", venue_format or "neurips")
        content = content.replace("{VENUE_PAGES}", str(venue_pages))
        content = content.replace("{LATEX_DIR}", "paper")
        content = content.replace("{FIGURES_DIR}", "paper/figures")
        (agents_dir / pf.name).write_text(content)


def _clean_project_state(project_dir: Path):
    """Remove all generated state/results for a fresh restart.

    Preserves: config.yaml, uploaded.pdf, venue template files (.cls/.sty/.bst).
    If the caller wants to keep deep_research or figures across a restart, they
    must copy those out before calling this function and restore them after.
    """
    # Clean auto_research/state/
    state_dir = project_dir / "auto_research" / "state"
    if state_dir.exists():
        for f in state_dir.iterdir():
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

    # Clean agents/ so initializer re-specializes each prompt
    agents_dir = project_dir / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir, ignore_errors=True)

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


def _read_project_score(project_dir: Path, project=None) -> float:
    """Read score from DB (primary) or state files (fallback)."""
    if project and project.score:
        return float(project.score)
    # Fallback to YAML for legacy/unsynced projects
    state_dir = project_dir / "auto_research" / "state"
    ps = state_dir / "paper_state.yaml"
    if ps.exists():
        try:
            d = yaml.safe_load(ps.read_text()) or {}
            score = d.get("current_score")
            if score is not None:
                return float(score)
        except Exception:
            pass
    return 0.0


def _read_score_history(project_dir: Path, project=None) -> list[dict]:
    """Read score history from DB (primary) or paper_state.yaml (fallback)."""
    if project and project.score_history:
        try:
            import json
            return json.loads(project.score_history)
        except Exception:
            pass
    # Fallback to YAML
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


def _read_current_iteration(project_dir: Path, project=None) -> int:
    """Read current iteration from DB (primary) or paper_state.yaml (fallback)."""
    if project and project.iteration:
        return project.iteration
    # Fallback to YAML
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


def _read_phase_status(project_dir: Path, project) -> dict:
    """Read phase status from DB (primary) or YAML state files (fallback).

    Returns a dict with: phase, dev_iter, max_dev_iter, review_iter, max_review_iter
    """
    result = {
        "phase": "",
        "dev_iter": 0,
        "max_dev_iter": project.max_dev_iterations,
        "review_iter": 0,
        "max_review_iter": project.max_iterations,
    }

    # Try DB fields first
    if project.phase:
        result["phase"] = project.phase
        result["dev_iter"] = project.dev_iteration
        result["review_iter"] = project.iteration
        return result

    # Fallback to YAML for legacy/unsynced projects
    state_dir = project_dir / "auto_research" / "state"

    dev_state_file = state_dir / "dev_phase_state.yaml"
    if dev_state_file.exists():
        try:
            ds = yaml.safe_load(dev_state_file.read_text()) or {}
            result["dev_iter"] = int(ds.get("iteration", 0))
            dev_status = ds.get("status", "pending")
            if dev_status == "complete":
                result["phase"] = "review"
            elif dev_status == "in_progress":
                result["phase"] = "dev"
        except Exception:
            pass

    paper_state_file = state_dir / "paper_state.yaml"
    if paper_state_file.exists():
        try:
            ps = yaml.safe_load(paper_state_file.read_text()) or {}
            reviews = ps.get("reviews") or []
            if reviews:
                result["review_iter"] = int(reviews[-1].get("iteration", len(reviews)))
                result["phase"] = "review"
            paper_status = ps.get("status", "")
            if paper_status in ("accepted", "accepted_pending_cleanup"):
                result["phase"] = "accepted"
        except Exception:
            pass

    deep_research_file = state_dir / "deep_research.md"
    if deep_research_file.exists() and not result["phase"]:
        result["phase"] = "research"
    elif not result["phase"] and project.status == "running":
        result["phase"] = "initializing"

    return result


def _read_cost_report(project_dir: Path, project=None) -> dict:
    """Read cost report from DB (primary) + YAML per-agent details (secondary).

    DB has totals; YAML has per-agent breakdown. Returns a merged dict.
    """
    result = {}

    # DB has the totals — fast, no file I/O
    if project and project.total_cost_usd:
        result = {
            "total_cost_usd": project.total_cost_usd,
            "total_input_tokens": project.total_input_tokens,
            "total_output_tokens": project.total_output_tokens,
            "total_agent_calls": project.total_agent_calls,
        }

    # Per-agent breakdown still comes from YAML (too detailed for DB columns)
    p = project_dir / "auto_research" / "state" / "cost_report.yaml"
    if p.exists():
        try:
            d = yaml.safe_load(p.read_text()) or {}
        except Exception:
            d = {}
        if not result:
            # No DB data yet — use YAML for everything
            result = {
                "total_cost_usd": d.get("total_cost_usd", 0),
                "total_input_tokens": d.get("total_input_tokens", 0),
                "total_output_tokens": d.get("total_output_tokens", 0),
                "total_agent_calls": d.get("total_agent_calls", 0),
            }
        # Always merge per-agent and timing from YAML
        result["total_cache_read_tokens"] = d.get("total_cache_read_tokens", 0)
        result["total_cache_creation_tokens"] = d.get("total_cache_creation_tokens", 0)
        result["total_agent_seconds"] = d.get("total_agent_seconds", 0)
        result["per_agent"] = d.get("per_agent", {})
        result["generated_at"] = d.get("generated_at")

    return result


_TEMPLATE_TITLES = {"Paper Title", "Title Text", "Insert Title Here", ""}

def _read_paper_title(project_dir: Path) -> str:
    """Read paper title from paper/main.tex \\title{...}, fallback to config.yaml.

    Ignores template defaults. The config.yaml fallback covers the case where
    the title has been auto-generated but LaTeX hasn't been written yet (e.g.
    during the dev phase).
    """
    # Primary: LaTeX \title{}
    tex = project_dir / "paper" / "main.tex"
    if tex.exists():
        try:
            import re as _re
            text = tex.read_text(errors="replace")
            m = _re.search(r'\\(?:icmltitle|title)\{([^}]+)\}', text)
            if m:
                title = m.group(1).strip()
                if title not in _TEMPLATE_TITLES:
                    return title
        except Exception:
            pass
    # Fallback: config.yaml title (set by pipeline _update_title_from_idea)
    cfg = project_dir / "config.yaml"
    if cfg.exists():
        try:
            d = yaml.safe_load(cfg.read_text()) or {}
            title = (d.get("title") or "").strip()
            if title:
                return title
        except Exception:
            pass
    return ""


def _read_project_model(project_dir: Path, project=None) -> str:
    """Read model variant from DB (primary) or config.yaml (fallback)."""
    if project:
        if project.model_variant:
            return project.model_variant
        if project.model == "gemini":
            return "gemini"
        if project.model:
            return project.model
    # Fallback to config.yaml
    cfg = project_dir / "config.yaml"
    if cfg.exists():
        try:
            d = yaml.safe_load(cfg.read_text()) or {}
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
    """Find the generated paper PDF. Only returns main.pdf (not template/sample PDFs)."""
    main_pdf = project_dir / "paper" / "main.pdf"
    if main_pdf.exists() and main_pdf.stat().st_size > 10000:  # >10KB = real paper, not empty
        return main_pdf
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


async def _start_project_async(
    project_id: str,
    user_id: str,
    template_available: bool,
    is_admin: bool,
):
    """
    Background task: notify the user the project is initializing, then either
    transition to ``waiting_template`` or submit the pipeline job. The pipeline
    itself now owns conda env provisioning (Research Phase Step 0), so the
    webapp no longer blocks on cloning ``ark-base`` here.
    """
    settings = get_settings()
    pdir = _project_dir(settings, user_id, project_id)

    # Pull current Telegram credentials so we can notify on outcomes.
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project:
            return
        token = project.telegram_token
        chat_id = project.telegram_chat_id
        url = f"{settings.base_url}{_app_base()}/#project/{project_id}"

    send_telegram_notify(
        f"🛠️ <b>{_pname(project)}</b> initializing…",
        bot_token=token, chat_id=chat_id,
    )

    # User may have stopped/deleted in between. Don't override.
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project:
            return
        if project.status != "initializing":
            logger.info(f"Project {project_id} no longer initializing (now {project.status}); skipping submit")
            return

        if not template_available:
            update_project(session, project, status="waiting_template")
            send_telegram_notify(
                f"📦 <b>{_pname(project)}</b> waiting for a <b>{project.venue}</b> "
                f"LaTeX template (reply with a .zip link).",
                bot_token=token, chat_id=chat_id,
            )
            return

        try:
            final_status = _try_submit_or_pending(
                project, pdir, session, settings, is_admin=is_admin,
            )
        except Exception as e:
            logger.error(f"Submit failed for {project_id}: {e}")
            update_project(session, project, status="failed")
            send_telegram_notify(
                f"❌ <b>{_pname(project)}</b> submission failed: {e}",
                bot_token=token, chat_id=chat_id,
            )
            return

    send_telegram_notify(
        f"🔬 <b>{_pname(project)}</b> {final_status}\n"
        f"Venue: {project.venue} · {project.max_iterations} iter\n"
        f"<a href='{url}'>{url}</a>",
        bot_token=token, chat_id=chat_id,
    )


def _try_submit_or_pending(project, pdir, session, settings, is_admin=False) -> str:
    from sqlmodel import select as _sel
    active = session.exec(
        _sel(Project).where(Project.status.in_(["queued", "running"]))
        .where(Project.id != project.id)
    ).all()
    user_active = [p for p in active if p.user_id == project.user_id]
    if not is_admin and (
        len(user_active) >= MAX_CONCURRENT_PER_USER
        or len(active) >= MAX_CONCURRENT_GLOBAL
    ):
        update_project(session, project, status="pending")
        return "pending"
    
    # Fetch user keys
    user_obj = get_user(session, project.user_id)
    api_keys = _get_user_keys(user_obj) if user_obj else {}

    log_dir = pdir / "logs"
    log_dir.mkdir(exist_ok=True)
    
    if settings.cloud_provider:
        job_id = launch_cloud_job(project.id, project.mode, project.max_iterations,
                                  pdir, log_dir, settings, api_keys=api_keys)
        update_project(session, project, status="running", slurm_job_id=job_id)
        return "running"

    if slurm_available():
        job_id = submit_job(project.id, project.mode, project.max_iterations,
                            pdir, log_dir, settings, api_keys=api_keys)
        update_project(session, project, status="queued", slurm_job_id=job_id)
        return "queued"
    else:
        job_id = launch_local_job(project.id, project.mode, project.max_iterations,
                                  pdir, log_dir, settings, api_keys=api_keys)
        update_project(session, project, status="running", slurm_job_id=job_id)
        return "running"


# ── health probe ─────────────────────────────────────────────────────────────

@router.get("/health", include_in_schema=False)
async def health():
    """Liveness probe — no auth required. Used by Docker / k8s healthchecks."""
    return JSONResponse({"ok": True})


# ── pages ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse, name="index")
async def index(request: Request):
    # app_base = scope["root_path"], set to "/dashboard" by Starlette's
    # native Mount. Used by the Jinja template for APP_BASE injection.
    return _templates.TemplateResponse(
        request,
        "app.html",
        {
            "app_base": request.scope.get("root_path", ""),
            "share_mode": False,
            "share_project_id": "",
        },
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/share/{token}", name="share")
async def share_view(token: str, request: Request):
    """Public entry point for a share link.

    The `token` path segment is either:
      - a short alias registered in the ShareAlias table (e.g. "icml"), or
      - a full signed token produced by make_share_token / make_user_share_token.

    Alias lookup runs first because it's cheap and lets us revoke by deleting
    the row. If the segment isn't a known alias, fall back to signed-token
    verification so legacy long URLs keep working.

    Two kinds of share, regardless of how they were resolved:
      - "user"    → auto-login as that user. Full webapp access, identical to
                    what the user would see after Google/magic-link login. Hand
                    these out as anonymous reviewer accounts and control blast
                    radius via provider-side API spend caps.
      - "project" → seats a read-only grant scoped to one project. Reviewer sees
                    only that project's detail view; writes are blocked.

    CF Access must have a Bypass policy covering /dashboard/share/* so
    unauthenticated visitors reach this handler.
    """
    settings = get_settings()
    kind: str | None = None
    ident: str | None = None
    alias_name: str | None = None

    with get_session(settings.db_path) as session:
        row = get_share_alias(session, token)
        if row:
            if row.expires_at > datetime.utcnow():
                kind, ident, alias_name = row.kind, row.ident, row.alias
            # Expired alias falls through to the invalid-link response below.

    if kind is None:
        verified = verify_share_token(token, settings.secret_key)
        if verified:
            kind, ident = verified

    if kind is None or ident is None:
        return HTMLResponse(
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Link invalid</title>"
            "<style>body{font-family:sans-serif;display:flex;align-items:center;"
            "justify-content:center;min-height:100vh;margin:0;background:#f0fdfa}"
            ".card{background:#fff;border-radius:16px;padding:40px 48px;max-width:420px;"
            "box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center}"
            "h2{color:#991b1b;margin-bottom:12px}p{color:#555;line-height:1.6}</style></head>"
            "<body><div class='card'><h2>Share link invalid or expired</h2>"
            "<p>Ask the project owner for a fresh link.</p></div></body></html>",
            status_code=403,
        )

    if kind == "user":
        # Full auto-login. Clear any previous share-mode state, then seat
        # user_id the same way /auth/verify does after magic-link success.
        for k in ("share_token", "share_alias", "share_kind", "share_id", "share_project_id"):
            request.session.pop(k, None)
        with get_session(settings.db_path) as session:
            user = session.get(User, ident)
            if not user:
                return HTMLResponse(
                    "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
                    "<body style='font-family:sans-serif;padding:40px;text-align:center'>"
                    "<h2>User not found</h2><p>This share link references a user that no longer exists.</p>"
                    "</body></html>",
                    status_code=404,
                )
            request.session["user_id"] = user.id
        return RedirectResponse(_home_path())

    # kind == "project": read-only grant for one project only.
    # Session carries either the alias or the signed token so the grant can
    # be re-verified on every request (see _share_project_grant).
    if alias_name:
        request.session["share_alias"] = alias_name
        request.session.pop("share_token", None)
    else:
        request.session["share_token"] = token
        request.session.pop("share_alias", None)
    request.session["share_kind"] = kind
    request.session["share_id"] = ident
    request.session.pop("user_id", None)
    request.session.pop("share_project_id", None)
    return _templates.TemplateResponse(
        request,
        "app.html",
        {
            "app_base": request.scope.get("root_path", ""),
            "share_mode": True,
            "share_project_id": ident,
        },
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


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
    # Build absolute URL from BASE_URL + DASHBOARD_PREFIX. request.url_for
    # would produce http://localhost:9527/... (wrong host behind proxy).
    link = _absolute_url(f"/auth/verify?token={token}")

    print(f"\n  *** MAGIC LINK for {email} ***\n  {link}\n", flush=True)

    ok = send_magic_link_email(settings, email, link)
    if not ok:
        logger.warning(f"Email delivery failed — magic link printed to server console only")
    return JSONResponse({"ok": True})


@router.get("/auth/verify", name="auth_verify")
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
    return RedirectResponse(_home_path())


@router.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse(_home_path())


@router.get("/auth/google")
async def auth_google(request: Request):
    oauth = _get_google_oauth()
    if not oauth:
        raise HTTPException(400, "Google login is not configured on this server.")
    # Build OAuth redirect URI from BASE_URL + root_path. Must match what's
    # registered in Google Cloud Console. In prod this yields
    # https://idea2paper.org/dashboard/auth/google/callback.
    redirect_uri = _absolute_url("/auth/google/callback")
    return await oauth.google.authorize_redirect(
        request, redirect_uri, prompt="select_account"
    )


@router.get("/auth/google/callback", name="auth_google_callback")
async def auth_google_callback(request: Request):
    oauth = _get_google_oauth()
    if not oauth:
        raise HTTPException(400, "Google login is not configured on this server.")
    settings = get_settings()
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        logger.warning(f"Google OAuth error: {exc}")
        return RedirectResponse(_home_path() + "?google_error=1")

    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        return RedirectResponse(_home_path() + "?google_error=1")

    # Apply same allow-list checks as magic link
    denied = False
    if settings.allowed_emails:
        if email not in settings.allowed_emails:
            denied = True
    elif settings.email_domains:
        if email.split("@")[-1] not in settings.email_domains:
            denied = True

    if denied:
        _home = _home_path()
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
       <a href="mailto:contact@idea2paper.org">contact@idea2paper.org</a></p>
    <a class="back" href="{_home}">← Back to login</a>
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
    return RedirectResponse(_home_path())


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


# ── user settings & keys ──────────────────────────────────────────────────────

def _get_user_keys(user: User) -> dict:
    if not user.encrypted_keys:
        return {}
    try:
        return json.loads(decrypt_text(user.encrypted_keys, user.id))
    except Exception:
        return {}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


def _mask_json(val: str) -> str:
    if not val:
        return ""
    return "[JSON Config]"


@router.get("/api/user/settings")
async def api_get_user_settings(request: Request):
    user = _require_user(request)
    keys = _get_user_keys(user)
    return JSONResponse({
        "gemini": _mask_key(keys.get("gemini")),
        "anthropic": _mask_key(keys.get("anthropic")),
        "openai": _mask_key(keys.get("openai")),
        "claude_oauth_token": _mask_key(keys.get("claude_oauth_token")),
        "gemini_oauth_json": _mask_json(keys.get("gemini_oauth_json")),
        "aws_access_key_id": _mask_key(keys.get("aws_access_key_id")),
        "aws_secret_access_key": _mask_key(keys.get("aws_secret_access_key")),
        "aws_default_region": keys.get("aws_default_region") or "",
        "gcp_service_account_json": _mask_json(keys.get("gcp_service_account_json")),
        "gcp_project": keys.get("gcp_project") or "",
        "azure_subscription_id": _mask_key(keys.get("azure_subscription_id")),
        "azure_tenant_id": _mask_key(keys.get("azure_tenant_id")),
        "azure_client_id": _mask_key(keys.get("azure_client_id")),
        "azure_client_secret": _mask_key(keys.get("azure_client_secret")),
        "has_keys": any(keys.values()),
    })


@router.post("/api/user/settings")
async def api_save_user_settings(request: Request):
    user = _require_user(request)
    body = await request.json()
    
    # Keep a copy of old keys to revert if verification fails
    old_keys = _get_user_keys(user)
    current_keys = old_keys.copy()
    
    # Update keys based on body
    fields = [
        "gemini", "anthropic", "openai", "claude_oauth_token", "gemini_oauth_json",
        "aws_access_key_id", "aws_secret_access_key", "aws_default_region",
        "gcp_service_account_json", "gcp_project",
        "azure_subscription_id", "azure_tenant_id", "azure_client_id", "azure_client_secret"
    ]
    for field in fields:
        if field not in body:
            continue
        
        val = (body.get(field) or "").strip()
        
        if not val:
            # User explicitly cleared the field
            current_keys[field] = ""
            continue
        
        # For masked fields, only update if not a placeholder
        if field in ("gemini_oauth_json", "gcp_service_account_json"):
            if val != "[JSON Config]":
                current_keys[field] = val
        else:
            if "..." not in val:
                current_keys[field] = val


    # Run verification suite
    from website.dashboard.utils.verify import run_verification_suite
    settings = get_settings()

    # Mutual exclusion check: Anthropic API Key OR Claude CLI Session
    if current_keys.get("anthropic") and current_keys.get("claude_oauth_token"):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail="Conflict: You cannot provide both an Anthropic API Key and a Claude CLI Session token. Please clear one of them."
        )
    
    # run_verification_suite will verify the updated keys.

    # even if they weren't all updated in this request.
    verification_results = await asyncio.to_thread(run_verification_suite, user.id, settings.projects_root, current_keys)
    
    # Revert failed keys
    # 1. LLM API Keys
    for p in ["gemini", "anthropic", "openai"]:
        res = verification_results.get(p)
        if res and not res.get("ok"):
            # Verification failed, revert to old value
            current_keys[p] = old_keys.get(p, "")
        
    # 2. Claude CLI
    claude_res = verification_results.get("claude_token")
    if claude_res and not claude_res.get("ok"):
        # Verification failed, revert Claude token
        current_keys["claude_oauth_token"] = old_keys.get("claude_oauth_token", "")

    # 3. Gemini CLI (API key auth)
    gemini_cli_res = verification_results.get("gemini_cli")
    if gemini_cli_res and not gemini_cli_res.get("ok"):
        current_keys["gemini"] = old_keys.get("gemini", "")

    # 4. Gemini CLI (OAuth auth)
    gemini_oauth_res = verification_results.get("gemini_oauth")
    if gemini_oauth_res and not gemini_oauth_res.get("ok"):
        current_keys["gemini_oauth_json"] = old_keys.get("gemini_oauth_json", "")

    with get_session(settings.db_path) as session:
        db_user = get_user(session, user.id)
        if db_user:
            db_user.encrypted_keys = encrypt_text(json.dumps(current_keys), user.id)
            session.add(db_user)
            session.commit()

            
    return JSONResponse({
        "ok": True, 
        "verification": verification_results
    })



# Removing old interactive Claude auth logic as we switched to manual Headless Setup


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
            score = _read_project_score(pdir, project=p)
            pdf = _find_pdf(pdir)
            # Sync paper_title from LaTeX into DB title+name if it differs
            paper_title = _read_paper_title(pdir)
            if paper_title and paper_title != p.title:
                update_project(session, p, title=paper_title, name=paper_title)
            display_title = paper_title or p.title or "\u23f0 Pending: ARK will decide later"
            d = {
                "id": p.id,
                "name": p.name,
                "title": display_title,
                "idea": p.idea,
                "venue": p.venue,
                "mode": p.mode,
                "status": p.status,
                "score": score,
                "has_pdf": pdf is not None,
                "has_pdf_upload": bool(p.has_pdf_upload),
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
    max_iterations: int = Form(1),
    max_dev_iterations: int = Form(1),
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
        active = [p for p in user_projects if p.status in ("queued", "running", "initializing")]
        if not _is_admin(user) and len(active) >= MAX_CONCURRENT_PER_USER:
            raise HTTPException(
                400,
                f"You already have {len(active)} active projects. "
                f"Max {MAX_CONCURRENT_PER_USER} concurrent — wait for one to finish.",
            )
            
        # Check for configured keys
        db_user = get_user(_s, user.id)
        keys = _get_user_keys(db_user) if db_user else {}
        if not any(keys.values()):
            raise HTTPException(400, "Please configure at least one API key or link your Claude account in Settings first.")

    # Generate project ID: full UUID
    project_id = str(uuid.uuid4())

    # Title: keep as-is. If user didn't provide, it stays empty.
    # Dashboard will show "⏰ Pending" for empty titles.
    # Title will be auto-generated after deep research.

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
        # Extract text and summarize via Claude if no idea provided
        if not idea.strip():
            idea = await _summarize_pdf(pdf_bytes)

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
    _substitute_agent_templates(
        pdir, project_id, title,
        venue_name=venue or venue_format or "NeurIPS",
        venue_format=venue_format,
        venue_pages=venue_pages,
    )

    # New flow: every project starts in "initializing" while we clone its
    # per-project conda env in the background. The background task then
    # transitions it to either waiting_template or runs _try_submit_or_pending.
    initial_status = "initializing"

    # Map model to backend + variant for DB.
    MODEL_MAP = {
        "claude-sonnet-4-6": ("claude", "claude-sonnet-4-6"),
        "claude-opus-4-7": ("claude", "claude-opus-4-7"),
        "claude-opus-4-6": ("claude", "claude-opus-4-6"),
        "claude-haiku-4-5": ("claude", "claude-haiku-4-5"),
        "gemini": ("gemini", ""),
    }
    model_backend, model_variant = MODEL_MAP.get(model, ("claude", "claude-sonnet-4-6"))

    with get_session(settings.db_path) as session:
        project = create_project(
            session,
            id=project_id,
            user_id=user.id,
            name=title,
            title=title,
            idea=idea,
            venue=venue,
            venue_format=venue_format,
            venue_pages=venue_pages,
            max_iterations=max_iterations,
            max_dev_iterations=max_dev_iterations,
            mode=mode,
            status=initial_status,
            has_pdf_upload=has_pdf_upload,
            telegram_token=telegram_token,
            telegram_chat_id=telegram_chat_id,
            model=model_backend,
            model_variant=model_variant,
            code_dir=str(pdir),
            source="webapp",
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
        cloud_cfg = _build_cloud_config(db_user or user, settings)
        _write_config_yaml(pdir, project, model=model, compute_backend=cloud_cfg)

        if comment.strip():
            _write_user_update(pdir, comment.strip(), source="webapp_create")
            _write_user_instructions(pdir, comment.strip(), source="webapp_create")

    # Kick off submission in the background. The pipeline itself provisions
    # the per-project conda env in Research Phase Step 0, so the webapp just
    # transitions to queued/running/pending/waiting_template/failed.
    asyncio.create_task(_start_project_async(
        project_id=project_id,
        user_id=user.id,
        template_available=template_available,
        is_admin=_is_admin(user),
    ))

    return JSONResponse({
        "id": project_id,
        "name": title,
        "status": initial_status,
        "slurm_job_id": "",
    }, status_code=201)


@router.get("/api/projects/{project_id}")
async def api_get_project(project_id: str, request: Request):
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_read_project(request, project):
            raise HTTPException(404)
        pdir = _project_dir(settings, project.user_id, project_id)
        score = _read_project_score(pdir, project=project)
        pdf = _find_pdf(pdir)
        owner = session.get(User, project.user_id)
        env_ready = project_env_ready(pdir)
        if env_ready:
            conda_env_display = str(project_env_prefix(pdir))
        else:
            conda_env_display = settings.slurm_conda_env or ""
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
            "score_history": _read_score_history(pdir, project=project),
            "current_iteration": _read_current_iteration(pdir, project=project),
            "max_iterations": project.max_iterations,
            "phase_status": _read_phase_status(pdir, project),
            "has_pdf": pdf is not None,
            "has_pdf_upload": bool(project.has_pdf_upload),
            "slurm_job_id": project.slurm_job_id,
            "queue_position": _queue_position(project_id, session),
            "user_email": owner.email if owner else "",
            "model": _read_project_model(pdir, project=project),
            "telegram_token": project.telegram_token,
            "telegram_chat_id": project.telegram_chat_id,
            "has_deep_research": (pdir / "auto_research" / "state" / "deep_research.md").exists(),
            "environment": "ROCS Testbed" if project.slurm_job_id and not project.slurm_job_id.startswith(("local", "cloud")) else ("Cloud" if project.slurm_job_id and project.slurm_job_id.startswith("cloud") else "Local"),
            "conda_env": conda_env_display,
            "conda_env_ready": env_ready,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "cost_report": _read_cost_report(pdir, project=project),
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
            if project.slurm_job_id.startswith(("local:", "cloud:")):
                prefix = "local:" if project.slurm_job_id.startswith("local:") else "cloud:"
                pid_str = project.slurm_job_id[len(prefix):]
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
                  if p.status in ("queued", "running", "initializing") and p.id != project_id]
        if not _is_admin(user) and len(active) >= MAX_CONCURRENT_PER_USER:
            raise HTTPException(
                400,
                f"You already have {len(active)} active projects. "
                f"Max {MAX_CONCURRENT_PER_USER} concurrent.",
            )
        pdir = _project_dir(settings, project.user_id, project_id)

        # Update project fields from request body
        if body.get("idea"):
            project.idea = body["idea"]
            # Clear title so it gets auto-regenerated for the new idea
            project.title = ""
        if body.get("venue"):
            project.venue = body["venue"]
        if body.get("venue_format"):
            project.venue_format = body["venue_format"]
        if "venue_pages" in body:
            project.venue_pages = int(body["venue_pages"])
        if "max_iterations" in body:
            project.max_iterations = max(1, min(MAX_ITER_PER_START, int(body["max_iterations"])))
        if "telegram_token" in body:
            project.telegram_token = body["telegram_token"]
        if "telegram_chat_id" in body:
            project.telegram_chat_id = body["telegram_chat_id"]
        project.score = 0.0

        # Clean up project state for fresh restart.
        # Copy kept files into a backup dir, wipe everything, then restore.
        # The main pipeline then detects which artifacts still exist and skips
        # the corresponding steps — no per-flag branches downstream.
        redo_deep_research = body.get("redo_deep_research", False)
        keep_figures = body.get("keep_figures", False)
        backup_dir = pdir / ".ark-restart-backup"
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        backup_dir.mkdir(parents=True, exist_ok=True)

        state_dir = pdir / "auto_research" / "state"
        if not redo_deep_research and state_dir.exists():
            for f in state_dir.glob("deep_research*"):
                if f.is_file():
                    shutil.copy2(f, backup_dir / f.name)

        figures_src = pdir / "paper" / "figures"
        if keep_figures and figures_src.exists():
            shutil.copytree(figures_src, backup_dir / "figures", dirs_exist_ok=True)

        _clean_project_state(pdir)

        # Restore preserved artifacts from backup
        state_dir.mkdir(parents=True, exist_ok=True)
        for f in backup_dir.glob("deep_research*"):
            if f.is_file():
                shutil.move(str(f), state_dir / f.name)
        backup_figures = backup_dir / "figures"
        if backup_figures.exists():
            figures_src.mkdir(parents=True, exist_ok=True)
            for item in backup_figures.iterdir():
                dest = figures_src / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.move(str(item), dest)
        shutil.rmtree(backup_dir, ignore_errors=True)

        # Re-copy venue template (clean removed .bib/.tex template files)
        venue_fmt = body.get("venue_format") or project.venue_format or ""
        if venue_fmt and venue_fmt != "custom":
            paper_dir = pdir / "paper"
            paper_dir.mkdir(parents=True, exist_ok=True)
            copy_venue_template(venue_fmt, paper_dir)

        # Re-substitute agent prompt templates (clean removed agents/)
        _substitute_agent_templates(
            pdir, project.id, project.title,
            venue_name=project.venue or project.venue_format or "NeurIPS",
            venue_format=project.venue_format,
            venue_pages=project.venue_pages,
        )

        # Rewrite config.yaml with updated settings
        model = body.get("model", "claude-sonnet-4-6")
        cloud_cfg = _build_cloud_config(user, settings)
        _write_config_yaml(pdir, project, model=model, compute_backend=cloud_cfg)

        # Write instructions if provided
        comment = body.get("comment", "").strip()
        if comment:
            _write_user_instructions(pdir, comment, source="webapp_restart")

        session.add(project)
        session.commit()
        session.refresh(project)

        final_status = _try_submit_or_pending(project, pdir, session, settings, is_admin=_is_admin(user))
        send_telegram_notify(
            f"🔄 <b>{_pname(project)}</b> restarted ({final_status})",
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
            if project.slurm_job_id.startswith(("local:", "cloud:")):
                prefix = "local:" if project.slurm_job_id.startswith("local:") else "cloud:"
                pid_str = project.slurm_job_id[len(prefix):]
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
                  if p.status in ("queued", "running", "initializing") and p.id != project_id]
        if not _is_admin(user) and len(active) >= MAX_CONCURRENT_PER_USER:
            raise HTTPException(
                400,
                f"You already have {len(active)} active projects. "
                f"Max {MAX_CONCURRENT_PER_USER} concurrent.",
            )
        new_max = project.max_iterations + additional
        update_project(session, project, max_iterations=new_max)
        pdir = _project_dir(settings, project.user_id, project_id)
        # Use requested model, or fall back to existing
        model = body.get("model") or _read_project_model(pdir, project=project) or "claude-sonnet-4-6"
        cloud_cfg = _build_cloud_config(user, settings)
        _write_config_yaml(pdir, project, model=model, compute_backend=cloud_cfg)
        if comment:
            _write_user_update(pdir, comment, source="webapp_continue")
            _write_user_instructions(pdir, comment, source="webapp_continue")
        final_status = _try_submit_or_pending(project, pdir, session, settings, is_admin=_is_admin(user))
        return JSONResponse({"ok": True, "status": final_status, "max_iterations": new_max})


@router.get("/api/system/status")
async def api_system_status():
    """Public endpoint — returns webapp gate state and cloud info (no auth required)."""
    settings = get_settings()
    return JSONResponse({
        "disabled": _disabled_flag().exists(),
        "cloud": {
            "enabled": bool(settings.cloud_provider),
            "provider": settings.cloud_provider,
            "region": settings.cloud_region,
        }
    })


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
            _sel(Project).where(Project.status.in_(["queued", "running", "pending", "initializing"]))
        ).all()
        for p in active:
            if p.slurm_job_id:
                if p.slurm_job_id.startswith(("local:", "cloud:")):
                    prefix = "local:" if p.slurm_job_id.startswith("local:") else "cloud:"
                    pid_str = p.slurm_job_id[len(prefix):]
                    if pid_str.isdigit():
                        cancel_local_job(int(pid_str))
                else:
                    cancel_job(p.slurm_job_id)
            update_project(session, p, status="stopped")
            stopped.append(p.id)
    return JSONResponse({"stopped": stopped, "count": len(stopped)})


@router.get("/api/projects/{project_id}/pdf")
async def api_get_pdf(project_id: str, request: Request):
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_read_project(request, project):
            raise HTTPException(404)
        owner_id = project.user_id
    pdir = _project_dir(settings, owner_id, project_id)
    pdf = _find_pdf(pdir)
    if not pdf:
        raise HTTPException(404, "PDF not ready")
    return FileResponse(pdf, media_type="application/pdf",
                        filename=pdf.name, content_disposition_type="inline")


@router.get("/api/projects/{project_id}/uploaded-pdf")
async def api_get_uploaded_pdf(project_id: str, request: Request):
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_read_project(request, project):
            raise HTTPException(404)
        owner_id = project.user_id
    pdir = _project_dir(settings, owner_id, project_id)
    uploaded = pdir / "uploaded.pdf"
    if not uploaded.exists():
        raise HTTPException(404, "No uploaded PDF")
    return FileResponse(uploaded, media_type="application/pdf",
                        filename="uploaded.pdf", content_disposition_type="inline")


@router.get("/api/projects/{project_id}/zip")
async def api_download_zip(project_id: str, request: Request):
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_read_project(request, project):
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

        # sandbox_live/ — live-agent / firewall reproducibility bundle.
        # Include source, policy, scenarios, skill bodies, container definition.
        # Exclude: venvs, caches, slurm outputs, debug dumps, log spam.
        sandbox_dir = pdir / "sandbox_live"
        if sandbox_dir.exists():
            sandbox_exts = {".py", ".sh", ".co", ".jsonl", ".md",
                            ".yaml", ".toml", ".def", ".txt"}
            sandbox_skip_dirs = {"litellm_venv", "__pycache__",
                                 "cl_debug", "local_out"}
            for f in sandbox_dir.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(pdir)
                parts = set(rel.parts)
                if parts & sandbox_skip_dirs:
                    continue
                # slurm_out_* subdirectories (one per run) — exclude
                if any(p.startswith("slurm_out_") for p in rel.parts):
                    continue
                # slurm .out / .err at any depth, and bare .log files
                if f.name.startswith("slurm_") and f.suffix in {".out", ".err"}:
                    continue
                if f.suffix == ".log":
                    continue
                if f.suffix in sandbox_exts:
                    zf.write(f, rel)

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
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_read_project(request, project):
            raise HTTPException(404)
        owner_id = project.user_id
    pdir = _project_dir(settings, owner_id, project_id)
    log_dir = pdir / "logs"

    # Find the latest log file
    log_lines: list[str] = []
    log_file = ""
    for pattern in ["local_*.out", "slurm_*.out", "orchestrator.log", "*.log"]:
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
    settings = get_settings()

    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_read_project(request, project):
            raise HTTPException(404)
        owner_id = project.user_id

    pdir = _project_dir(settings, owner_id, project_id)
    log_dir = pdir / "logs"

    async def event_generator():
        # Track which log file we're tailing and how many lines we've sent
        # FROM THAT FILE. The client just fetched the last N lines via
        # /log?lines=N before opening this stream, so on the very first
        # iteration we skip everything that's already in the file (to avoid
        # double-rendering on the dashboard) and only emit what arrives
        # *after* the stream opened.
        current_file: Path | None = None
        sent_lines = 0
        first_iteration = True
        while True:
            if await request.is_disconnected():
                break

            # Find latest log file
            log_file = None
            for pattern in ["local_*.out", "slurm_*.out", "orchestrator.log", "*.log"]:
                matches = sorted(log_dir.glob(pattern),
                                 key=lambda p: p.stat().st_mtime if p.exists() else 0,
                                 reverse=True)
                if matches:
                    log_file = matches[0]
                    break

            if log_file and log_file.exists():
                try:
                    all_lines = log_file.read_text(errors="replace").splitlines()
                    if log_file != current_file:
                        # Either the very first iteration (skip past the
                        # client's initial /log fetch) or the orchestrator
                        # rotated to a new log file (e.g. env_provision.log
                        # → local_*.out). On a real rotation we DO want to
                        # send the whole new file, since the client never
                        # saw it; on first iteration we DON'T want to resend
                        # the catch-up.
                        if first_iteration:
                            sent_lines = len(all_lines)
                        else:
                            sent_lines = 0
                        current_file = log_file
                    new_lines = all_lines[sent_lines:]
                    for line in new_lines:
                        payload = json.dumps({"line": line})
                        yield f"data: {payload}\n\n"
                    sent_lines += len(new_lines)
                except Exception:
                    pass

            first_iteration = False

            # Also emit status (reads from DB — fast, no YAML parsing)
            with get_session(settings.db_path) as session:
                p = get_project(session, project_id)
                if p:
                    score = _read_project_score(pdir, project=p)
                    payload = json.dumps({
                        "status": p.status,
                        "score": score,
                        "cost_report": _read_cost_report(pdir, project=p),
                    })
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
        # Verified
        {"name": "ICML",       "format": "icml",     "pages": 9,  "year": 2025, "tag": "[Verified] (Default)"},
        {"name": "NeurIPS",    "format": "neurips",  "pages": 9,  "year": 2025, "tag": "[Verified]"},
        {"name": "EuroMLSys",  "format": "euromlsys",  "pages": 6, "year": 2025, "tag": "[Verified]"},
        # ML / AI
        {"name": "ICLR",       "format": "iclr",     "pages": 9,  "year": 2026},
        {"name": "ACL",        "format": "acl",      "pages": 8,  "year": 2025},
        {"name": "EMNLP",      "format": "emnlp",    "pages": 8,  "year": 2025},
        {"name": "CVPR",       "format": "cvpr",     "pages": 8,  "year": 2025},
        {"name": "MLSys",      "format": "mlsys",    "pages": 8,  "year": 2026},
        # Systems
        {"name": "SOSP",       "format": "sosp",     "pages": 14, "year": 2025},
        {"name": "EuroSys",    "format": "sosp",     "pages": 12, "year": 2026},
        {"name": "NSDI",       "format": "osdi",     "pages": 14, "year": 2025},
        {"name": "OSDI",       "format": "osdi",     "pages": 14, "year": 2025},
        {"name": "USENIX ATC", "format": "osdi",     "pages": 12, "year": 2026},
        {"name": "IEEE S&P",   "format": "neurips",  "pages": 13, "year": 2025},
        # Networking
        {"name": "INFOCOM",    "format": "infocom",  "pages": 9,  "year": 2025},
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
