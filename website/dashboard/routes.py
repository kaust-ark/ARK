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

MAX_PROJECTS_PER_USER = 5       # regular host cap (bounds disk: one workspace/conda env each)
MAX_PROJECTS_PER_ADMIN = 25
MAX_ITER_PER_START = 2  # queue fairness: bound per-start work (dev + review rounds)
# Two concurrency LANES so regular users and admins never block each other, and
# each lane drains as a simple FIFO queue:
#   • regular lane: at most 1 active per user, at most 3 active across all regulars
#   • admin  lane: at most 5 active across all admins (separate pool)
# Overflow → status "pending" (queued), promoted by app.py::_advance_pending_queue.
MAX_CONCURRENT_PER_USER = 1          # regular: one running/queued at a time
MAX_CONCURRENT_REGULAR_GLOBAL = 3    # regular lane global cap
MAX_CONCURRENT_ADMIN_GLOBAL = 5      # admin lane global cap
MAX_CONCURRENT_GLOBAL = MAX_CONCURRENT_REGULAR_GLOBAL  # back-compat alias (regular lane)
from ark.paths import get_ark_root as _get_ark_root
_DISABLED_FLAG = None  # lazy


def _disabled_flag() -> _Path:
    global _DISABLED_FLAG
    if _DISABLED_FLAG is None:
        _DISABLED_FLAG = _get_ark_root() / "ark_webapp" / "disabled"
    return _DISABLED_FLAG


# Maintenance banner: an admin-authored notice shown to all users (e.g. planned
# downtime). File-backed like the submission gate so it survives restarts, needs
# no DB migration, and can be cleared by hand in an emergency. Absent file (or
# empty message) ⇒ no banner. Independent of the submission gate — the banner
# is purely informational; disabling submissions is a separate control.
_MAINTENANCE_FILE = None  # lazy


def _maintenance_file() -> _Path:
    global _MAINTENANCE_FILE
    if _MAINTENANCE_FILE is None:
        _MAINTENANCE_FILE = _get_ark_root() / "ark_webapp" / "maintenance.json"
    return _MAINTENANCE_FILE


# Levels drive the banner color in the UI. "warning" is the default.
_MAINTENANCE_LEVELS = ("info", "warning", "critical")


def _read_maintenance() -> dict:
    """Return {message, level} for the active banner, or {} if none is set."""
    f = _maintenance_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text() or "{}")
    except Exception:
        return {}
    message = str(data.get("message", "")).strip()
    if not message:
        return {}
    level = data.get("level", "warning")
    if level not in _MAINTENANCE_LEVELS:
        level = "warning"
    return {"message": message, "level": level}


def _write_maintenance(message: str, level: str = "warning") -> dict:
    """Persist the banner. Empty/blank message clears it. Returns the new state."""
    f = _maintenance_file()
    message = (message or "").strip()
    if not message:
        f.unlink(missing_ok=True)
        return {}
    if level not in _MAINTENANCE_LEVELS:
        level = "warning"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"message": message, "level": level}))
    return {"message": message, "level": level}

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.requests import Request

# Lazy-initialized Google OAuth client
_google_oauth = None


def _get_google_oauth():
    """Return authlib OAuth client if Google credentials are configured."""
    global _google_oauth
    if _google_oauth is not None:
        return _google_oauth
    
    # Lazy import to avoid webapp-only dependency in CLI/orchestrator environments
    try:
        from authlib.integrations.starlette_client import OAuth as _OAuth
    except ImportError:
        logger.warning("authlib not installed, Google login unavailable")
        return None

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
from .config import get_settings
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
    touch_user_login,
    get_share_alias,
    get_user,
    update_project,
    enqueue_command,
    answer_decision,
    get_decision,
    get_open_decision,
    set_autonomy,
    add_message,
    list_messages,
    list_events,
    latest_artifact,
    get_state_doc,
    list_state_docs,
    list_access_requests,
    list_users,
    mark_access_declined,
)
from .crypto import encrypt_text, decrypt_text
from .skyworkspaces import render_sky_workspaces, render_aws_profiles
from . import sideband
from .jobs import (
    project_env_prefix,
    project_env_ready,
    slurm_available,
)
from ark.launcher import (
    LaunchSpec,
    LocalJobLauncher,
    SkyPilotVmJobLauncher,
    launcher_from_handle,
    select_launcher,
)
from ark.compute import VALID_ORCHESTRATOR_TYPES, VALID_EXPERIMENT_TYPES
from starlette.concurrency import run_in_threadpool
from . import compute_catalog
from .notify import send_completion_email, send_magic_link_email, send_telegram_login_link, send_telegram_notify, send_welcome_email, send_access_declined_email
from .auth import make_token, verify_token, verify_share_token
from .templates import copy_venue_template, has_venue_template, copy_test_fixtures, read_test_idea

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

    # Preprocess the template: strip venue placeholder title/author/abstract
    # and replace the instructional body with empty section stubs.  Emits
    # paper/template_manifest.yaml so the writer agent knows which sections
    # to populate and which files to preserve.
    try:
        from ark.template_preprocess import preprocess_custom_template
        preprocess_custom_template(paper_dir, venue_hint="custom")
    except Exception as e:
        logger.exception("Template preprocessing failed")
        return (
            f"Template preprocessing failed: {e}\n\n"
            f"Please check that main.tex is a valid LaTeX file and re-upload."
        )

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


def _require_model_key(keys: dict, model_variant: str) -> None:
    """Reject launches whose model's PROVIDER has no key configured.

    The basic gate only checks "has ANY key" — users kept picking a direct-
    vendor model (deepseek/…, anthropic/…) while holding a different provider's
    key, and the run died minutes later inside the orchestrator as a cryptic
    failed project ("no key found"; 4/6 real-user launches on 2026-07-05).
    Used by create AND restart/continue (a restart re-runs the stored model, so
    it dies the same way unless the owner added the matching key).
    """
    prov = (model_variant.split("/", 1)[0] if "/" in (model_variant or "") else "anthropic").lower()
    ok = bool(keys.get(prov)) or (
        prov == "anthropic" and keys.get("claude_oauth_token")) or (
        prov == "gemini" and keys.get("gemini_oauth_json")) or (
        # An OpenRouter key covers everything: _maybe_route_via_openrouter
        # rewrites native/long-tail models to openrouter/<slug> at launch.
        bool(keys.get("openrouter")))
    if not ok:
        nice = {"openrouter": "OpenRouter", "anthropic": "Anthropic",
                "openai": "OpenAI", "gemini": "Gemini"}.get(prov, prov.capitalize())
        raise HTTPException(
            400,
            f"This model runs on {nice}, but no {nice} API key is configured. "
            f"Add one in Settings → API Keys, or pick a model from a provider you already "
            f"have a key for (the OpenRouter row covers most models with a single key).")


def _admin_user_ids(session) -> set:
    """User IDs whose email is in the admin allowlist (for lane partitioning).

    The users table is tiny, so a full scan is cheap and avoids threading admin
    status through every project row.
    """
    from sqlmodel import select as _sel
    settings = get_settings()
    if not settings.admin_emails:
        return set()
    rows = session.exec(_sel(User.id, User.email)).all()
    return {uid for uid, email in rows if (email or "").lower() in settings.admin_emails}


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
        from ark.llm_lite import complete, utility_model
        out = complete(prompt, model=utility_model(), timeout=60)
        if out:
            return out[:8000]
    except Exception:
        pass
    # Fallback: raw truncated text
    return raw_text[:8000]


def _to_litellm_model(m: str) -> str:
    """Convert a Settings model value to a LiteLLM model string (provider/model).

    Already-LiteLLM strings (with '/') pass through; bare names are mapped by
    prefix so any provider works, not just the mainstream three.
    """
    m = (m or "").strip()
    if not m:
        return "anthropic/claude-sonnet-4-6"
    if "/" in m:
        return m
    if m in ("gemini", "gemini-auto"):
        return "gemini/gemini-2.5-flash"
    if m.startswith("claude"):
        return f"anthropic/{m}"
    if m.startswith(("gpt", "o1", "o3", "o4")):
        return f"openai/{m}"
    if m.startswith("gemini"):
        return f"gemini/{m}"
    # Unrecognized bare name (no provider prefix) — can't be routed by LiteLLM.
    # Fall back to the default rather than emit an unroutable model string.
    # Users on other providers (deepseek, xai, …) pass a full "provider/model"
    # string, which is handled by the '/' passthrough above.
    return "anthropic/claude-sonnet-4-6"


# OpenRouter proxies all major model families behind a single key. When a user
# has ONLY an OpenRouter key (no native anthropic/openai/gemini key) but picked a
# first-party model, rewrite the LiteLLM string to route through OpenRouter.
# LiteLLM's openrouter provider expects "openrouter/<openrouter-slug>"; the slug
# namespace uses "google/" for Gemini (not "gemini/"). The model-id portion must
# match an OpenRouter catalog slug — verify against https://openrouter.ai/models
# when adding models. Users who need an exact slug can also type a full
# "openrouter/…" string in the unverified-model field, which passes through.
_OPENROUTER_SLUG = {
    # Anthropic
    "anthropic/claude-opus-4-8": "anthropic/claude-opus-4.8",
    "anthropic/claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "anthropic/claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    # OpenAI
    "openai/gpt-5.5-pro": "openai/gpt-5.5-pro",
    "openai/gpt-5.5": "openai/gpt-5.5",
    "openai/gpt-5.4-mini": "openai/gpt-5.4-mini",
    # Gemini (OpenRouter namespaces Google models under "google/")
    "gemini/gemini-3.5-flash": "google/gemini-3.5-flash",
    "gemini/gemini-2.5-pro": "google/gemini-2.5-pro",
    "gemini/gemini-2.5-flash": "google/gemini-2.5-flash",
}

_OPENROUTER_NATIVE_KEY = {"anthropic": "anthropic", "openai": "openai", "gemini": "gemini"}


def _cheapest_model_for(keys: dict) -> str:
    """Pick the cheapest first-party model for whichever provider the user has a
    key for (used by the cheap test-project preset). Falls back to Haiku — which,
    if only an OpenRouter key is present, gets routed through OpenRouter at launch.
    """
    keys = keys or {}
    if keys.get("anthropic"):
        return "claude-haiku-4-5"
    if keys.get("openai"):
        return "gpt-5.4-mini"
    if keys.get("gemini"):
        return "gemini-2.5-flash"
    return "claude-haiku-4-5"


def _maybe_route_via_openrouter(model_str: str, keys: dict) -> str:
    """Graceful fallback: if a native vendor model was chosen but the user has no
    key for that vendor (only an OpenRouter key), rewrite ``provider/model`` to
    ``openrouter/<slug>`` so it still runs. Keys are parallel — a native key, when
    present, is always used directly; explicit ``openrouter/…`` strings (the
    OpenRouter model row) pass through untouched.
    """
    if not keys or "/" not in model_str:
        return model_str
    provider = model_str.split("/", 1)[0]
    if provider == "openrouter":
        return model_str
    if keys.get(provider):
        return model_str  # user has the direct key (mainstream or long-tail) — use it
    if keys.get("openrouter"):
        slug = _OPENROUTER_SLUG.get(model_str)
        if slug:
            return f"openrouter/{slug}"
        if provider not in _OPENROUTER_NATIVE_KEY:
            # Long-tail vendors (deepseek, xai, moonshot, …): OpenRouter slugs
            # keep the provider/model shape, so route generically instead of
            # dying with "no key found" (a real user hit this on 2026-07-05).
            return f"openrouter/{model_str}"
    return model_str


def _reject_unknown_backend(value: str, valid: frozenset, label: str) -> None:
    """400 on an unrecognized compute-backend type at the write path, before it is
    persisted. The launch path's ``validate_config`` is the frozenset gate; this
    brings the check forward to the API boundary so an unknown type never sits in
    the DB. Normalizes the compound ``skypilot:gcp`` selector to its base."""
    base = (value or "local").split(":", 1)[0]
    if base not in valid:
        raise HTTPException(400, f"Unknown {label} compute backend: {value!r}")


async def _validate_instance_type_or_400(backend: str, instance_type: str) -> str:
    """Validate a user-chosen orchestrator instance type against the selected
    cloud's SkyPilot catalog, BEFORE it is persisted / launched. Returns the
    cleaned instance type ("" when none was given, or when the backend isn't a
    cloud where the pick applies). Raises 400 when the type is definitively
    invalid. An "unable to verify" result (sky not installed / catalog error)
    is allowed through — the launch would surface any real problem."""
    instance_type = (instance_type or "").strip()
    if not instance_type:
        return ""
    base, _, cloud = (backend or "").partition(":")
    # Instance type only applies to a cloud (skypilot:<cloud>) orchestrator.
    if base != "skypilot" or cloud not in compute_catalog.SUPPORTED_CLOUDS:
        return ""
    result = await run_in_threadpool(compute_catalog.validate, cloud, instance_type)
    if result["valid"] is False:
        raise HTTPException(400, result["error"] or f"Invalid instance type: {instance_type!r}")
    return instance_type


def _write_config_yaml(project_dir: Path, project: Project, user_obj: User, settings, model: str = "claude-sonnet-4-6"):
    """Write config.yaml that ark orchestrator will read."""
    # All agents run through OpenHands; the orchestrator wants a LiteLLM string.
    model_str = _to_litellm_model(model)

    # Auto-push each project to a private GitHub repo only when the owner has
    # saved a GitHub PAT. The token itself is injected into the orchestrator
    # env (ARK_GITHUB_PAT) at launch time — never written into config.yaml.
    _owner_keys = _get_user_keys(user_obj) if user_obj else {}

    # If the user only has an OpenRouter key, route first-party models through it.
    model_str = _maybe_route_via_openrouter(model_str, _owner_keys)

    config = {
        "project": project.name,
        "title": project.title or project.name,
        "idea": project.idea,
        "venue": project.venue,
        "venue_format": project.venue_format,
        "venue_pages": project.venue_pages,
        "layout_mode": getattr(project, "layout_mode", "balanced") or "balanced",
        "mode": project.mode,
        "model": model_str,
        "model_variant": "",
        "max_iterations": project.max_iterations,
        "max_dev_iterations": project.max_dev_iterations,
        "language": "en",
        "code_dir": str(project_dir),
        "latex_dir": "paper",
        "figures_dir": "paper/figures",
        "figure_generation": getattr(project, "figure_generation", None) or "nano_banana",
        "nano_banana_model": "pro",
        "auto_github_remote": bool(_owner_keys.get("github_pat")),
    }

    def _resolve_compute_config(chosen: str, is_orchestrator: bool = False):
        if chosen == "slurm":
            return {
                "type": "slurm",
                "job_prefix": f"{project.name.upper()[:8]}_",
                "conda_env": settings.slurm_conda_env or "ark-base",
            }
        elif chosen.split(":", 1)[0] == "skypilot":
            if not is_orchestrator:
                # ── Layer-1 experiments (phased rollout) ──────────────────────
                # For now, a "SkyPilot" project runs its experiments LOCALLY on the
                # orchestrator's own SkyPilot VM — NOT a nested experiment cluster.
                # Rationale (SKYPILOT_PLAN §Multi-tenancy, Phase 1): keeping
                # experiments on the VM means the VM needs no `sky` SDK and no cloud
                # credentials of its own (nested clusters would require both, plus
                # their own autostop reaping), which is the tractable first step and
                # the cleanest BYOC story. Nested skypilot experiment clusters (for
                # GPUs / large parallel sweeps) are a later phase. `local` on the VM
                # runs in the per-project conda env cloned from ark-base (which the
                # orchestrator setup provisions).
                return {"type": "local", "conda_env": settings.cloud_conda_env or "ark-base"}

            # ── Layer-2 orchestrator ──────────────────────────────────────────
            # Shape the `type: skypilot` block. SkyPilot infers/optimizes anything
            # left unset; the cloud is optional (parsed from a "skypilot:{cloud}"
            # suffix). We only shape the minimal, known-good defaults below (a
            # pinned GCP machine type + baked image); everything else is left to
            # SkyPilot's optimizer. CLI users set skypilot resources explicitly in
            # config.yaml.
            #
            # The dashboard supports GCP and AWS, each with its own per-user
            # isolation: a central launcher provisions into the user's project
            # (GCP: cross-project SA grant + baked image) or account (AWS: cross-
            # account role trust, stock image). The UI's cloud selector submits a
            # "skypilot:{cloud}" suffix; a bare "skypilot" (single-cloud deployment
            # or a legacy caller) defaults to gcp here — else the GCP shaping below
            # (n4-standard-2, baked image) would be dead code and the optimizer would
            # pick an arbitrary machine type.
            cloud = chosen.split(":", 1)[1] if ":" in chosen else "gcp"
            cfg = {"type": "skypilot", "conda_env": settings.cloud_conda_env or "ark-base"}
            if cloud:
                cfg["cloud"] = cloud
            # User-chosen orchestrator instance/machine type (validated at the API
            # boundary before we ever get here). Empty = fall through to the cloud's
            # default shaping below. This is the machine the orchestrator VM (and,
            # in this phase, its local experiments) runs on.
            chosen_it = (getattr(project, "orchestrator_instance_type", "") or "").strip()
            # Pin a default GCP machine type so the orchestrator VM is predictable,
            # rather than left to SkyPilot's optimizer (which picked an arbitrary
            # n*-standard-2 in testing). Only for cloud == "gcp": n4-standard-2 is a
            # GCP-specific type and would be an invalid instance on AWS/Azure, so we
            # only pin it when GCP is the selected cloud (an explicit
            # "skypilot:{other}" suffix skips this). A CLI user's explicit
            # config.yaml bypasses this shaping entirely.
            if cloud == "gcp":
                # User pick wins; otherwise the historical known-good default.
                cfg["instance_type"] = chosen_it or "n4-standard-2"
                # Boot from the baked ARK image so the VM comes up with
                # texlive-full / openhands / node CLIs preinstalled; the setup:
                # block below then only re-runs the fast conda-specific steps
                # (its texlive/openhands guards short-circuit). SkyPilot needs a
                # FULL image path and does NOT resolve image families — it does
                # images().get(image=<last path segment>), so a `.../family/<f>`
                # URL 404s — hence we pin a specific image NAME. Bump the version
                # whenever scripts/build_ark_gcp_image.sh produces a newer image.
                #
                # The image lives ONCE in the CENTRAL project
                # (settings.cloud_gcp_project) — NOT replicated into each tenant's
                # project. SkyPilot resolves image_id against the project named in
                # the PATH (gcp.py::_get_image_size takes project = path segment[1],
                # not the launch project) using the launcher SA's ADC creds, and GCP
                # lets that SA boot a VM in the tenant's project from the cross-
                # project image because the SA is the caller on both ends
                # (roles/compute.admin on the central project ⇒ compute.images.
                # useReadOnly on the image; the user-granted roles ⇒ instance-create
                # in their project). So one golden image serves every tenant with no
                # per-project image copy. When no central project is configured we
                # OMIT image_id (boot stock public Debian); the setup_commands block
                # below then does the full — slower — install from scratch.
                central_project = settings.cloud_gcp_project
                if central_project:
                    cfg["image_id"] = (
                        f"projects/{central_project}/global/images/ark-debian-base-v7")
            elif cloud == "aws":
                # No baked AMI yet: boot a stock image and let the setup_commands
                # block below do the full install (its texlive/openhands guards keep
                # it idempotent). We do pin the region so the launch is predictable
                # — the user's configured region, else the operator default; SkyPilot
                # optimizes the instance type. Bake an AMI later for fast boots.
                _aws_keys = _get_user_keys(user_obj) if user_obj else {}
                region = (_aws_keys.get("aws_region") or "").strip() or settings.cloud_aws_region
                if region:
                    cfg["region"] = region
                # No AWS default machine type — SkyPilot's optimizer picks one
                # unless the user explicitly chose an instance type.
                if chosen_it:
                    cfg["instance_type"] = chosen_it
            # When the user has their OWN project/account configured for the chosen
            # cloud, launch into it via their per-user SkyPilot workspace (the
            # central launcher has cross-project/cross-account access). The launcher
            # selects this workspace per launch (ark/compute/_sky.py::active_workspace).
            # Without per-user config we omit it and fall back to the host's default.
            if user_obj:
                _keys = _get_user_keys(user_obj)
                _has_cloud = (cloud == "gcp" and _keys.get("gcp_project")) or \
                             (cloud == "aws" and _keys.get("aws_account_id"))
                if _has_cloud:
                    from website.dashboard.skyworkspaces import workspace_name_for
                    cfg["workspace"] = workspace_name_for(user_obj.id)
            # The orchestrator cluster comes up bare, so its deps must be installed
            # via the setup: block (the run command is plain `python -m
            # ark.orchestrator`). Install the synced ARK source (workdir →
            # ~/sky_workdir) with the research extra, then the agent runtime:
            # `openhands` is a separate uv-managed CLI, NOT a pip dep of ark, so
            # without it the orchestrator exits the moment it tries to run an agent
            # (ark/pipeline.py fails fast on a missing `openhands` binary). uv
            # installs both itself and the tool into ~/.local/bin, which the run
            # command puts on PATH. PR4 replaces this with a baked orchestrator image.
            cfg["setup_commands"] = [
                "cd ~/sky_workdir && pip install -e '.[research]'",
                # Guarded + idempotent: a re-launch reconnects to the same cluster
                # and re-runs setup, so skip the (slow) toolchain download when
                # `openhands` is already runnable. Test *runnability* (`openhands
                # --version`), NOT mere presence (`command -v`): the baked image
                # installs openhands as root under /opt/uv + /usr/local/bin, which
                # SkyPilot's `gcpuser` can't exec (EACCES). A presence-only guard
                # sees that broken system copy on PATH and skips the reinstall, so
                # every agent call then dies with "[Errno 13] Permission denied:
                # 'openhands'". Running it fails on the un-execable copy, so we
                # reinstall a user-owned one into ~/.local/bin (prepended to PATH,
                # shadowing the system copy); --force overwrites any partial install.
                "export PATH=\"$HOME/.local/bin:$PATH\"; "
                "openhands --version >/dev/null 2>&1 || "
                "{ curl -LsSf https://astral.sh/uv/install.sh | sh && "
                "uv tool install --force --python 3.12 openhands; }",
                # Paper mode compiles the PDF with pdflatex + bibtex, which the base
                # VM image lacks. Match ARK's own recommendation
                # (latex_utils.detect_latex_install_command → texlive-full on apt);
                # heavy (~GBs) but avoids missing-package compile failures mid-run.
                # Guarded so a re-launch skips the reinstall.
                "command -v pdflatex >/dev/null 2>&1 || "
                "{ sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive "
                "apt-get install -y texlive-full; }",
                # Experiments run locally on this VM (see the Layer-1 branch above)
                # in a per-project conda env cloned from `ark-base`
                # (pipeline._ensure_project_env → conda create --clone ark-base). A
                # fresh VM has no such env, so create it from the repo's
                # environment.yml (synced to ~/sky_workdir via workdir; `name:
                # ark-base`). Guarded/idempotent so a re-launch skips the slow solve.
                "conda env list | grep -qw ark-base || "
                "conda env create -f ~/sky_workdir/environment.yml",
            ]
            return cfg
        return {"type": "local"}

    orch_chosen = project.orchestrator_compute_backend or "local"
    config["orchestrator_compute_backend"] = _resolve_compute_config(orch_chosen, is_orchestrator=True)
    
    exp_chosen = project.experiment_compute_backend
    # If experiment_compute_backend is the legacy default "slurm" but compute_backend
    # is explicitly set to skypilot/something else, prefer compute_backend.
    if exp_chosen == "slurm" and project.compute_backend and project.compute_backend != "slurm":
        exp_chosen = project.compute_backend
    exp_chosen = exp_chosen or project.compute_backend or "slurm"
    config["experiment_compute_backend"] = _resolve_compute_config(exp_chosen, is_orchestrator=False)
    config["compute_backend"] = config["experiment_compute_backend"]  # Legacy
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
    # Deep Research toggle (webapp) — when set, the orchestrator skips the
    # Gemini literature survey (pipeline handles a missing deep_research.md).
    config["skip_deep_research"] = bool(getattr(project, "skip_deep_research", False))
    config_path = project_dir / "config.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))


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

    # Custom-template notes: pulled from paper/template_manifest.yaml if the
    # preprocessor emitted one.  For non-custom projects this is the empty
    # string so the placeholder doesn't leak through.
    try:
        from ark.template_preprocess import render_custom_template_notes
        custom_notes = render_custom_template_notes(project_dir / "paper")
    except Exception:
        custom_notes = ""

    for pf in templates_dir.glob("*.prompt"):
        content = pf.read_text()
        content = content.replace("{PROJECT_NAME}", project_id)
        content = content.replace("{PAPER_TITLE}", title or project_id)
        content = content.replace("{VENUE_NAME}", venue_name)
        content = content.replace("{VENUE_FORMAT}", venue_format or "neurips")
        content = content.replace("{VENUE_PAGES}", str(venue_pages))
        content = content.replace("{LATEX_DIR}", "paper")
        content = content.replace("{FIGURES_DIR}", "paper/figures")
        content = content.replace("{CUSTOM_TEMPLATE_NOTES}", custom_notes)
        (agents_dir / pf.name).write_text(content)


def _clean_project_state(project_dir: Path):
    """Remove all generated state/results for a fresh restart.

    Preserves: config.yaml, uploaded.pdf, venue template files (.cls/.sty/.bst).
    If the caller wants to keep deep_research or figures across a restart, they
    must copy those out before calling this function and restore them after.
    """
    # Clean auto_research/state/ — but keep user-supplied inputs. These
    # are not generated state; treating them as such silently loses the
    # instructions the user typed at project creation when they restart
    # without re-typing them.
    state_dir = project_dir / "auto_research" / "state"
    _state_keep = {"user_instructions.yaml"}
    if state_dir.exists():
        for f in state_dir.iterdir():
            if f.is_file() and f.name not in _state_keep:
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

    # Clean paper/ — keep venue template files, remove generated content.
    # When a user-uploaded template was preprocessed (template_manifest.yaml
    # present), also keep .tex/.bib/manifest: those are the uploaded skeleton,
    # not generated content. api_restart_project re-runs preprocess afterwards
    # to strip any writer-filled prose back to clean stubs. Without this,
    # restart on a custom-template project leaves paper/ with only .sty files
    # and the next run fails to find main.tex.
    paper_dir = project_dir / "paper"
    if paper_dir.exists():
        custom_template = (paper_dir / "template_manifest.yaml").exists()
        keep_exts = {".cls", ".sty", ".bst"}
        if custom_template:
            keep_exts |= {".tex", ".bib"}
        for f in paper_dir.iterdir():
            if f.is_dir():
                if f.name == "figures":
                    shutil.rmtree(f, ignore_errors=True)
                    f.mkdir(exist_ok=True)
            elif f.name == "template_manifest.yaml":
                continue
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


def _projected_state(project, name: str) -> dict:
    """Load a projected state document (paper_state, dev_phase_state, …) from the
    control-plane DB — replaces the legacy read of the orchestrator's on-disk
    YAML (Phase 3, ADR-0013). Returns {} if absent."""
    if not project:
        return {}
    try:
        with get_session(get_settings().db_path) as s:
            return get_state_doc(s, project.id, name) or {}
    except Exception:
        return {}


def _read_project_score(project_dir: Path, project=None) -> float:
    """Read score from DB columns (primary) or the projected state doc (fallback)."""
    if project and project.score:
        return float(project.score)
    # Fallback to the projected paper_state for legacy/unsynced projects.
    d = _projected_state(project, "paper_state")
    score = d.get("current_score")
    if score is not None:
        try:
            return float(score)
        except (TypeError, ValueError):
            pass
    return 0.0


def _read_score_history(project_dir: Path, project=None) -> list[dict]:
    """Read score history from DB columns (primary) or projected state (fallback)."""
    if project and project.score_history:
        try:
            import json
            return json.loads(project.score_history)
        except Exception:
            pass
    d = _projected_state(project, "paper_state")
    reviews = d.get("reviews", [])
    try:
        return [
            {"iteration": r.get("iteration", i + 1), "score": float(r.get("score", 0))}
            for i, r in enumerate(reviews)
            if r and r.get("score") is not None
        ]
    except Exception:
        return []


def _read_current_iteration(project_dir: Path, project=None) -> int:
    """Read current iteration from DB columns (primary) or projected state (fallback)."""
    if project and project.iteration:
        return project.iteration
    d = _projected_state(project, "paper_state")
    reviews = d.get("reviews") or []
    try:
        if reviews:
            return int(reviews[-1].get("iteration", len(reviews)))
    except Exception:
        pass
    return 0


def _read_phase_status(project_dir: Path, project) -> dict:
    """Read phase status from DB columns (primary) or the projected state docs
    (fallback — dev_phase_state / paper_state via the DB projection, ADR-0013).

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
        # Clamp iter to max for display. Historical projects whose pipeline
        # ran past the cumulative cap (pre-c077e15 bug) still have
        # project.iteration > project.max_iterations persisted; rendering
        # "11/8" reads as a UI bug to the user. The cap fix already
        # prevents new runs from overshooting — this is cosmetic for
        # already-saved overshot state.
        result["dev_iter"] = min(project.dev_iteration, project.max_dev_iterations)
        result["review_iter"] = min(project.iteration, project.max_iterations)
        return result

    # Fallback to the projected state docs for legacy/unsynced projects.
    ds = _projected_state(project, "dev_phase_state")
    if ds:
        try:
            result["dev_iter"] = int(ds.get("iteration", 0))
            dev_status = ds.get("status", "pending")
            if dev_status == "complete":
                result["phase"] = "review"
            elif dev_status == "in_progress":
                result["phase"] = "dev"
        except Exception:
            pass

    ps = _projected_state(project, "paper_state")
    if ps:
        try:
            reviews = ps.get("reviews") or []
            if reviews:
                result["review_iter"] = int(reviews[-1].get("iteration", len(reviews)))
                result["phase"] = "review"
            paper_status = ps.get("status", "")
            if paper_status in ("accepted", "accepted_pending_cleanup"):
                result["phase"] = "accepted"
        except Exception:
            pass

    state_dir = project_dir / "auto_research" / "state"
    deep_research_file = state_dir / "deep_research.md"
    if deep_research_file.exists() and not result["phase"]:
        result["phase"] = "research"
    elif not result["phase"] and project.status == "running":
        result["phase"] = "initializing"

    return result


def _read_cost_report(project_dir: Path, project=None) -> dict:
    """Read cost report from YAML (primary) + DB totals as fallback.

    YAML is the ground truth written atomically after every agent call.
    DB totals can lag (e.g. after a restart or transient write failure), so
    when YAML exists we always prefer its numbers to keep the headline total
    consistent with the per-agent breakdown.
    """
    result = {}

    # YAML is authoritative — always read it first
    p = project_dir / "auto_research" / "state" / "cost_report.yaml"
    if p.exists():
        try:
            d = yaml.safe_load(p.read_text()) or {}
        except Exception:
            d = {}
        if d:
            result = {
                "total_cost_usd": d.get("total_cost_usd", 0),
                "total_input_tokens": d.get("total_input_tokens", 0),
                "total_output_tokens": d.get("total_output_tokens", 0),
                "total_agent_calls": d.get("total_agent_calls", 0),
                "total_cache_read_tokens": d.get("total_cache_read_tokens", 0),
                "total_cache_creation_tokens": d.get("total_cache_creation_tokens", 0),
                "total_agent_seconds": d.get("total_agent_seconds", 0),
                "per_agent": d.get("per_agent", {}),
                "generated_at": d.get("generated_at"),
            }

    # Fall back to DB totals only when YAML has no data yet
    if not result and project and project.total_cost_usd:
        result = {
            "total_cost_usd": project.total_cost_usd,
            "total_input_tokens": project.total_input_tokens,
            "total_output_tokens": project.total_output_tokens,
            "total_agent_calls": project.total_agent_calls,
        }

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


def _artifact_store_for(pdir: Path):
    """Build the artifact store for a project from its config.yaml (Phase 3,
    ADR-0012). Falls back to a local store rooted at the project dir."""
    try:
        from ark.artifacts import from_config as _afc
        cfg = {}
        cfg_file = pdir / "config.yaml"
        if cfg_file.exists():
            cfg = yaml.safe_load(cfg_file.read_text()) or {}
        return _afc(cfg, pdir)
    except Exception:
        from ark.artifacts import LocalArtifactStore
        return LocalArtifactStore(pdir)


def _serve_registered_artifact(pdir: Path, ref: Optional[dict], *,
                               filename: str, inline: bool, min_size: int = 0):
    """Resolve a registered Artifact reference to a response, or None to fall
    back to reading the project dir. A store ``url()`` (presigned, future) yields
    a redirect; a local file is served with ``FileResponse`` (Range + fd cleanup);
    anything else is streamed with the handle closed on completion (ADR-0012).

    ``min_size`` skips (returns None) an artifact smaller than the threshold, so a
    tiny/broken PDF falls through to the disk path's "not ready" 404 — parity with
    ``_find_pdf``'s >10KB check."""
    if not ref:
        return None
    # An artifact registered with a known-too-small size isn't a real paper.
    size = int(ref.get("size") or 0)
    if min_size and size and size <= min_size:
        return None
    try:
        from ark.artifacts import ArtifactRef
        store = _artifact_store_for(pdir)
        aref = ArtifactRef.from_dict(ref)
        signed = store.url(aref)
        if signed:
            return RedirectResponse(signed)
        media = ref.get("content_type") or "application/octet-stream"
        disposition = "inline" if inline else "attachment"
        local = store.fspath(aref)
        if local and os.path.exists(local):
            return FileResponse(local, media_type=media, filename=filename,
                                content_disposition_type=disposition)

        # Object store (no local path): stream, closing the handle when done.
        fh = store.open(aref)

        def _iterfile():
            try:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                fh.close()

        return StreamingResponse(
            _iterfile(), media_type=media,
            headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
        )
    except Exception:
        return None


def _latest_artifact_ref(session, project_id: str, kind: str) -> Optional[dict]:
    """The latest registered artifact of ``kind`` as a plain dict, or None."""
    row = latest_artifact(session, project_id, kind)
    if not row:
        return None
    return {"store_type": row.store_type, "key": row.key,
            "content_type": row.content_type, "size": row.size, "sha256": row.sha256}


def _check_webapp_enabled():
    if _disabled_flag().exists():
        raise HTTPException(503, "Webapp submissions are currently disabled by admin.")


def _queue_position(project_id: str, session) -> int:
    """1-based position within the project's OWN lane (regular vs admin).

    Regular and admin queues drain independently, so a regular user's position
    should only count other regular pending projects ahead of them.
    """
    from sqlmodel import select as _sel
    project = get_project(session, project_id)
    if not project or project.status != "pending":
        return 0
    admin_ids = _admin_user_ids(session)
    owner_is_admin = project.user_id in admin_ids
    ahead = session.exec(
        _sel(Project).where(Project.status == "pending")
        .where(Project.created_at < project.created_at)
    ).all()
    same_lane = [p for p in ahead if (p.user_id in admin_ids) == owner_is_admin]
    return len(same_lane) + 1


_MEDIAN_RUN_CACHE: dict = {"ts": 0.0, "hours": 0.0}


def _run_start_ts(pdir: Path) -> Optional[int]:
    """Unix time the project's latest run started: the newest logs/local_<ts>.out
    file name IS the launch timestamp (see submit_job's log naming)."""
    try:
        logs = sorted((pdir / "logs").glob("local_*.out"))
        if logs:
            return int(logs[-1].stem.split("_", 1)[1])
    except (ValueError, OSError):
        pass
    return None


def _median_run_hours(session, settings) -> float:
    """Median wall-clock hours of recent finished runs (launch-log name ts →
    log mtime). DB created_at→updated_at is unusable here: title syncs and
    later restarts inflate it by weeks. Cached 10 min; fallback 18h (typical
    full run observed in production)."""
    import time as _time
    if _MEDIAN_RUN_CACHE["hours"] and _time.time() - _MEDIAN_RUN_CACHE["ts"] < 600:
        return _MEDIAN_RUN_CACHE["hours"]
    from sqlmodel import select as _sel
    done = session.exec(
        _sel(Project).where(Project.status == "done")
        .order_by(Project.updated_at.desc()).limit(20)
    ).all()
    durations = []
    for p in done:
        pdir = _project_dir(settings, p.user_id, p.id)
        start = _run_start_ts(pdir)
        if start is None:
            continue
        try:
            logs = sorted((pdir / "logs").glob("local_*.out"))
            hours = (logs[-1].stat().st_mtime - start) / 3600.0
        except OSError:
            continue
        if 0.5 <= hours <= 48:  # drop cheap-test blips and stale-mtime junk
            durations.append(hours)
    hours = sorted(durations)[len(durations) // 2] if durations else 18.0
    _MEDIAN_RUN_CACHE.update(ts=_time.time(), hours=hours)
    return hours


def _queue_eta_end(session, settings, project) -> Optional[str]:
    """Rough ISO-UTC estimate of when a PENDING project will FINISH: wait for a
    lane slot (remaining time of active same-lane runs, FIFO by queue position)
    plus one median run. Per-user-cap nuances are ignored — this is a banner
    estimate, not a promise."""
    if project.status != "pending":
        return None
    import time as _time
    from datetime import datetime as _dt, timedelta as _td
    from sqlmodel import select as _sel
    med_h = _median_run_hours(session, settings)
    pos = _queue_position(project.id, session)
    admin_ids = _admin_user_ids(session)
    owner_is_admin = project.user_id in admin_ids
    active = session.exec(
        _sel(Project).where(Project.status.in_(["running", "initializing", "queued"]))
    ).all()
    lane = [p for p in active if (p.user_id in admin_ids) == owner_is_admin]
    now = _time.time()
    remaining = []
    for p in lane:
        start = _run_start_ts(_project_dir(settings, p.user_id, p.id))
        elapsed_h = (now - start) / 3600.0 if start else 0.0
        # Runs already past the median are unpredictable — floor their
        # remaining time at a quarter median rather than "almost done".
        remaining.append(max(med_h - elapsed_h, med_h * 0.25))
    remaining.sort()
    if remaining:
        k = pos - 1
        wait_h = remaining[k % len(remaining)] + (k // len(remaining)) * med_h
    else:
        wait_h = 0.0
    eta = _dt.utcnow() + _td(hours=wait_h + med_h)
    return eta.isoformat() + "Z"


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

        # Capture what we need before the session closes
        pname = _pname(project)
        venue = project.venue
        max_iter = project.max_iterations

    def _blocking_submit():
        with get_session(settings.db_path) as session:
            project = get_project(session, project_id)
            if not project:
                return None, None
            try:
                return _try_submit_or_pending(project, pdir, session, settings, is_admin=is_admin), None
            except Exception as e:
                logger.error(f"Submit failed for {project_id}: {e}", exc_info=True)
                update_project(session, project, status="failed", error_message=str(e))
                return None, str(e)

    final_status, err = await asyncio.to_thread(_blocking_submit)
    if err:
        send_telegram_notify(
            f"❌ <b>{pname}</b> submission failed: {err}",
            bot_token=token, chat_id=chat_id,
        )
        return
    if final_status:
        send_telegram_notify(
            f"🔬 <b>{pname}</b> {final_status}\n"
            f"Venue: {venue} · {max_iter} iter\n"
            f"<a href='{url}'>{url}</a>",
            bot_token=token, chat_id=chat_id,
        )


async def _restart_project_async(
    project_id: str,
    pdir: Path,
    is_admin: bool,
    token: str,
    chat_id: str,
    apply_instruction: str = "",
    apply_scope: str = "edit",
    chat_message: str = "",
):
    """Background task for restart/continue: offloads blocking GCP provisioning
    to a thread so the event loop stays free to serve other requests."""
    settings = get_settings()

    def _blocking_submit():
        with get_session(settings.db_path) as session:
            project = get_project(session, project_id)
            if not project:
                return None, None, None
            try:
                final_status = _try_submit_or_pending(project, pdir, session, settings, is_admin=is_admin,
                                                      apply_instruction=apply_instruction, apply_scope=apply_scope,
                                                      chat_message=chat_message)
                return final_status, _pname(project), None
            except Exception as e:
                logger.error(f"Restart failed for {project_id}: {e}", exc_info=True)
                update_project(session, project, status="failed", error_message=str(e))
                return None, _pname(project), str(e)

    final_status, pname, err = await asyncio.to_thread(_blocking_submit)
    if pname is None:
        return
    if err:
        send_telegram_notify(
            f"❌ <b>{pname}</b> restart failed: {err}",
            bot_token=token, chat_id=chat_id,
        )
        return
    send_telegram_notify(
        f"🔄 <b>{pname}</b> restarted ({final_status})",
        bot_token=token, chat_id=chat_id,
    )


def orchestrator_launcher_for(project, spec, session, settings):
    """Resolve the JobLauncher for ``project``'s configured orchestrator backend,
    populating ``spec.config`` for the skypilot path.

    The single launch-dispatch point, shared by initial submission
    (``_try_submit_or_pending``) and queue/template promotion so the paths can't
    drift (previously the queue path ignored the backend and forced slurm/local).
    Raises ValueError on an unrecognized backend type (callers surface it as a
    failed launch) rather than silently running an unknown backend locally."""
    backend = project.orchestrator_compute_backend or "local"
    base = backend.split(":", 1)[0]
    if base not in ("local", "slurm", "skypilot"):
        raise ValueError(f"Unknown orchestrator backend: {backend!r}")

    if base == "skypilot":
        # The skypilot orchestrator config was shaped into config.yaml by
        # _resolve_compute_config; the launcher reads its cluster/resources from
        # there — the config block is self-contained (folded Phases 5+6, ADR-0010).
        import yaml
        with open(Path(spec.project_dir) / "config.yaml") as f:
            spec.config = yaml.safe_load(f)
        return SkyPilotVmJobLauncher(log_fn=logger.info)

    return select_launcher(backend, slurm_ok=slurm_available())


def _try_submit_or_pending(project, pdir, session, settings, is_admin=False,
                           apply_instruction: str = "", apply_scope: str = "edit",
                           chat_message: str = "") -> str:
    from sqlmodel import select as _sel
    active = session.exec(
        _sel(Project).where(Project.status.in_(["queued", "running"]))
        .where(Project.id != project.id)
    ).all()
    # Lane-aware admission: regular users and admins are separate pools so one
    # can't starve the other. Overflow queues as "pending".
    admin_ids = _admin_user_ids(session)
    owner_is_admin = project.user_id in admin_ids
    if owner_is_admin:
        admin_active = [p for p in active if p.user_id in admin_ids]
        if len(admin_active) >= MAX_CONCURRENT_ADMIN_GLOBAL:
            update_project(session, project, status="pending")
            return "pending"
    else:
        regular_active = [p for p in active if p.user_id not in admin_ids]
        user_active = [p for p in regular_active if p.user_id == project.user_id]
        if (len(user_active) >= MAX_CONCURRENT_PER_USER
                or len(regular_active) >= MAX_CONCURRENT_REGULAR_GLOBAL):
            update_project(session, project, status="pending")
            return "pending"
    
    # Fetch user keys
    user_obj = get_user(session, project.user_id)
    api_keys = _get_user_keys(user_obj) if user_obj else {}

    log_dir = pdir / "logs"
    log_dir.mkdir(exist_ok=True)
    
    # Phase 4: dispatch is unified behind the JobLauncher seam via
    # orchestrator_launcher_for. The spec mirrors the old per-launcher arg lists;
    # apply_instruction / chat_message are only honoured by the local launcher.
    spec = LaunchSpec(
        project_id=project.id, mode=project.mode,
        max_iterations=project.max_iterations,
        project_dir=pdir, log_dir=log_dir, settings=settings, api_keys=api_keys,
        apply_instruction=apply_instruction, apply_scope=apply_scope,
        chat_message=chat_message,
    )
    try:
        launcher = orchestrator_launcher_for(project, spec, session, settings)
        job_id = launcher.launch(spec)
    except Exception as e:
        logger.error(f"Failed to launch orchestrator for {project.id}: {e}", exc_info=True)
        update_project(session, project, status="failed",
                       error_message=f"Launch failed: {str(e)[:400]}")
        return "failed"
    update_project(session, project, status=launcher.initial_status, slurm_job_id=job_id)
    return launcher.initial_status


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
    from website.dashboard.gcp_access import (
        launcher_sa_email, launcher_org_customer_id, REQUIRED_ROLES)
    return _templates.TemplateResponse(
        request,
        "app.html",
        {
            "app_base": request.scope.get("root_path", ""),
            "share_mode": False,
            "share_project_id": "",
            "launcher_sa": launcher_sa_email(get_settings()),
            "launcher_customer_id": launcher_org_customer_id(get_settings()),
            "required_roles": REQUIRED_ROLES,
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
    from website.dashboard.gcp_access import (
        launcher_sa_email, launcher_org_customer_id, REQUIRED_ROLES)
    return _templates.TemplateResponse(
        request,
        "app.html",
        {
            "app_base": request.scope.get("root_path", ""),
            "share_mode": True,
            "share_project_id": ident,
            "launcher_sa": launcher_sa_email(get_settings()),
            "launcher_customer_id": launcher_org_customer_id(get_settings()),
            "required_roles": REQUIRED_ROLES,
        },
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ── auth ──────────────────────────────────────────────────────────────────────

# Magic-link abuse guard. With CF Access removed from the public dashboard,
# /auth/send-link is an internet-reachable email sender. Sliding-window limits:
# per-IP (bots), per-target-email (mailbox bombing from many IPs), and a global
# hourly backstop. In-process state is fine — the webapp is a single process.
_SENDLINK_BY_IP: dict[str, list] = {}
_SENDLINK_BY_EMAIL: dict[str, list] = {}
_SENDLINK_GLOBAL: list = []
_SENDLINK_IP_LIMIT = (5, 900)         # 5 links / 15 min per IP
_SENDLINK_EMAIL_LIMIT = (3, 900)      # 3 links / 15 min per target inbox
_SENDLINK_GLOBAL_LIMIT = (120, 3600)  # site-wide safety valve


def _rate_ok(bucket: list, limit: tuple[int, int]) -> bool:
    import time as _time
    n, window = limit
    now = _time.time()
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= n:
        return False
    bucket.append(now)
    return True


@router.post("/auth/send-link")
async def auth_send_link(request: Request):
    settings = get_settings()
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    from .request_access import _client_ip
    ip = _client_ip(request)
    if not _rate_ok(_SENDLINK_BY_IP.setdefault(ip, []), _SENDLINK_IP_LIMIT):
        raise HTTPException(429, "Too many login links requested — try again in a few minutes.")
    if not _rate_ok(_SENDLINK_BY_EMAIL.setdefault(email, []), _SENDLINK_EMAIL_LIMIT):
        raise HTTPException(429, "Too many login links for this address — check your inbox or try later.")
    if not _rate_ok(_SENDLINK_GLOBAL, _SENDLINK_GLOBAL_LIMIT):
        logger.warning(f"send-link GLOBAL rate limit hit (requested by {ip})")
        raise HTTPException(429, "Login is briefly rate-limited — please try again in a few minutes.")

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
        touch_user_login(session, user)  # "who accessed" — record this login
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
  <title>Access Denied — Idea2Paper</title>
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
    <p>Your Google account (<strong>{email}</strong>) is not authorized to access Idea2Paper.</p>
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
        touch_user_login(session, user)  # "who accessed" — record this login
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
        # Drives the first-run onboarding nudge (configure an API key).
        "has_keys": any(_get_user_keys(user).values()),
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


@router.get("/api/user/settings")
async def api_get_user_settings(request: Request):
    user = _require_user(request)
    keys = _get_user_keys(user)
    settings = get_settings()
    # Keys with dedicated fields below (or retired legacy cloud creds we keep
    # suppressed) — anything NOT here surfaces via the generic passthrough loop.
    _STD_KEY_FIELDS = {
        "gemini", "anthropic", "openai", "openrouter",
        "claude_oauth_token", "gemini_oauth_json",
        "github_pat", "github_org",
        "gcp_project", "gcp_conda_env",
        # SkyPilot AWS: account the launcher assumes into + its region.
        "aws_account_id", "aws_region",
        # Retired native-cloud creds — no longer surfaced, but suppressed so any
        # value left in an old vault doesn't show up as a stray settings row.
        "aws_access_key_id", "aws_secret_access_key", "aws_default_region",
        "gcp_service_account_json", "gcp_zone", "gcp_instance_type",
        "gcp_image_family", "gcp_image_project", "gcp_ssh_user",
        "gcp_network", "gcp_subnet", "gcp_ssh_private_key", "azure_subscription_id",
        "azure_tenant_id", "azure_client_id", "azure_client_secret",
    }
    _resp = {
        "anthropic": _mask_key(keys.get("anthropic")),
        "openai": _mask_key(keys.get("openai")),
        "gemini": _mask_key(keys.get("gemini")),
        "openrouter": _mask_key(keys.get("openrouter")),
        "github_pat": _mask_key(keys.get("github_pat")),
        "github_org": keys.get("github_org") or "",
        # GCP project the SkyPilot launcher provisions into (per-user isolation).
        "gcp_project": keys.get("gcp_project") or "",
        "gcp_conda_env": keys.get("gcp_conda_env") or settings.cloud_conda_env or "ark-base",
        # AWS account the SkyPilot launcher assumes into (per-user isolation) + region.
        "aws_account_id": keys.get("aws_account_id") or "",
        "aws_region": keys.get("aws_region") or settings.cloud_aws_region or "",
        "has_keys": any(keys.values()),
        # Telegram is a user-level account setting (managed in Settings).
        "telegram_token": user.telegram_token or "",
        "telegram_chat_id": user.telegram_chat_id or "",
    }
    # Surface any other-provider keys (deepseek, xai, …) so the UI can list them.
    for _k, _v in keys.items():
        if _k not in _STD_KEY_FIELDS and _v:
            _resp[_k] = _mask_key(_v)
    return JSONResponse(_resp)


@router.post("/api/user/settings")
async def api_save_user_settings(request: Request):
    user = _require_user(request)
    body = await request.json()
    
    # Keep a copy of old keys to revert if verification fails
    old_keys = _get_user_keys(user)
    current_keys = old_keys.copy()
    
    # Update keys based on body. `gcp_project` / `gcp_conda_env` drive the
    # SkyPilot per-user launch (the launcher provisions into the user's project
    # via an IAM grant — no service-account key is stored).
    fields = [
        "gemini", "anthropic", "openai", "github_pat", "github_org",
        "gcp_project", "gcp_conda_env",
        "aws_account_id", "aws_region",
    ]
    for field in fields:
        if field not in body:
            continue

        val = (body.get(field) or "").strip()

        if not val:
            current_keys[field] = ""
            continue

        # Plain text / API key fields: skip masked placeholders
        if "..." not in val:
            current_keys[field] = val

    # Other (unverified / long-tail) providers — any extra <provider> key in the
    # body that isn't a standard field. Stored verbatim into encrypted_keys so
    # any OpenHands/LiteLLM provider works (deepseek, xai, mistral, groq, …).
    # Telegram lives on User columns, not in encrypted_keys — exclude it from the
    # long-tail provider sweep so it isn't stored as a bogus provider key.
    _reserved = set(fields) | {"verification", "telegram_token", "telegram_chat_id"}
    for field, raw in body.items():
        if not isinstance(field, str) or field in _reserved:
            continue
        v = (raw or "").strip() if isinstance(raw, str) else ""
        if not v:
            current_keys[field] = ""
        elif "..." not in v:
            current_keys[field] = v


    # Run verification suite
    from website.dashboard.utils.verify import run_verification_suite
    settings = get_settings()

    # Verify the three first-class providers (anthropic/openai/gemini); other
    # providers are stored without verification (they're flagged unverified).
    verification_results = await asyncio.to_thread(run_verification_suite, user.id, settings.projects_root, current_keys)

    # Revert any key whose verification failed (first-class + other providers).
    for p, res in verification_results.items():
        if res and not res.get("ok"):
            current_keys[p] = old_keys.get(p, "")

    with get_session(settings.db_path) as session:
        db_user = get_user(session, user.id)
        if db_user:
            db_user.encrypted_keys = encrypt_text(json.dumps(current_keys), user.id)
            # Telegram is a user-level account setting. Honor whatever is sent —
            # including empty strings — so the user can clear it here and stop
            # receiving notifications on future projects.
            if "telegram_token" in body:
                db_user.telegram_token = (body.get("telegram_token") or "").strip()
            if "telegram_chat_id" in body:
                db_user.telegram_chat_id = (body.get("telegram_chat_id") or "").strip()
            session.add(db_user)
            session.commit()

    # The user may have just added/changed their GCP project id or AWS account;
    # re-render the SkyPilot workspaces (and the AWS ~/.aws profiles) so their next
    # launch targets the right project/account. Runs off the request thread and is
    # best-effort (never fails the save).
    await asyncio.to_thread(render_sky_workspaces, settings.db_path)
    await asyncio.to_thread(render_aws_profiles, settings.db_path)

    return JSONResponse({
        "ok": True,
        "verification": verification_results
    })


@router.get("/api/user/gcp/info")
async def api_gcp_grant_info(request: Request):
    """Return the grant target (launcher SA email + required roles) without
    probing the user's project. The settings UI calls this on open so it can
    render the step-by-step grant instructions before the user has verified —
    the verify probe (a real GCP call) stays reserved for the Verify button."""
    _require_user(request)
    settings = get_settings()
    from website.dashboard.gcp_access import (
        launcher_sa_email, launcher_org_customer_id, REQUIRED_ROLES)
    return JSONResponse({
        "launcher_sa": launcher_sa_email(settings),
        "launcher_customer_id": launcher_org_customer_id(settings),
        "required_roles": REQUIRED_ROLES,
    })


@router.post("/api/user/gcp/verify")
async def api_verify_gcp_access(request: Request):
    """Verify the launcher SA can provision into the user's GCP project — i.e.
    that they've completed the IAM grant. Probes ``project_id`` from the body,
    falling back to the saved key. Returns ``{ok, detail, launcher_sa,
    required_roles}`` so the settings UI can both show the verdict and render the
    grant instructions (SA email + roles) from one call."""
    user = _require_user(request)
    settings = get_settings()
    from website.dashboard.gcp_access import (
        verify_project_access, launcher_sa_email, launcher_org_customer_id,
        REQUIRED_ROLES)

    body = await request.json()
    project_id = (body.get("gcp_project") or "").strip()
    if not project_id:
        project_id = (_get_user_keys(user).get("gcp_project") or "").strip()

    result = await asyncio.to_thread(verify_project_access, project_id)
    return JSONResponse({
        **result,
        "launcher_sa": launcher_sa_email(settings),
        "launcher_customer_id": launcher_org_customer_id(settings),
        "required_roles": REQUIRED_ROLES,
    })


@router.get("/api/user/aws/info")
async def api_aws_grant_info(request: Request):
    """Return the AWS grant target (launcher identity ARN + required policies +
    tenant role name + optional external id) without probing the user's account.
    The AWS analog of /api/user/gcp/info: the settings UI calls this on open so it
    can render the role-create + trust-policy instructions before the user has
    verified — the verify probe (a real STS AssumeRole) stays behind Verify."""
    _require_user(request)
    settings = get_settings()
    from website.dashboard.aws_access import (
        launcher_role_arn, launcher_external_id, REQUIRED_POLICIES, TENANT_ROLE_NAME)
    return JSONResponse({
        "launcher_role_arn": await asyncio.to_thread(launcher_role_arn, settings),
        "launcher_external_id": launcher_external_id(settings),
        "required_policies": REQUIRED_POLICIES,
        "tenant_role_name": TENANT_ROLE_NAME,
    })


@router.post("/api/user/aws/verify")
async def api_verify_aws_access(request: Request):
    """Verify the launcher can assume the user's ark-launcher role — i.e. that
    they've created it with a trust policy naming our identity. Probes
    ``aws_account_id`` from the body, falling back to the saved value. Returns
    ``{ok, detail, launcher_role_arn, required_policies, tenant_role_name,
    launcher_external_id}`` so the settings UI can both show the verdict and
    render the grant instructions from one call."""
    user = _require_user(request)
    settings = get_settings()
    from website.dashboard.aws_access import (
        verify_account_access, launcher_role_arn, launcher_external_id,
        REQUIRED_POLICIES, TENANT_ROLE_NAME)

    body = await request.json()
    account_id = (body.get("aws_account_id") or "").strip()
    if not account_id:
        account_id = (_get_user_keys(user).get("aws_account_id") or "").strip()

    result = await asyncio.to_thread(verify_account_access, account_id, settings)
    return JSONResponse({
        **result,
        "launcher_role_arn": await asyncio.to_thread(launcher_role_arn, settings),
        "launcher_external_id": launcher_external_id(settings),
        "required_policies": REQUIRED_POLICIES,
        "tenant_role_name": TENANT_ROLE_NAME,
    })


# Removing old interactive Claude auth logic as we switched to manual Headless Setup


# ── compute (instance type) API ───────────────────────────────────────────────

@router.get("/api/compute/instance-types")
async def api_compute_instance_types(request: Request, cloud: str = ""):
    """Curated instance-type shortlist for a cloud, for the launch dropdown.
    Returns ``{cloud, default, options:[{value,label,vcpus,mem_gb,gpu}]}``. The
    UI also lets users type any type — validated via the endpoint below."""
    _require_user(request)
    cloud = (cloud or "").strip().lower()
    return JSONResponse({
        "cloud": cloud,
        "default": compute_catalog.default_instance_type(cloud) or "",
        "options": compute_catalog.curated_options(cloud),
    })


@router.get("/api/compute/instance-types/validate")
async def api_compute_validate_instance_type(request: Request, cloud: str = "", instance_type: str = ""):
    """Validate a single instance type against a cloud's SkyPilot catalog, for
    live feedback as the user types a custom type. Returns
    ``{valid, vcpus, mem_gb, error}``; ``valid`` is null when we can't check
    (SkyPilot not installed / catalog error) so the UI can show "unverified"
    rather than a hard error. The catalog call is blocking, so run it off-loop."""
    _require_user(request)
    result = await run_in_threadpool(
        compute_catalog.validate, (cloud or "").strip().lower(), (instance_type or "").strip())
    return JSONResponse(result)


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
            display_title = paper_title or p.title or "\u23f0 Pending: Idea2Paper will decide later"
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
                "compute_backend": p.compute_backend,
                "user_email": user_email_cache.get(p.user_id, ""),
                "error_message": p.error_message or "",
            }
            if p.status == "pending":
                d["queue_position"] = _queue_position(p.id, session)
                d["queue_eta_end"] = _queue_eta_end(session, settings, p)
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
    layout_mode: str = Form("balanced"),
    mode: str = Form("paper"),
    max_iterations: int = Form(2),
    max_dev_iterations: int = Form(1),
    pdf_file: Optional[UploadFile] = File(None),
    template_zip: Optional[UploadFile] = File(None),
    model: str = Form("claude-sonnet-4-6"),
    telegram_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    comment: str = Form(""),
    compute_backend: str = Form("local"),
    orchestrator_compute_backend: str = Form("local"),
    orchestrator_instance_type: str = Form(""),
    preset: str = Form(""),
    skip_deep_research: str = Form(""),
    skip_ai_figures: str = Form(""),
):
    user = _require_user(request)
    _check_webapp_enabled()

    max_iterations = max(1, min(max_iterations, MAX_ITER_PER_START))
    max_dev_iterations = max(1, min(max_dev_iterations, MAX_ITER_PER_START))
    settings = get_settings()
    with get_session(settings.db_path) as _s:
        user_projects = get_projects_for_user(_s, user.id)
        project_cap = MAX_PROJECTS_PER_ADMIN if _is_admin(user) else MAX_PROJECTS_PER_USER
        if len(user_projects) >= project_cap:
            raise HTTPException(400, f"Max {project_cap} projects per user.")
        # NOTE: no hard concurrent-reject here anymore. Extra submissions are
        # accepted and QUEUED (status "pending") by _try_submit_or_pending, then
        # promoted FIFO within the user's lane by _advance_pending_queue. The
        # host cap above (5 regular / 25 admin) is what bounds total footprint.

        # Check for configured keys
        db_user = get_user(_s, user.id)
        keys = _get_user_keys(db_user) if db_user else {}
        if not any(keys.values()):
            raise HTTPException(400, "Please add at least one API key in Settings first (e.g. OpenRouter — one key unlocks every model).")

    # Cheap test-project preset (admin-only): a 2-page EuroMLSys paper with a
    # trivial, self-contained sklearn experiment, NO live Deep Research (a canned
    # report is seeded into the project below), NO AI figures (matplotlib only),
    # the cheapest available model, and a single iteration. Lets us exercise the
    # full pipeline end-to-end for a few cents.
    figure_generation = "nano_banana"
    # "Skip AI Figures" works for every template now: disable the AI concept-figure
    # phase (matplotlib data figures still run). The Test preset re-enables it below
    # because it seeds a canned concept figure instead.
    if skip_ai_figures and preset != "test":
        figure_generation = "none"
    if preset == "test":
        if not _is_admin(user):
            raise HTTPException(403, "The Test template is admin-only.")
        venue, venue_format, venue_pages = "ACM SIGPLAN", "euromlsys", 2
        layout_mode, mode = "balanced", "paper"
        max_iterations, max_dev_iterations = 1, 1
        # Defaults to a full run (real Deep Research + real AI figures). The two
        # "Skip …" boxes seed canned fixtures instead (see copy_test_fixtures
        # below): skip_ai_figures pre-seeds a concept figure so the figure phase
        # reuses it (Phase-0 skip, zero PaperBanana cost) — so keep nano_banana.
        figure_generation = "nano_banana"
        compute_backend = orchestrator_compute_backend = "local"
        # Respect the model the user actually picked in the dropdown (e.g.
        # MiniMax); only fall back to the cheapest available when they left the
        # default (Sonnet). The model picker is visible for the Test venue, so a
        # chosen model should win — Test's cheapness comes from 2 pages / 1
        # iteration / the Skip boxes, not from forcing a model.
        if not model or model == "claude-sonnet-4-6":
            model = _cheapest_model_for(keys)
        idea = read_test_idea() or idea
        if not title.strip():
            # Plain ASCII — the title flows into the paper's \title{}, and a raw
            # emoji (🧪) makes pdflatex fail with "Unicode character not set up".
            title = "Test run"

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

    # Test template: each "Skip …" box seeds a canned fixture so that step is
    # reused instead of run. Skip Deep Research → canned deep_research.md (Step 2
    # skips the live call); Skip AI Figures → canned concept figure + manifest
    # (the figure phase reuses it, zero PaperBanana cost). Neither box → full run.
    if preset == "test" and (skip_deep_research or skip_ai_figures):
        copy_test_fixtures(
            pdir,
            seed_deep_research=bool(skip_deep_research),
            seed_figures=bool(skip_ai_figures),
        )

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
        # Anthropic
        "claude-opus-4-8": ("anthropic", "claude-opus-4-8"),
        "claude-sonnet-4-6": ("anthropic", "claude-sonnet-4-6"),
        "claude-haiku-4-5": ("anthropic", "claude-haiku-4-5"),
        # OpenAI
        "gpt-5.5-pro": ("openai", "gpt-5.5-pro"),
        "gpt-5.5": ("openai", "gpt-5.5"),
        "gpt-5.4-mini": ("openai", "gpt-5.4-mini"),
        # Gemini (agent-verified after the FinishAction parse fix)
        "gemini-3.5-flash": ("gemini", "gemini-3.5-flash"),
        "gemini-2.5-pro": ("gemini", "gemini-2.5-pro"),
        "gemini-2.5-flash": ("gemini", "gemini-2.5-flash"),
    }
    if model in MODEL_MAP:
        model_backend, model_variant = MODEL_MAP[model]
    else:
        # Unverified / custom "provider/model" string (incl. openrouter/<slug>):
        # keep it as-is so the DB-stored variant survives continue/restart
        # instead of silently falling back to Sonnet.
        model_variant = _to_litellm_model(model)
        model_backend = model_variant.split("/", 1)[0] or "anthropic"

    # Model↔key match guard (fail fast; see _require_model_key).
    _require_model_key(keys, model_variant)

    # Page fitting strictness: relaxed (no adjustment) | balanced (within ~1 page,
    # default) | strict (exact). Back-compat: old 'off' == new 'relaxed'.
    if layout_mode == "off":
        layout_mode = "relaxed"
    if layout_mode not in ("relaxed", "balanced", "strict"):
        layout_mode = "balanced"

    # Orchestration mode: only the paper pipeline exists. `mode` is rendered
    # into the SLURM submit script (slurm_template.sh: `--mode {{ mode }}`), so
    # an unvalidated value is a shell-injection vector. Pin to the allowlist.
    if mode not in ("paper",):
        mode = "paper"

    with get_session(settings.db_path) as session:
        # Telegram is now a user-level account setting (managed in Settings). New
        # projects inherit the user's current Telegram credentials; if the user
        # cleared them, the project gets none and stays silent.
        if not telegram_token and not telegram_chat_id:
            _du = get_user(session, user.id)
            if _du:
                telegram_token = _du.telegram_token or ""
                telegram_chat_id = _du.telegram_chat_id or ""

        _reject_unknown_backend(orchestrator_compute_backend, VALID_ORCHESTRATOR_TYPES, "orchestrator")
        _reject_unknown_backend(compute_backend, VALID_EXPERIMENT_TYPES, "experiment")
        orchestrator_instance_type = await _validate_instance_type_or_400(
            orchestrator_compute_backend, orchestrator_instance_type)
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
            layout_mode=layout_mode,
            figure_generation=figure_generation,
            skip_deep_research=bool(skip_deep_research),
            max_iterations=max_iterations,
            max_dev_iterations=max_dev_iterations,
            mode=mode,
            compute_backend=compute_backend,
            experiment_compute_backend=compute_backend,
            orchestrator_compute_backend=orchestrator_compute_backend,
            orchestrator_instance_type=orchestrator_instance_type,
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
        _write_config_yaml(pdir, project, db_user or user, settings, model=model)

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
        # SkyPilot runs remotely with the env living on the remote cluster (no
        # local .conda_env dir), so it takes the "env ready" fast path — otherwise
        # a healthy skypilot run reads as env-not-ready.
        sid = project.slurm_job_id or ""
        is_remote = sid.startswith("skypilot")
        if is_remote:
            owner_keys = _get_user_keys(owner) if owner else {}
            conda_env_display = owner_keys.get("gcp_conda_env") or settings.cloud_conda_env or "ark-base"
            env_ready = True
        else:
            env_ready = project_env_ready(pdir)
            if env_ready:
                conda_env_display = str(project_env_prefix(pdir))
            else:
                conda_env_display = settings.slurm_conda_env or ""
        # Environment label shown in the dashboard, keyed off the handle prefix.
        if sid.startswith("skypilot"):
            environment = "SkyPilot"
        elif sid and not sid.startswith("local"):
            environment = "ROCS Testbed"
        else:
            environment = "Local"
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
            "queue_eta_end": _queue_eta_end(session, settings, project),
            "user_email": owner.email if owner else "",
            "model": _read_project_model(pdir, project=project),
            "telegram_token": project.telegram_token,
            "telegram_chat_id": project.telegram_chat_id,
            "has_deep_research": (pdir / "auto_research" / "state" / "deep_research.md").exists(),
            "environment": environment,
            "conda_env": conda_env_display,
            "conda_env_ready": env_ready,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "cost_report": _read_cost_report(pdir, project=project),
            "compute_backend": project.compute_backend,
            "orchestrator_compute_backend": project.orchestrator_compute_backend or "local",
            "orchestrator_instance_type": getattr(project, "orchestrator_instance_type", "") or "",
            "experiment_compute_backend": project.experiment_compute_backend or project.compute_backend or "slurm",
            "error_message": project.error_message or "",
            # HITL
            "autonomy_level": project.autonomy_level or "collaborative",
            "control_state": project.control_state or "",
            "activity": project.activity or "",
            "pending_decision": _decision_to_dict(get_open_decision(session, project_id)),
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
            # Dispatch cancel off the persisted handle (local:/skypilot:/slurm).
            pdir = _project_dir(settings, project.user_id, project_id)
            launcher_from_handle(project.slurm_job_id, log_fn=logger.info).cancel(
                project.slurm_job_id, pdir
            )
        update_project(session, project, status="stopped")
        return JSONResponse({"ok": True})


# ── HITL control plane (webapp ↔ running orchestrator via the shared DB) ──────

def _decision_to_dict(dec) -> Optional[dict]:
    if not dec:
        return None
    try:
        options = json.loads(dec.options or "[]")
    except Exception:
        options = []
    return {
        "id": dec.id, "kind": dec.kind, "question": dec.question,
        "options": options, "context": dec.context,
        "default_index": dec.default_index, "timeout_action": dec.timeout_action,
        "deadline_at": dec.deadline_at.isoformat() if dec.deadline_at else None,
    }


@router.post("/api/projects/{project_id}/command")
async def api_project_command(project_id: str, request: Request):
    """Send a control command to a RUNNING project's orchestrator (the回程
    channel the webapp lacked): pause | resume | stop | steer | set_autonomy."""
    user = _require_user(request)
    body = await request.json()
    kind = (body.get("kind") or "").strip()
    payload = (body.get("payload") or "").strip()
    if kind not in ("pause", "resume", "stop", "steer", "set_autonomy"):
        raise HTTPException(400, "unknown command")
    if kind == "steer" and not payload:
        raise HTTPException(400, "steer needs a message")
    if kind == "set_autonomy" and payload not in ("full_auto", "collaborative", "hands_on"):
        raise HTTPException(400, "bad autonomy level")
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if kind == "set_autonomy":
            set_autonomy(session, project_id, payload)
        enqueue_command(session, project_id, kind, payload=payload,
                        source="webapp", created_by=user.email)
        if kind == "steer":
            add_message(session, project_id, "user", payload, kind="steer")
    return JSONResponse({"ok": True})


@router.get("/api/projects/{project_id}/decision")
async def api_get_decision(project_id: str, request: Request):
    """Current open decision the orchestrator is waiting on, if any."""
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        return JSONResponse({"decision": _decision_to_dict(get_open_decision(session, project_id))})


@router.post("/api/projects/{project_id}/decision")
async def api_answer_decision(project_id: str, request: Request):
    """Answer a pending decision from the webapp (index for a menu pick, or
    free text). Mirrors a Telegram reply — whichever channel answers first wins."""
    user = _require_user(request)
    body = await request.json()
    decision_id = (body.get("decision_id") or "").strip()
    index = int(body.get("index", -1))
    text = (body.get("text") or "").strip()
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        dec = get_decision(session, decision_id) if decision_id else get_open_decision(session, project_id)
        if not dec or dec.project_id != project_id:
            raise HTTPException(404, "no open decision")
        ok = answer_decision(session, dec.id, index=index, text=text,
                             by=user.email, source="webapp")
        if not ok:
            raise HTTPException(409, "decision already closed")
        # user bubble for the answer
        try:
            opts = json.loads(dec.options or "[]")
        except Exception:
            opts = []
        bubble = text or (f"Option {index + 1}: {opts[index]}" if 0 <= index < len(opts) else "")
        if bubble:
            add_message(session, project_id, "user", bubble, kind="answer")
    return JSONResponse({"ok": True})


def _message_to_dict(m) -> dict:
    return {"id": m.id, "role": m.role, "kind": m.kind, "text": m.text,
            "meta": (json.loads(m.meta) if m.meta else None),
            "ts": m.created_at.isoformat()}


@router.get("/api/projects/{project_id}/messages")
async def api_get_messages(project_id: str, request: Request, after: str = ""):
    """Conversation thread (chat bubbles). ``after`` = ISO cursor for incremental."""
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_read_project(request, project):
            raise HTTPException(404)
        msgs = list_messages(session, project_id, after=after or None)
        return JSONResponse({"messages": [_message_to_dict(m) for m in msgs]})


@router.post("/api/projects/{project_id}/message")
async def api_post_message(project_id: str, request: Request):
    """User sends a chat message — the sideband control channel. The message is
    routed by intent:
      • an open decision is waiting  → this answers it;
      • ASK (a status/state question) → answered instantly from the project's DB
        state read-model, without ever touching the running agent;
      • STEER (an instruction)        → enqueued for the orchestrator + acked now.
    The classification is automatic (cheap model + heuristic). The response's
    ``routed`` field tells the client what happened (so a finished-project ASK
    doesn't trigger a resume)."""
    user = _require_user(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    settings = get_settings()
    keys = _get_user_keys(user)

    # Phase 1 (session open): auth, answer-a-decision short-circuit, snapshot state.
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        dec = get_open_decision(session, project_id)
        if dec:
            answer_decision(session, dec.id, index=-1, text=text,
                            by=user.email, source="webapp")
            add_message(session, project_id, "user", text, kind="answer")
            return JSONResponse({"ok": True, "routed": "decision"})
        running = (project.status == "running")
        activity = project.activity or ""
        state_text = sideband.build_state_readmodel(session, project)

    # FINISHED project → hand the message to the PERSISTENT OpenHands chat agent
    # (Claude-Code-level: full memory + file access; it decides whether to answer,
    # read, edit, or run, and streams its steps). Running projects keep the
    # lightweight classify path below (the orchestrator is the live agent — we
    # steer it rather than spawn a competitor).
    if not running and project.status in ("done", "stopped", "failed"):
        with get_session(settings.db_path) as session:
            project = get_project(session, project_id)
            add_message(session, project_id, "user", text, kind="message")
            pdir = _project_dir(settings, project.user_id, project_id)
            model = _read_project_model(pdir, project=project) or "claude-sonnet-4-6"
            # Config must reflect the OWNER's keys (reroute, cloud, github),
            # not the requester's — an admin may be acting on a user's project.
            _owner = get_user(session, project.user_id) or user
            _write_config_yaml(pdir, project, _owner, settings, model=model)
            tg_token = project.telegram_token
            tg_chat = project.telegram_chat_id
            project.status = "initializing"
            session.add(project)
            session.commit()
            session.refresh(project)
        asyncio.create_task(_restart_project_async(
            project_id=project_id, pdir=pdir, is_admin=_is_admin(user),
            token=tg_token, chat_id=tg_chat, chat_message=text))
        return JSONResponse({"ok": True, "routed": "chat", "launching": True})

    # A chat turn is already in progress (it set activity "💬 …") — don't route
    # this message into the steer void; tell the user to hold on.
    if running and activity.startswith("💬"):
        with get_session(settings.db_path) as session:
            add_message(session, project_id, "user", text, kind="message")
            add_message(session, project_id, "agent",
                        "⏳ 我还在处理上一条，稍等一下再发。", kind="notice")
        return JSONResponse({"ok": True, "routed": "busy"})

    # Phase 2 (no session held): classify; LLM calls are offloaded so the async
    # event loop isn't blocked.
    intent = await asyncio.to_thread(sideband.classify_message, text, keys)

    if intent == "ask":
        # A status/progress question → instant snapshot answer. A CONTENT question
        # (page count, what a section says, are the citations real) on a FINISHED
        # project → a read-only Claude agent that reads the real files and answers
        # (Claude-Code-level). Running projects always use the snapshot (don't spawn
        # a second agent over a live run).
        depth = "status" if running else sideband.classify_ask_depth(text)
        if depth == "content":
            with get_session(settings.db_path) as session:
                project = get_project(session, project_id)
                add_message(session, project_id, "user", text, kind="ask")
                pdir = _project_dir(settings, project.user_id, project_id)
                model = _read_project_model(pdir, project=project) or "claude-sonnet-4-6"
                _owner = get_user(session, project.user_id) or user
                _write_config_yaml(pdir, project, _owner, settings, model=model)
                tg_token = project.telegram_token
                tg_chat = project.telegram_chat_id
                project.status = "initializing"
                session.add(project)
                session.commit()
                session.refresh(project)
            asyncio.create_task(_restart_project_async(
                project_id=project_id, pdir=pdir, is_admin=_is_admin(user),
                token=tg_token, chat_id=tg_chat,
                apply_instruction=text, apply_scope="answer"))
            return JSONResponse({"ok": True, "routed": "ask", "depth": "content", "launching": True})
        answer = await asyncio.to_thread(sideband.answer_from_state, text, state_text, keys)
        with get_session(settings.db_path) as session:
            add_message(session, project_id, "user", text, kind="ask")
            add_message(session, project_id, "agent", answer, kind="message")
        return JSONResponse({"ok": True, "routed": "ask", "depth": "status"})

    # STEER. For a finished project, also classify the change's SCOPE so the
    # client offers the smallest mechanism (edit vs experiment vs full iteration).
    scope = "edit"
    if not running:
        scope = await asyncio.to_thread(sideband.classify_steer_scope, text, keys)
    with get_session(settings.db_path) as session:
        add_message(session, project_id, "user", text, kind="steer")
        if running:
            enqueue_command(session, project_id, "steer", payload=text,
                            source="webapp", created_by=user.email)
            where = f"「{activity}」" if activity else "the current step"
            add_message(session, project_id, "agent",
                        f"收到 ✅ 当前在 {where}。我会在这一步结束后应用这条指令。",
                        kind="message")
    return JSONResponse({"ok": True, "routed": "steer", "running": running, "scope": scope})


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
        _owner = get_user(session, project.user_id)
        _require_model_key(_get_user_keys(_owner) if _owner else {}, project.model_variant or "")
        # No concurrent hard-reject: if the user's lane is full the restart is
        # QUEUED (pending) by _try_submit_or_pending and promoted FIFO later —
        # consistent with new-project submission.
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

        # Re-copy venue template (clean removed .bib/.tex template files).
        # For custom templates the uploaded skeleton was preserved by
        # _clean_project_state above; re-run preprocess to strip any
        # writer-filled prose back to empty section stubs so the next run
        # starts from the same baseline as the initial upload.
        venue_fmt = body.get("venue_format") or project.venue_format or ""
        paper_dir = pdir / "paper"
        if venue_fmt and venue_fmt != "custom":
            paper_dir.mkdir(parents=True, exist_ok=True)
            copy_venue_template(venue_fmt, paper_dir)
        elif venue_fmt == "custom" and (paper_dir / "template_manifest.yaml").exists():
            try:
                from ark.template_preprocess import preprocess_custom_template
                preprocess_custom_template(paper_dir, venue_hint="custom")
            except Exception as e:
                logger.warning(f"Custom template re-preprocess on restart failed: {e}")

        # Re-substitute agent prompt templates (clean removed agents/)
        _substitute_agent_templates(
            pdir, project.id, project.title,
            venue_name=project.venue or project.venue_format or "NeurIPS",
            venue_format=project.venue_format,
            venue_pages=project.venue_pages,
        )

        # Rewrite config.yaml with updated settings. Default to the project's
        # OWN model, not a hardcoded one — a restart without an explicit model
        # choice must never silently switch providers.
        model = body.get("model") or _read_project_model(pdir, project=project) or "claude-sonnet-4-6"
        new_backend = body.get("compute_backend")
        if new_backend:
             _reject_unknown_backend(new_backend, VALID_EXPERIMENT_TYPES, "experiment")
             update_project(session, project, compute_backend=new_backend)
        new_orch_backend = body.get("orchestrator_compute_backend")
        if new_orch_backend:
            _reject_unknown_backend(new_orch_backend, VALID_ORCHESTRATOR_TYPES, "orchestrator")
            update_project(session, project, orchestrator_compute_backend=new_orch_backend)
        # Re-derive the orchestrator instance type whenever the restart dialog
        # touches cloud selection (either key present). Validate against the
        # EFFECTIVE backend (the new one if switched, else the project's current),
        # and persist the cleaned value — "" when the backend has no instance type
        # (local/slurm) so a stale cloud pick can't leak across a backend switch.
        if "orchestrator_instance_type" in body or "orchestrator_compute_backend" in body:
            eff_backend = new_orch_backend or project.orchestrator_compute_backend or "local"
            it = await _validate_instance_type_or_400(eff_backend, body.get("orchestrator_instance_type") or "")
            update_project(session, project, orchestrator_instance_type=it)

        _write_config_yaml(pdir, project, _owner or user, settings, model=model)

        # Write instructions if provided
        comment = body.get("comment", "").strip()
        if comment:
            _write_user_instructions(pdir, comment, source="webapp_restart")

        project.status = "initializing"
        tg_token = project.telegram_token
        tg_chat = project.telegram_chat_id
        session.add(project)
        session.commit()
        session.refresh(project)

    asyncio.create_task(_restart_project_async(
        project_id=project_id,
        pdir=pdir,
        is_admin=_is_admin(user),
        token=tg_token,
        chat_id=tg_chat,
    ))
    return JSONResponse({"ok": True, "status": "initializing", "slurm_job_id": ""})


@router.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str, request: Request):
    user = _require_user(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        pdir = _project_dir(settings, project.user_id, project_id)

        def _cleanup():
            shutil.rmtree(pdir, ignore_errors=True)

        if project.slurm_job_id:
            # Dispatch cancel off the persisted handle. rmtree runs via on_complete:
            # synchronously for local/SLURM (also cascades queued sub-jobs first),
            # and only after the async SkyPilot teardown has read config/state — so
            # we never delete the dir out from under an in-flight teardown.
            launcher_from_handle(project.slurm_job_id, log_fn=logger.info).cancel(
                project.slurm_job_id, pdir, on_complete=_cleanup
            )
        else:
            _cleanup()
        delete_project(session, project_id)
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
        _owner = get_user(session, project.user_id)
        _require_model_key(_get_user_keys(_owner) if _owner else {}, project.model_variant or "")
        # No concurrent hard-reject: if the user's lane is full the continue is
        # QUEUED (pending) by _try_submit_or_pending and promoted FIFO later —
        # consistent with new-project submission.
        # Floor at current iteration so historical-overshoot projects
        # (iter > max from pre-c077e15 code) still honor the requested +N.
        new_max = max(project.max_iterations, project.iteration) + additional
        update_project(session, project, max_iterations=new_max)
        pdir = _project_dir(settings, project.user_id, project_id)
        # Use requested model, or fall back to existing
        model = body.get("model") or _read_project_model(pdir, project=project) or "claude-sonnet-4-6"
        new_backend = body.get("compute_backend")
        if new_backend:
             _reject_unknown_backend(new_backend, VALID_EXPERIMENT_TYPES, "experiment")
             update_project(session, project, compute_backend=new_backend)
        new_orch_backend = body.get("orchestrator_compute_backend")
        if new_orch_backend:
            _reject_unknown_backend(new_orch_backend, VALID_ORCHESTRATOR_TYPES, "orchestrator")
            update_project(session, project, orchestrator_compute_backend=new_orch_backend)
        # Same instance-type re-derivation as restart (see there): validate the
        # pick against the effective backend and persist the cleaned value.
        if "orchestrator_instance_type" in body or "orchestrator_compute_backend" in body:
            eff_backend = new_orch_backend or project.orchestrator_compute_backend or "local"
            it = await _validate_instance_type_or_400(eff_backend, body.get("orchestrator_instance_type") or "")
            update_project(session, project, orchestrator_instance_type=it)

        _write_config_yaml(pdir, project, _owner or user, settings, model=model)
        if comment:
            _write_user_update(pdir, comment, source="webapp_continue")
            _write_user_instructions(pdir, comment, source="webapp_continue")

        tg_token = project.telegram_token
        tg_chat = project.telegram_chat_id
        project.status = "initializing"
        session.add(project)
        session.commit()
        session.refresh(project)

    asyncio.create_task(_restart_project_async(
        project_id=project_id,
        pdir=pdir,
        is_admin=_is_admin(user),
        token=tg_token,
        chat_id=tg_chat,
    ))
    return JSONResponse({"ok": True, "status": "initializing", "max_iterations": new_max})


@router.post("/api/projects/{project_id}/apply")
async def api_apply_change(project_id: str, request: Request):
    """Apply ONE targeted change to a finished project WITHOUT a full iteration.

    scope='edit' → a single writer/coder agent makes just this change + recompile;
    scope='experiment' → run the requested experiment, regen its figure, fold it in.
    Heavier "make it better overall" requests should use /continue instead."""
    user = _require_user(request)
    _check_webapp_enabled()
    body = await request.json()
    instruction = (body.get("instruction") or body.get("comment") or "").strip()
    scope = (body.get("scope") or "edit").strip()
    if scope not in ("edit", "experiment", "answer"):
        scope = "edit"
    if not instruction:
        raise HTTPException(400, "instruction required")
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_access_project(user, project):
            raise HTTPException(404)
        if project.status not in ("done", "stopped", "failed"):
            raise HTTPException(400, "Only finished projects take a direct apply.")
        active = [p for p in get_projects_for_user(session, project.user_id)
                  if p.status in ("queued", "running", "initializing") and p.id != project_id]
        if not _is_admin(user) and len(active) >= MAX_CONCURRENT_PER_USER:
            raise HTTPException(400, f"You already have {len(active)} active projects. "
                                     f"Max {MAX_CONCURRENT_PER_USER} concurrent.")
        pdir = _project_dir(settings, project.user_id, project_id)
        model = _read_project_model(pdir, project=project) or "claude-sonnet-4-6"
        _owner = get_user(session, project.user_id) or user
        _write_config_yaml(pdir, project, _owner, settings, model=model)
        tg_token = project.telegram_token
        tg_chat = project.telegram_chat_id
        project.status = "initializing"
        session.add(project)
        session.commit()
        session.refresh(project)
    asyncio.create_task(_restart_project_async(
        project_id=project_id, pdir=pdir, is_admin=_is_admin(user),
        token=tg_token, chat_id=tg_chat,
        apply_instruction=instruction, apply_scope=scope,
    ))
    return JSONResponse({"ok": True, "status": "initializing", "scope": scope})


@router.get("/api/system/status")
async def api_system_status():
    """Public endpoint — returns webapp gate state (no auth required)."""
    settings = get_settings()
    return JSONResponse({
        "disabled": _disabled_flag().exists(),
        # Maintenance banner (admin-authored notice, shown to all users). {} ⇒ none.
        "maintenance": _read_maintenance(),
        "slurm": {
            "available": slurm_available()
        },
        # Which SkyPilot clouds the operator has configured a launcher for, so the
        # UI can show only the usable cloud options in the compute selector. GCP
        # needs a central project; AWS needs a launcher identity (role ARN or a
        # host credential source).
        "cloud": {
            "gcp": bool(settings.cloud_gcp_project),
            "aws": bool(settings.cloud_launcher_role_arn
                        or settings.cloud_launcher_aws_credential_source),
        },
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


@router.get("/api/admin/maintenance")
async def api_admin_maintenance_get(request: Request):
    """Current maintenance banner state (for the admin console)."""
    _require_admin(request)
    return JSONResponse({"maintenance": _read_maintenance()})


@router.post("/api/admin/maintenance")
async def api_admin_maintenance_set(request: Request):
    """Publish or clear the maintenance banner. Empty message ⇒ cleared."""
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    state = _write_maintenance(
        message=body.get("message", ""),
        level=body.get("level", "warning"),
    )
    return JSONResponse({"maintenance": state})


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
                pdir = _project_dir(settings, p.user_id, p.id)
                launcher_from_handle(p.slurm_job_id, log_fn=logger.info).cancel(
                    p.slurm_job_id, pdir
                )
            update_project(session, p, status="stopped")
            stopped.append(p.id)
    return JSONResponse({"stopped": stopped, "count": len(stopped)})


def _req_dict(r) -> dict:
    """Serialise an AccessRequest row for the admin console."""
    return {
        "id": r.id,
        "email": r.email,
        "name": r.name or "",
        "affiliation": r.affiliation or "",
        "purpose": r.purpose or "",
        "status": r.status,
        "decline_reason": getattr(r, "decline_reason", "") or "",
        "created_at": r.created_at.isoformat() if r.created_at else "",
        "decided_at": r.authorized_at.isoformat() if r.authorized_at else "",
        "decided_by": r.authorized_by or "",
    }


@router.get("/api/admin/access-requests")
async def api_admin_access_requests(request: Request):
    """All access requests, grouped by status, for the admin Users panel."""
    _require_admin(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        rows = list_access_requests(session)
        out = {"pending": [], "authorized": [], "rejected": []}
        for r in rows:
            out.setdefault(r.status, []).append(_req_dict(r))
    return JSONResponse(out)


@router.get("/api/admin/users")
async def api_admin_users(request: Request):
    """Registered dashboard users, enriched with affiliation from their request."""
    _require_admin(request)
    settings = get_settings()
    with get_session(settings.db_path) as session:
        users = list_users(session)
        reqs = list_access_requests(session)
        # newest affiliation/purpose per email from access requests
        meta: dict[str, dict] = {}
        for r in sorted(reqs, key=lambda x: x.created_at or datetime.min):
            meta[r.email.lower()] = {"affiliation": r.affiliation or "", "purpose": r.purpose or ""}
        # project counts per user
        counts: dict[str, int] = {}
        for p in get_all_projects(session):
            counts[p.user_id] = counts.get(p.user_id, 0) + 1
        out = []
        for u in users:
            m = meta.get((u.email or "").lower(), {})
            out.append({
                "id": u.id,
                "email": u.email,
                "name": u.name or "",
                "affiliation": m.get("affiliation", ""),
                "projects": counts.get(u.id, 0),
                "login_count": u.login_count or 0,
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else "",
                "created_at": u.created_at.isoformat() if u.created_at else "",
                "is_admin": _is_admin(u),
            })
    return JSONResponse({"users": out})


@router.post("/api/admin/access/approve")
async def api_admin_access_approve(request: Request):
    """Approve a pending request: add to the CF Access allowlist + email the user."""
    admin = _require_admin(request)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "A valid email is required.")

    def _grant():
        from ark import access as _access
        # cmd_add adds to the Cloudflare allowlist, flips the DB request to
        # authorized, and sends the access-granted email. Raises if CF creds
        # are missing/misconfigured.
        _access.cmd_add([email], notify=True)

    try:
        await asyncio.to_thread(_grant)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"admin approve failed for {email}: {e}")
        raise HTTPException(502, f"Could not grant access: {e}")
    logger.info(f"admin {admin.email} approved access for {email}")
    return JSONResponse({"ok": True, "email": email, "status": "authorized"})


@router.post("/api/admin/access/decline")
async def api_admin_access_decline(request: Request):
    """Decline a request with a reason and email the applicant the reason."""
    admin = _require_admin(request)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    reason = (body.get("reason") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "A valid email is required.")
    settings = get_settings()
    with get_session(settings.db_path) as session:
        req = mark_access_declined(session, email, reason=reason, by=admin.email)
        name = (req.name if req else "") or ""
    emailed = False
    try:
        emailed = await asyncio.to_thread(
            send_access_declined_email, settings, email, name, reason
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"decline email failed for {email}: {e}")
    logger.info(f"admin {admin.email} declined access for {email} (emailed={emailed})")
    return JSONResponse({"ok": True, "email": email, "status": "rejected", "emailed": emailed})


@router.get("/api/projects/{project_id}/pdf")
async def api_get_pdf(project_id: str, request: Request):
    settings = get_settings()
    with get_session(settings.db_path) as session:
        project = get_project(session, project_id)
        if not project or not _can_read_project(request, project):
            raise HTTPException(404)
        owner_id = project.user_id
        ref = _latest_artifact_ref(session, project_id, "pdf")
    pdir = _project_dir(settings, owner_id, project_id)
    # Prefer a registered artifact (works for object storage / remote runs with
    # no shared FS); fall back to scanning the project dir (local/SLURM, or
    # before the first publish).
    served = _serve_registered_artifact(pdir, ref, filename="main.pdf",
                                        inline=True, min_size=10000)
    if served is not None:
        return served
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
        ref = _latest_artifact_ref(session, project_id, "uploaded_pdf")
    pdir = _project_dir(settings, owner_id, project_id)
    served = _serve_registered_artifact(pdir, ref, filename="uploaded.pdf", inline=True)
    if served is not None:
        return served
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
        # State from the DB projection + the registered PDF (ADR-0012/0013) so the
        # bundle doesn't depend on reading the orchestrator's disk.
        state_docs = list_state_docs(session, project_id)
        pdf_ref = _latest_artifact_ref(session, project_id, "pdf")
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

        # config (on disk) + key state docs (from the DB projection — ADR-0013;
        # disk fallback for legacy/unsynced projects).
        cfg_file = pdir / "config.yaml"
        if cfg_file.exists():
            zf.write(cfg_file, "config.yaml")
        for name in ("paper_state", "findings", "action_plan", "memory"):
            rel = f"auto_research/state/{name}.yaml"
            doc = state_docs.get(name)
            if doc:
                zf.writestr(rel, yaml.safe_dump(doc, default_flow_style=False,
                                                allow_unicode=True))
            else:
                f = pdir / rel
                if f.exists():
                    zf.write(f, rel)

        # Include the PDF from the artifact store when it isn't on a shared
        # filesystem (object storage / remote runs); a no-op locally, where the
        # paper/ walk above already added it.
        if pdf_ref and not (paper_dir / "main.pdf").exists():
            try:
                from ark.artifacts import ArtifactRef
                store = _artifact_store_for(pdir)
                with store.open(ArtifactRef.from_dict(pdf_ref)) as fh:
                    zf.writestr("paper/main.pdf", fh.read())
            except Exception:
                pass

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
    # HTTP/remote transport: the orchestrator pushes log lines to the event
    # store since there's no shared .out file. Prefer it when present.
    with get_session(settings.db_path) as session:
        evs = list_events(session, project_id, after_id=0, limit=5000)
    if evs:
        return JSONResponse({"lines": [e["line"] for e in evs[-lines:]],
                             "log_file": "control-plane"})

    pdir = _project_dir(settings, owner_id, project_id)
    log_dirs = [pdir / "logs", pdir / "auto_research" / "logs"]

    # Find the latest log file across all candidate dirs. Every chat/apply/
    # continue run spawns a NEW local_<ts>.out, so showing only the newest file
    # made the Live Log "forget" the whole prior run the moment a chat agent
    # started. Instead, concatenate ALL siblings of the newest file's pattern
    # family (oldest first, with separators) and return the tail — history stays
    # visible, and the SSE stream keeps appending from the newest file.
    log_lines: list[str] = []
    log_file = ""
    best: tuple[float, Path, str] | None = None
    for log_dir in log_dirs:
        for pattern in ["local_*.out", "slurm_*.out", "orchestrator.log", "*.log"]:
            for p in log_dir.glob(pattern):
                try:
                    mtime = p.stat().st_mtime
                    if best is None or mtime > best[0]:
                        best = (mtime, p, pattern)
                except Exception:
                    pass
    if best:
        log_file = str(best[1])
        try:
            family = sorted(best[1].parent.glob(best[2]), key=lambda p: p.stat().st_mtime)
            for i, p in enumerate(family):
                try:
                    chunk = p.read_text(errors="replace").splitlines()
                except Exception:
                    continue
                if i > 0 and log_lines:
                    log_lines.append(f"── {p.name} ──")
                log_lines.extend(chunk)
            log_lines = log_lines[-lines:]
        except Exception:
            try:
                log_lines = best[1].read_text(errors="replace").splitlines()[-lines:]
            except Exception:
                pass

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
    log_dirs = [pdir / "logs", pdir / "auto_research" / "logs"]

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
        last_msg_ts = None   # ISO cursor for chat-thread messages
        # Log source: pushed events (HTTP/remote transport, no shared FS) take
        # over as soon as any exist; otherwise tail the on-disk .out files
        # (local/SLURM). The latch means a project that starts emitting events
        # mid-stream switches over and never double-renders from files.
        last_event_id = 0
        saw_events = False
        while True:
            if await request.is_disconnected():
                break

            with get_session(settings.db_path) as _s:
                evs = list_events(_s, project_id, after_id=last_event_id, limit=2000)
            if evs:
                saw_events = True
                # On the first iteration skip the catch-up the client already
                # fetched via /log; afterwards emit everything new.
                if not first_iteration:
                    for e in evs:
                        yield f"data: {json.dumps({'line': e['line']})}\n\n"
                last_event_id = evs[-1]["id"]

            if not saw_events:
                # Find latest log file across all candidate dirs
                log_file = None
                best: tuple[float, Path] | None = None
                for log_dir in log_dirs:
                    for pattern in ["local_*.out", "slurm_*.out", "orchestrator.log", "*.log"]:
                        for p in log_dir.glob(pattern):
                            try:
                                mtime = p.stat().st_mtime
                                if best is None or mtime > best[0]:
                                    best = (mtime, p)
                            except Exception:
                                pass
                if best:
                    log_file = best[1]

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
                        # HITL live fields
                        "control_state": p.control_state or "",
                        "activity": p.activity or "",
                        "autonomy_level": p.autonomy_level or "collaborative",
                        "pending_decision": _decision_to_dict(get_open_decision(session, project_id)),
                    })
                    yield f"event: status\ndata: {payload}\n\n"

                # Emit new chat-thread bubbles since the last tick.
                try:
                    new_msgs = list_messages(session, project_id, after=last_msg_ts)
                    for m in new_msgs:
                        yield f"event: message\ndata: {json.dumps(_message_to_dict(m))}\n\n"
                        last_msg_ts = m.created_at.isoformat()
                except Exception:
                    pass

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
    # One entry per official template. Venues grouped on the same line share the
    # exact same LaTeX style files (verified against each venue's 2026 CFP); the
    # parenthetical names the official template/format.
    # Each entry's name is the template / organization; the parenthetical lists
    # example venues that use it (verified against the venues' official CFPs).
    venues = [
        # ── ML / AI (one template each) ──
        {"name": "ICML",    "format": "icml",    "pages": 8, "year": 2026},
        {"name": "NeurIPS", "format": "neurips", "pages": 9, "year": 2026},
        {"name": "ICLR",    "format": "iclr",    "pages": 9, "year": 2026},
        {"name": "AAAI",    "format": "aaai",    "pages": 7, "year": 2026},
        {"name": "TMLR (journal — no page limit)", "format": "tmlr", "pages": 12, "year": 2026},
        # ── ACL — Association for Computational Linguistics ──
        {"name": "ACL (ACL, EMNLP, NAACL, EACL, AACL, COLING)", "format": "acl", "pages": 8, "year": 2026},
        # ── CVF — Computer Vision Foundation ──
        {"name": "CVF (CVPR, ICCV, WACV)", "format": "cvpr", "pages": 8, "year": 2026},
        # ── ML Systems ──
        {"name": "MLSys", "format": "mlsys", "pages": 10, "year": 2026},
        # ── ACM SIGPLAN — acmart [sigplan] (verified each venue's 2026 CFP) ──
        {"name": "ACM SIGPLAN (SOSP, EuroSys, ASPLOS, PPoPP)", "format": "acmart", "pages": 12, "year": 2026},
        # ── USENIX — one template for all ──
        {"name": "USENIX (OSDI, NSDI, FAST, USENIX Security)", "format": "usenix", "pages": 13, "year": 2026},
        # ── IEEEtran — IEEE conference mode ──
        {"name": "IEEEtran (INFOCOM, ICC, GLOBECOM, ICASSP)", "format": "ieee", "pages": 9, "year": 2026},
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
