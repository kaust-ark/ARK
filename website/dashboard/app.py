"""FastAPI application factory for ARK webapp."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import get_engine
from .jobs import poll_job, slurm_state_to_status, launch_local_job, poll_local_job, cancel_local_job
from .notify import send_completion_email, send_telegram_notify
from .routes import router

logger = logging.getLogger("website.dashboard")

_log_mtimes: dict[str, float] = {}   # project_id → last log mtime


def _pname(p) -> str:
    """Human-readable project label: title if set, else slug name."""
    return p.title if p.title else p.name


def _advance_pending_queue(session, settings):
    """Promote the oldest pending project whose owner has room and global cap allows.

    Respects MAX_CONCURRENT_PER_USER and MAX_CONCURRENT_GLOBAL from routes.
    Keeps looping until no more slots are available.
    """
    from .db import update_project, Project, get_user
    from .routes import (
        _get_user_keys,
        MAX_CONCURRENT_PER_USER,
        MAX_CONCURRENT_GLOBAL,
    )
    from sqlmodel import select

    while True:
        active = session.exec(
            select(Project).where(Project.status.in_(["queued", "running"]))
        ).all()
        if len(active) >= MAX_CONCURRENT_GLOBAL:
            return
        per_user: dict[str, int] = {}
        for p in active:
            per_user[p.user_id] = per_user.get(p.user_id, 0) + 1

        pending_list = session.exec(
            select(Project).where(Project.status == "pending")
            .order_by(Project.created_at.asc())
        ).all()
        pending = next(
            (p for p in pending_list
             if per_user.get(p.user_id, 0) < MAX_CONCURRENT_PER_USER),
            None,
        )
        if not pending:
            return

        from .jobs import submit_job, slurm_available
        pdir = settings.projects_root / pending.user_id / pending.id
        log_dir = pdir / "logs"
        log_dir.mkdir(exist_ok=True)
        user_obj = get_user(session, pending.user_id)
        api_keys = _get_user_keys(user_obj) if user_obj else {}
        try:
            if slurm_available():
                job_id = submit_job(pending.id, pending.mode, pending.max_iterations,
                                    pdir, log_dir, settings, api_keys=api_keys)
                update_project(session, pending, status="queued", slurm_job_id=job_id)
            else:
                job_id = launch_local_job(pending.id, pending.mode, pending.max_iterations,
                                          pdir, log_dir, settings, api_keys=api_keys)
                update_project(session, pending, status="running", slurm_job_id=job_id)
            logger.info(f"Queue advance: {pending.id} → job {job_id}")
        except Exception as e:
            logger.error(f"Queue advance failed {pending.id}: {e}")
            return
_stuck_alerted: set[str] = set()     # project_ids already sent stuck alert
_tg_offsets: dict[str, int] = {}     # project_id → last Telegram update_id seen
STUCK_MINUTES = 60


async def _poll_jobs(app: FastAPI):
    """Background task: poll SLURM job states every 60 s."""
    from .db import get_running_projects, get_session, update_project, get_user
    settings = get_settings()

    while True:
        try:
            await asyncio.sleep(60)
            with get_session(settings.db_path) as session:
                projects = get_running_projects(session)
                for p in projects:
                    if not p.slurm_job_id:
                        continue

                    pdir = settings.projects_root / p.user_id / p.id
                    from .constants import DASHBOARD_PREFIX
                    url = f"{settings.base_url}{DASHBOARD_PREFIX}/#project/{p.id}"

                    # ── Local / Cloud subprocess job ──────────────────────
                    if p.slurm_job_id.startswith(("local:", "cloud:")):
                        prefix = "local:" if p.slurm_job_id.startswith("local:") else "cloud:"
                        pid_str = p.slurm_job_id[len(prefix):]
                        if not pid_str.isdigit():
                            continue
                        pid = int(pid_str)
                        local_state = poll_local_job(pid, pdir / "logs")
                        new_status = slurm_state_to_status(local_state)
                        if new_status != p.status:
                            update_project(session, p, status=new_status)
                            logger.info(f"Local project {p.id}: {p.status} → {new_status}")
                            if new_status in ("done", "failed", "stopped"):
                                _advance_pending_queue(session, settings)
                            if new_status == "done":
                                score = 0.0
                                ps = pdir / "auto_research" / "state" / "paper_state.yaml"
                                if ps.exists():
                                    import yaml as _yaml
                                    d = _yaml.safe_load(ps.read_text()) or {}
                                    score = float(d.get("current_score", 0))
                                send_telegram_notify(
                                    f"✅ <b>{_pname(p)}</b> done — {score:.1f}/10\n<a href='{url}'>{url}</a>",
                                    bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                                )
                                user = get_user(session, p.user_id)
                                if user:
                                    pdf_files = sorted(
                                        (pdir / "paper").glob("*.pdf"),
                                        key=lambda x: x.stat().st_mtime,
                                        reverse=True,
                                    )
                                    pdf_path = str(pdf_files[0]) if pdf_files else None
                                    send_completion_email(
                                        settings,
                                        to_email=user.email,
                                        project_name=_pname(p),
                                        score=score,
                                        pdf_path=pdf_path,
                                        project_url=url,
                                    )
                                _log_mtimes.pop(p.id, None)
                                _stuck_alerted.discard(p.id)
                            elif new_status in ("failed", "stopped"):
                                send_telegram_notify(
                                    f"❌ <b>{_pname(p)}</b> {new_status}\n<a href='{url}'>{url}</a>",
                                    bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                                )
                                _log_mtimes.pop(p.id, None)
                                _stuck_alerted.discard(p.id)
                        # Stuck watchdog for local jobs
                        if p.status == "running" or new_status == "running":
                            log_dir = pdir / "logs"
                            log_files = sorted(
                                log_dir.glob("local_*.out"),
                                key=lambda x: x.stat().st_mtime,
                                reverse=True,
                            )
                            if log_files:
                                mtime = log_files[0].stat().st_mtime
                                last = _log_mtimes.get(p.id, mtime)
                                _log_mtimes[p.id] = mtime
                                if mtime != last:
                                    _stuck_alerted.discard(p.id)
                                elif p.id not in _stuck_alerted:
                                    idle_min = (time.time() - mtime) / 60
                                    if idle_min > STUCK_MINUTES:
                                        send_telegram_notify(
                                            f"⚠️ <b>{_pname(p)}</b> may be stuck\n"
                                            f"No log output for {int(idle_min)} min",
                                            bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                                        )
                                        _stuck_alerted.add(p.id)
                        continue

                    # ── SLURM job ─────────────────────────────────────────
                    slurm_state = poll_job(p.slurm_job_id)
                    new_status = slurm_state_to_status(slurm_state)

                    if new_status != p.status:
                        # Auto-restart if cluster cancelled the job
                        # (user-initiated Stop sets DB to "stopped" synchronously, so poll
                        #  won't see those projects as "running" → no false trigger)
                        if new_status == "stopped":
                            log_files = list((pdir / "logs").glob("slurm_*.out"))
                            if len(log_files) < 5:
                                try:
                                    from .jobs import submit_job
                                    new_job_id = submit_job(
                                        project_id=p.id,
                                        mode=p.mode,
                                        max_iterations=p.max_iterations,
                                        project_dir=pdir,
                                        log_dir=pdir / "logs",
                                        settings=settings,
                                    )
                                    update_project(session, p, status="queued", slurm_job_id=new_job_id)
                                    logger.info(f"Auto-restarted {p.id}: new job {new_job_id} (attempt {len(log_files)})")
                                    send_telegram_notify(
                                        f"⚡ <b>{_pname(p)}</b> 自动重启（集群 cancel，第 {len(log_files)} 次）\n"
                                        f"新 Job: #{new_job_id}\n<a href='{url}'>{url}</a>",
                                        bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                                    )
                                    _log_mtimes.pop(p.id, None)
                                    _stuck_alerted.discard(p.id)
                                    continue  # skip normal stopped handling
                                except Exception as e:
                                    logger.error(f"Auto-restart failed for {p.id}: {e}")
                                    # fall through → normal "stopped"

                        update_project(session, p, status=new_status)
                        logger.info(f"Project {p.id}: {p.status} → {new_status}")
                        if new_status in ("done", "failed", "stopped"):
                            _advance_pending_queue(session, settings)

                        if new_status == "running":
                            send_telegram_notify(
                                f"🚀 <b>{_pname(p)}</b> started running\n<a href='{url}'>{url}</a>",
                                bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                            )

                        elif new_status == "done":
                            # Read score from paper_state.yaml (authoritative)
                            score = 0.0
                            ps = pdir / "auto_research" / "state" / "paper_state.yaml"
                            if ps.exists():
                                import yaml as _yaml
                                d = _yaml.safe_load(ps.read_text()) or {}
                                score = float(d.get("current_score", 0))

                            send_telegram_notify(
                                f"✅ <b>{_pname(p)}</b> done — {score:.1f}/10\n<a href='{url}'>{url}</a>",
                                bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                            )

                            # Send completion email
                            user = get_user(session, p.user_id)
                            if user:
                                pdf_files = sorted(
                                    (pdir / "paper").glob("*.pdf"),
                                    key=lambda x: x.stat().st_mtime,
                                    reverse=True
                                )
                                pdf_path = str(pdf_files[0]) if pdf_files else None
                                send_completion_email(
                                    settings,
                                    to_email=user.email,
                                    project_name=_pname(p),
                                    score=score,
                                    pdf_path=pdf_path,
                                    project_url=url,
                                )

                            _log_mtimes.pop(p.id, None)
                            _stuck_alerted.discard(p.id)

                        elif new_status in ("failed", "stopped"):
                            send_telegram_notify(
                                f"❌ <b>{_pname(p)}</b> {new_status}\n<a href='{url}'>{url}</a>",
                                bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                            )
                            _log_mtimes.pop(p.id, None)
                            _stuck_alerted.discard(p.id)

                    # Stuck watchdog — check projects that are (or just became) running
                    if p.status == "running" or new_status == "running":
                        log_dir = pdir / "logs"
                        log_files = sorted(
                            log_dir.glob("slurm_*.out"),
                            key=lambda x: x.stat().st_mtime,
                            reverse=True,
                        )
                        if log_files:
                            mtime = log_files[0].stat().st_mtime
                            last = _log_mtimes.get(p.id, mtime)
                            _log_mtimes[p.id] = mtime
                            if mtime != last:
                                _stuck_alerted.discard(p.id)  # new output → clear alert
                            elif p.id not in _stuck_alerted:
                                idle_min = (time.time() - mtime) / 60
                                if idle_min > STUCK_MINUTES:
                                    send_telegram_notify(
                                        f"⚠️ <b>{_pname(p)}</b> may be stuck\n"
                                        f"No log output for {int(idle_min)} min",
                                        bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                                    )
                                    _stuck_alerted.add(p.id)

                # Always try to advance queue at end of each poll cycle
                _advance_pending_queue(session, settings)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Job poller error: {e}")

        # Poll Telegram for template links on waiting_template projects
        try:
            await _poll_template_links(settings)
        except Exception as e:
            logger.error(f"Template link poller error: {e}")


async def _poll_template_links(settings):
    """Check Telegram for template .zip links on waiting_template projects."""
    import re
    import shutil
    import tempfile
    import urllib.request
    import zipfile
    from .db import get_waiting_template_projects, get_session, update_project
    from .jobs import submit_job, slurm_available
    from .notify import send_telegram_notify

    with get_session(settings.db_path) as session:
        projects = get_waiting_template_projects(session)

    for p in projects:
        if not p.telegram_token or not p.telegram_chat_id:
            continue
        token = p.telegram_token
        chat_id = p.telegram_chat_id
        offset = _tg_offsets.get(p.id, 0)

        # Fetch updates from Telegram
        try:
            url = (f"https://api.telegram.org/bot{token}/getUpdates"
                   f"?chat_id={chat_id}&offset={offset}&timeout=1&limit=10")
            with urllib.request.urlopen(url, timeout=10) as r:
                import json as _json
                data = _json.loads(r.read())
        except Exception:
            continue

        if not data.get("ok"):
            continue

        for update in data.get("result", []):
            update_id = update.get("update_id", 0)
            _tg_offsets[p.id] = update_id + 1

            msg = update.get("message", {})
            text = (msg.get("text") or "").strip()

            # Look for a URL ending in .zip or containing common template hosts
            url_match = re.search(r'https?://\S+\.zip', text)
            if not url_match:
                continue

            zip_url = url_match.group(0)
            pdir = settings.projects_root / p.user_id / p.id
            paper_dir = pdir / "paper"

            # Download and extract zip
            try:
                send_telegram_notify(
                    f"⬇️ Downloading template from:\n{zip_url}",
                    bot_token=token, chat_id=chat_id,
                )
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    urllib.request.urlretrieve(zip_url, tmp.name)
                    with zipfile.ZipFile(tmp.name) as zf:
                        # Extract only .tex, .sty, .cls, .bst, .bib files
                        for member in zf.namelist():
                            if any(member.endswith(ext) for ext in
                                   (".tex", ".sty", ".cls", ".bst", ".bib")):
                                # Flatten into paper/ (strip path prefix)
                                fname = Path(member).name
                                with zf.open(member) as src, \
                                        open(paper_dir / fname, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
            except Exception as e:
                send_telegram_notify(
                    f"❌ Failed to download/extract template: {e}",
                    bot_token=token, chat_id=chat_id,
                )
                continue

            # Submit the job now
            with get_session(settings.db_path) as session:
                proj = session.get(type(p), p.id)
                if not proj or proj.status != "waiting_template":
                    continue
                log_dir = pdir / "logs"
                log_dir.mkdir(exist_ok=True)
                slurm_job_id = ""
                try:
                    if slurm_available():
                        slurm_job_id = submit_job(
                            project_id=p.id,
                            mode=proj.mode,
                            max_iterations=proj.max_iterations,
                            project_dir=pdir,
                            log_dir=log_dir,
                            settings=settings,
                        )
                        new_proj_status = "queued"
                    else:
                        slurm_job_id = launch_local_job(
                            p.id, proj.mode, proj.max_iterations,
                            pdir, log_dir, settings,
                        )
                        new_proj_status = "running"
                    update_project(session, proj, status=new_proj_status,
                                   slurm_job_id=slurm_job_id)
                    send_telegram_notify(
                        f"✅ Template installed! <b>{_pname(proj)}</b> queued.\nJob: #{slurm_job_id}",
                        bot_token=token, chat_id=chat_id,
                    )
                except Exception as e:
                    update_project(session, proj, status="failed")
                    send_telegram_notify(
                        f"❌ Job submission failed: {e}",
                        bot_token=token, chat_id=chat_id,
                    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Idempotent migration: add columns BEFORE SQLAlchemy create_all,
    # so the ORM sees the full schema when it reflects existing tables.
    import sqlite3 as _sq3
    try:
        _c = _sq3.connect(settings.db_path)
        for col in ("telegram_token TEXT DEFAULT ''", "telegram_chat_id TEXT DEFAULT ''",
                    "max_dev_iterations INTEGER DEFAULT 3"):
            try:
                _c.execute(f"ALTER TABLE project ADD COLUMN {col}")
            except Exception:
                pass
        try:
            _c.execute("ALTER TABLE user ADD COLUMN welcome_sent BOOLEAN DEFAULT 0")
        except Exception:
            pass
        for ucol in ("telegram_token TEXT DEFAULT ''", "telegram_chat_id TEXT DEFAULT ''"):
            try:
                _c.execute(f"ALTER TABLE user ADD COLUMN {ucol}")
            except Exception:
                pass
        # Feedback table
        _c.execute("""CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT DEFAULT '',
            message TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        _c.commit()
        # Fix existing user display names (first.last@domain → First)
        try:
            rows = _c.execute("SELECT id, email, name FROM user").fetchall()
            for uid, email, old_name in rows:
                correct = email.split("@")[0].split(".")[0].capitalize()
                if old_name != correct:
                    _c.execute("UPDATE user SET name=? WHERE id=?", (correct, uid))
            _c.commit()
        except Exception:
            pass
        _c.close()
    except Exception:
        pass
    # Now create engine + tables (ORM will see the migrated schema)
    get_engine(settings.db_path)

    # Migrate existing project data: populate new DB columns from YAML state files
    from website.dashboard.db import migrate_project_data
    try:
        migrate_project_data(settings.db_path, str(settings.projects_root))
        logger.info("Project data migration completed.")
    except Exception as e:
        logger.warning(f"Project data migration failed (non-fatal): {e}")

    logger.info(f"ARK Webapp starting. DB: {settings.db_path}")
    logger.info(f"Projects root: {settings.projects_root}")

    poll_task = asyncio.create_task(_poll_jobs(app))
    yield
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    logger.info("ARK Webapp stopped.")


def create_app():
    """Create the ASGI application serving both homepage and dashboard.

    Architecture: outer FastAPI mounts the dashboard sub-app at /dashboard
    and serves the static homepage at /. Starlette's native Mount handles
    path prefix stripping and root_path propagation — no custom middleware.

    Lifespan (DB migration, poll_task) is on the outer app because
    Starlette does NOT propagate lifespan to mounted sub-apps.
    """
    from starlette.staticfiles import StaticFiles
    from pathlib import Path
    from .constants import DASHBOARD_PREFIX

    settings = get_settings()

    # ── Dashboard sub-app (webapp routes + static assets) ──────────────
    dashboard = FastAPI(
        title="ARK Dashboard",
        description="Lab-facing project submission & monitoring",
        version="0.1.0",
    )

    cookie_name = os.environ.get("ARK_SESSION_COOKIE", "session")
    dashboard.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=cookie_name,
        max_age=86400 * 7,   # 7 days
        https_only=False,    # Set True if behind HTTPS proxy
    )

    dashboard.include_router(router)

    static_dir = Path(__file__).parent / "static"
    dashboard.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── Outer app (homepage + dashboard mount) ─────────────────────────
    outer = FastAPI(title="ARK Research Portal", lifespan=lifespan)

    # Starlette's Mount matches /dashboard/ but NOT bare /dashboard (it
    # passes empty string to the sub-app which 404s). Register a redirect
    # BEFORE the mount so /dashboard → /dashboard/ works.
    from fastapi.responses import RedirectResponse as _Redir

    @outer.get(DASHBOARD_PREFIX)
    async def _dashboard_redirect():
        return _Redir(DASHBOARD_PREFIX + "/", status_code=301)

    outer.mount(DASHBOARD_PREFIX, dashboard)

    # Public /api/request-access endpoint — must be registered BEFORE the
    # homepage catch-all mount, otherwise StaticFiles swallows it.
    from .request_access import router as request_access_router
    outer.include_router(request_access_router)

    # Serve the static homepage as catch-all at /. Must be mounted LAST
    # so /dashboard/* matches first. html=True serves index.html for
    # directory URLs (/, /zh/, /ar/).
    homepage_dir = Path(__file__).resolve().parent.parent / "homepage"
    if homepage_dir.is_dir():
        outer.mount("/", StaticFiles(directory=str(homepage_dir), html=True), name="homepage")

    return outer
