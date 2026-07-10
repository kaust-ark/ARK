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
from ark.launcher import (
    LaunchSpec, launcher_from_handle,
    STOPPED, GONE, UNKNOWN, ACTIVE_STATUSES,
)
from .notify import send_completion_email, send_telegram_notify
from .routes import router

logger = logging.getLogger("website.dashboard")

_log_mtimes: dict[str, float] = {}   # project_id → last log mtime


def _pname(p) -> str:
    """Human-readable project label: title if set, else slug name."""
    return p.title if p.title else p.name


def _gc_project_env(pdir, project_id: str = ""):
    """Reclaim disk by deleting the per-project conda env once terminal.

    The env (<project_dir>/.conda_env, ~1-2 GB cloned from ark-base) is only
    needed while the pipeline runs. Research Phase Step 0 is idempotent, so a
    later continue/restart transparently re-provisions it (~200s). Best-effort —
    never raises into the poll loop.
    """
    try:
        from .jobs import project_env_prefix
        env = project_env_prefix(pdir)
        if env.exists():
            import shutil
            shutil.rmtree(env, ignore_errors=True)
            logger.info(f"GC: removed conda env for {project_id or pdir.name} ({env})")
    except Exception as e:
        logger.warning(f"conda env GC failed for {pdir}: {e}")


def _gc_terminal_envs(session, settings):
    """Sweep: remove .conda_env for finished projects.

    The transition-based GC (in the poll loop) only fires when the webapp itself
    observes running→terminal. But the orchestrator often self-reports `done`
    straight to the DB, so `get_running_projects` never returns the project and
    that hook is missed. This sweep is the reliable path: it GCs any terminal
    project whose env still exists. Safe — the orchestrator process has already
    exited, and a later continue/restart re-provisions (Research Step 0).
    """
    from .db import Project
    from sqlmodel import select
    try:
        terminal = session.exec(
            select(Project).where(Project.status.in_(["done", "failed", "stopped"]))
        ).all()
        for p in terminal:
            pdir = settings.projects_root / p.user_id / p.id
            if (pdir / ".conda_env" / "conda-meta").is_dir():
                _gc_project_env(pdir, p.id)
    except Exception as e:
        logger.warning(f"terminal env GC sweep failed: {e}")


def _advance_pending_queue(session, settings):
    """Promote the oldest pending project whose LANE has room.

    Two independent FIFO lanes (see routes.py caps):
      • regular lane: per-user MAX_CONCURRENT_PER_USER, global MAX_CONCURRENT_REGULAR_GLOBAL
      • admin  lane: global MAX_CONCURRENT_ADMIN_GLOBAL
    Regular and admin pools never block each other. Loops until no lane has room.
    """
    from .db import update_project, Project, get_user
    from .routes import (
        _get_user_keys,
        _admin_user_ids,
        MAX_CONCURRENT_PER_USER,
        MAX_CONCURRENT_REGULAR_GLOBAL,
        MAX_CONCURRENT_ADMIN_GLOBAL,
    )
    from sqlmodel import select

    while True:
        admin_ids = _admin_user_ids(session)
        active = session.exec(
            select(Project).where(Project.status.in_(["queued", "running"]))
        ).all()
        admin_active = [p for p in active if p.user_id in admin_ids]
        regular_active = [p for p in active if p.user_id not in admin_ids]
        per_user: dict[str, int] = {}
        for p in regular_active:
            per_user[p.user_id] = per_user.get(p.user_id, 0) + 1
        admin_room = len(admin_active) < MAX_CONCURRENT_ADMIN_GLOBAL
        regular_room = len(regular_active) < MAX_CONCURRENT_REGULAR_GLOBAL

        pending_list = session.exec(
            select(Project).where(Project.status == "pending")
            .order_by(Project.created_at.asc())
        ).all()

        def _promotable(p):
            if p.user_id in admin_ids:
                return admin_room
            return regular_room and per_user.get(p.user_id, 0) < MAX_CONCURRENT_PER_USER

        pending = next((p for p in pending_list if _promotable(p)), None)
        if not pending:
            return

        from .routes import orchestrator_launcher_for
        pdir = settings.projects_root / pending.user_id / pending.id
        log_dir = pdir / "logs"
        log_dir.mkdir(exist_ok=True)
        user_obj = get_user(session, pending.user_id)
        api_keys = _get_user_keys(user_obj) if user_obj else {}
        try:
            spec = LaunchSpec(
                project_id=pending.id, mode=pending.mode,
                max_iterations=pending.max_iterations,
                project_dir=pdir, log_dir=log_dir, settings=settings, api_keys=api_keys,
            )
            # Promotion honours the project's configured backend (cloud/slurm/local)
            # via the same dispatch as initial submission — no longer forced to
            # slurm/local (which silently ran cloud projects on the control plane).
            launcher = orchestrator_launcher_for(pending, spec, session, settings)
            job_id = launcher.launch(spec)
            update_project(session, pending, status=launcher.initial_status, slurm_job_id=job_id)
            logger.info(f"Queue advance: {pending.id} → {job_id} ({launcher.initial_status})")
        except Exception as e:
            logger.error(f"Queue advance failed {pending.id}: {e}")
            return
_stuck_alerted: set[str] = set()     # project_ids already sent stuck alert
_tg_offsets: dict[str, int] = {}     # project_id → last Telegram update_id seen
STUCK_MINUTES = 60

# Grace period after a cloud orchestrator turns terminal before its VM is torn
# down (see _reap_terminal_clusters). Long enough for post-run inspection and for
# the orchestrator's own shutdown — including Layer-1 experiment-cluster teardown
# — to finish before we pull the VM out from under it. Autostop
# (ark/compute/_sky.py) stays the crash-safety backstop if the webapp is down
# through this window.
CLUSTER_REAP_GRACE_MINUTES = 5


def _notify_terminal_sweep(session, settings):
    """Reliable terminal notifications: DONE → email the OWNER; FAILED → email
    the primary ADMIN (plus the owner's Telegram in both cases).

    Sweep-based for the same reason as _gc_terminal_envs: the orchestrator
    usually writes its terminal status straight to the DB, so poll-loop
    transition hooks never fire for it — completion emails were silently
    skipped for every self-reported `done`. A marker file in the project dir
    (.ark_terminal_notified) makes each project notify exactly once; only
    projects that turned terminal recently (<6 h) are considered, so historical
    rows never get retro-notified.
    """
    from datetime import datetime, timedelta
    from .db import Project, get_user
    from .notify import send_failure_email, send_user_failure_email, user_actionable_failure
    from .constants import DASHBOARD_PREFIX
    from sqlmodel import select
    try:
        cutoff = datetime.utcnow() - timedelta(hours=6)
        recent = session.exec(
            select(Project).where(Project.status.in_(["done", "failed"]),
                                  Project.updated_at > cutoff)
        ).all()
        for p in recent:
            pdir = settings.projects_root / p.user_id / p.id
            marker = pdir / ".ark_terminal_notified"
            if marker.exists() or not pdir.is_dir():
                continue
            url = f"{settings.base_url}{DASHBOARD_PREFIX}/#project/{p.id}"
            owner = get_user(session, p.user_id)
            try:
                if p.status == "done":
                    score = float(p.score or 0.0)
                    ps = pdir / "auto_research" / "state" / "paper_state.yaml"
                    if ps.exists():
                        import yaml as _yaml
                        score = float((_yaml.safe_load(ps.read_text()) or {}).get("current_score", score))
                    send_telegram_notify(
                        f"✅ <b>{_pname(p)}</b> done — {score:.1f}/10\n<a href='{url}'>{url}</a>",
                        bot_token=p.telegram_token, chat_id=p.telegram_chat_id)
                    if owner:
                        pdfs = sorted((pdir / "paper").glob("*.pdf"),
                                      key=lambda x: x.stat().st_mtime, reverse=True)
                        send_completion_email(
                            settings, to_email=owner.email, project_name=_pname(p),
                            score=score, pdf_path=str(pdfs[0]) if pdfs else None,
                            project_url=url)
                else:  # failed → alert the primary admin, not the whole team
                    send_telegram_notify(
                        f"❌ <b>{_pname(p)}</b> failed\n<a href='{url}'>{url}</a>",
                        bot_token=p.telegram_token, chat_id=p.telegram_chat_id)
                    admins = getattr(settings, "admin_emails", []) or []
                    if admins:
                        send_failure_email(
                            settings, to_email=admins[0], project_name=_pname(p),
                            owner_email=(owner.email if owner else p.user_id),
                            error=p.error_message or "", project_url=url)
                    # USER-actionable failure (their credits / API key — only
                    # they can fix it): tell the owner too, with guidance.
                    # Platform-side failures stay admin-only.
                    if owner and user_actionable_failure(p.error_message or ""):
                        send_user_failure_email(
                            settings, to_email=owner.email, project_name=_pname(p),
                            error=p.error_message or "", project_url=url)
                logger.info(f"terminal notify: {p.id} ({p.status})")
            finally:
                try:
                    marker.write_text(p.status)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"terminal notify sweep failed: {e}")


def _reap_terminal_clusters(session, settings):
    """Reliable sweep: tear down a finished cloud orchestrator's VM after a grace
    period.

    Same rationale as _gc_terminal_envs / _notify_terminal_sweep: a SkyPilot
    orchestrator self-reports `done`/`failed` straight to the DB, so the poll-loop
    transition hook never observes running→terminal for it and nothing reaps its
    VM — today it lingers until the ~60-min autostop backstop bills out. This
    sweep `sky down`s the cluster CLUSTER_REAP_GRACE_MINUTES after the terminal
    transition (updated_at), do-once via a `.ark_cluster_reaped` marker.

    Only `skypilot:` handles own a real VM to reap — `local:` (pid) and SLURM
    (bare job id) handles have nothing to down, so they're skipped. `stopped` is
    excluded: a user Stop already tears the cluster down synchronously (routes.py),
    so its VM is already going. Autostop remains the crash-safety backstop if the
    webapp is down through the grace window.
    """
    from datetime import datetime, timedelta
    from .db import Project
    from sqlmodel import select
    try:
        now = datetime.utcnow()
        grace_cutoff = now - timedelta(minutes=CLUSTER_REAP_GRACE_MINUTES)
        # Lower bound so a fresh boot doesn't re-`sky down` ancient rows (their VMs
        # are long gone via autostop); comfortably covers the autostop backstop's
        # reach. The marker makes this idempotent regardless, but the bound keeps
        # the sweep from scanning/among historical terminals every cycle.
        window_start = now - timedelta(hours=6)
        rows = session.exec(
            select(Project).where(
                Project.status.in_(["done", "failed"]),
                Project.updated_at > window_start,
                Project.updated_at <= grace_cutoff,
            )
        ).all()
        for p in rows:
            handle = p.slurm_job_id or ""
            if not handle.startswith("skypilot:"):
                continue
            pdir = settings.projects_root / p.user_id / p.id
            if not pdir.is_dir():
                continue
            marker = pdir / ".ark_cluster_reaped"
            if marker.exists():
                continue
            try:
                # cancel() dispatches `sky down` on a daemon thread and returns
                # immediately; a cluster already gone (autostop reaped it first) is
                # a harmless no-op there. Marker after a successful dispatch =
                # do-once; if dispatch itself raised we skip the marker and retry
                # next cycle (autostop still backstops meanwhile).
                launcher_from_handle(handle, log_fn=logger.info).cancel(handle, pdir)
            except Exception as e:
                logger.error(f"cluster reap failed for {p.id}: {e}")
                continue
            try:
                marker.write_text(handle)
            except Exception:
                pass
            logger.info(
                f"cluster reap: {p.id} ({p.status}) → sky down {handle} "
                f"(grace {CLUSTER_REAP_GRACE_MINUTES}m elapsed)"
            )
    except Exception as e:
        logger.warning(f"cluster reap sweep failed: {e}")


def _stuck_watchdog(p, launcher, pdir):
    """Alert once if a running project's orchestrator log has been silent for
    more than STUCK_MINUTES. The launcher reports the newest log mtime (or None
    for backends with no local log to watch, e.g. cloud). Behavior-identical to
    the pre-Phase-4 per-branch watchdogs (local_*.out / slurm_*.out).

    ``update_project`` refreshes ``p`` in place, so after a transition p.status
    already equals the new status — no separate new_status arg needed."""
    if p.status != "running":
        return
    mtime = launcher.latest_log_mtime(pdir)
    if mtime is None:
        return
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


async def _poll_jobs(app: FastAPI):
    """Background task: poll SLURM job states every 60 s."""
    from .db import get_running_projects, get_session, update_project, get_user
    settings = get_settings()

    while True:
        try:
            await asyncio.sleep(60)
            # Control-plane timeout enforcement (D1/D4): expire decisions past
            # their deadline even if the Telegram daemon isn't running, so a run
            # whose orchestrator died mid-decision can't hang a pending question.
            try:
                from . import hitl
                with get_session(settings.db_path) as session:
                    hitl.sweep(session)
            except Exception:
                pass
            with get_session(settings.db_path) as session:
                projects = get_running_projects(session)
                for p in projects:
                    if not p.slurm_job_id:
                        continue

                    pdir = settings.projects_root / p.user_id / p.id
                    from .constants import DASHBOARD_PREFIX
                    url = f"{settings.base_url}{DASHBOARD_PREFIX}/#project/{p.id}"

                    try:
                        # ── Unified launcher dispatch (Phase 4) ──────────────────
                        # poll/cancel dispatch purely off the persisted handle
                        # (local:/cloud:/slurm) via the JobLauncher seam.
                        launcher = launcher_from_handle(p.slurm_job_id, log_fn=logger.info)
                        result = launcher.poll(p.slurm_job_id, pdir)
                        logger.debug(f"Poll {p.id}: {result.state} ({result.raw})")

                        # UNKNOWN → transient probe failure / no state yet. Leave the
                        # project as-is and retry next cycle (still run the watchdog).
                        if result.state == UNKNOWN:
                            _stuck_watchdog(p, launcher, pdir)
                            continue

                        # GONE → the remote process vanished with no authoritative
                        # outcome. The control-plane DB is the source of truth (the
                        # orchestrator self-reports its terminal status over /v1), so
                        # this is a crash-safety-net: mark failed only if the DB still
                        # shows the run active.
                        if result.state == GONE:
                            session.refresh(p)
                            if p.status not in ACTIVE_STATUSES:
                                continue  # orchestrator already recorded a terminal status
                            prev_status = p.status
                            update_project(session, p, status="failed")
                            logger.info(
                                f"Orchestrator {p.id}: {prev_status} → failed "
                                f"(remote process gone with no terminal report)"
                            )
                            _gc_project_env(pdir, p.id)
                            _advance_pending_queue(session, settings)
                            send_telegram_notify(
                                f"❌ <b>{_pname(p)}</b> failed\n<a href='{url}'>{url}</a>",
                                bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                            )
                            _log_mtimes.pop(p.id, None)
                            _stuck_alerted.discard(p.id)
                            continue

                        # Authoritative poll states: running / queued / done / failed / stopped.
                        new_status = result.state
                        if new_status != p.status:
                            # Auto-restart a job the cluster cancelled out from under us
                            # (SLURM only; local/cloud return None). User-initiated Stop
                            # sets the DB to "stopped" synchronously, so those never reach
                            # here as "running" → no false trigger.
                            if new_status == STOPPED:
                                spec = LaunchSpec(
                                    project_id=p.id, mode=p.mode,
                                    max_iterations=p.max_iterations,
                                    project_dir=pdir, log_dir=pdir / "logs", settings=settings,
                                )
                                try:
                                    restart = launcher.maybe_restart(p.slurm_job_id, spec)
                                except Exception as e:
                                    logger.error(f"Auto-restart failed for {p.id}: {e}")
                                    restart = None
                                if restart is not None:
                                    update_project(session, p, status="queued", slurm_job_id=restart.handle)
                                    logger.info(
                                        f"Auto-restarted {p.id}: new job {restart.handle} "
                                        f"(attempt {restart.attempt})"
                                    )
                                    send_telegram_notify(
                                        f"⚡ <b>{_pname(p)}</b> 自动重启（集群 cancel，第 {restart.attempt} 次）\n"
                                        f"新 Job: #{restart.handle}\n<a href='{url}'>{url}</a>",
                                        bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                                    )
                                    _log_mtimes.pop(p.id, None)
                                    _stuck_alerted.discard(p.id)
                                    continue  # skip normal stopped handling

                            kwargs = {"status": new_status}
                            if new_status == "failed":
                                # Local jobs surface the crash tail (possibly "") so a
                                # failure always overwrites any stale error_message;
                                # SLURM/cloud have no local log → read_error returns
                                # None and we leave error_message untouched.
                                err = launcher.read_error(pdir)
                                if err is not None:
                                    kwargs["error_message"] = err
                            update_project(session, p, **kwargs)
                            logger.info(f"Project {p.id}: {p.status} → {new_status}")
                            if new_status in ("done", "failed", "stopped"):
                                _gc_project_env(pdir, p.id)
                                _advance_pending_queue(session, settings)

                            if new_status == "running":
                                send_telegram_notify(
                                    f"🚀 <b>{_pname(p)}</b> started running\n<a href='{url}'>{url}</a>",
                                    bot_token=p.telegram_token, chat_id=p.telegram_chat_id,
                                )
                            elif new_status in ("done", "failed", "stopped"):
                                # done/failed emails + telegram are owned by
                                # _notify_terminal_sweep (reliable for self-
                                # reported terminals too); just clean trackers.
                                _log_mtimes.pop(p.id, None)
                                _stuck_alerted.discard(p.id)

                        # Stuck watchdog — projects that are (or just became) running.
                        _stuck_watchdog(p, launcher, pdir)
                    except Exception as _poll_err:
                        # Isolate per-project failures so one bad poll/finalize (DB lock,
                        # notify error) can't abort the whole cycle — restores the guard the
                        # old cloud branch had, now covering every backend.
                        logger.error(f"Poll failed for {p.id}: {_poll_err}")


                # Always try to advance queue at end of each poll cycle
                _advance_pending_queue(session, settings)
                # Reclaim disk from finished projects' conda envs (reliable sweep;
                # the transition-based GC above misses orchestrator-self-reported done)
                _gc_terminal_envs(session, settings)
                # DONE → owner email; FAILED → admin email (reliable sweep, same
                # rationale as the GC sweep)
                _notify_terminal_sweep(session, settings)
                # Reap finished cloud orchestrator VMs after a grace period. Same
                # reliable-sweep rationale: self-reported terminals bypass the
                # transition hook, so a cloud VM would otherwise linger until
                # autostop. Autostop stays the crash-safety backstop.
                _reap_terminal_clusters(session, settings)

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
                    from .routes import orchestrator_launcher_for
                    spec = LaunchSpec(
                        project_id=p.id, mode=proj.mode,
                        max_iterations=proj.max_iterations,
                        project_dir=pdir, log_dir=log_dir, settings=settings,
                    )
                    launcher = orchestrator_launcher_for(proj, spec, session, settings)
                    slurm_job_id = launcher.launch(spec)
                    update_project(session, proj, status=launcher.initial_status,
                                   slurm_job_id=slurm_job_id)
                    send_telegram_notify(
                        f"✅ Template installed! <b>{_pname(proj)}</b> {launcher.initial_status}.\n"
                        f"Job: #{slurm_job_id}",
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
    # Legacy pre-ORM sqlite fixup: adds a few columns + a one-time user-name
    # normalization on old single-node dev DBs. This is sqlite-only and runs
    # against a bare file path — it is SKIPPED for a DSN backend (Postgres),
    # where Alembic owns the schema and a DSN string must never be handed to
    # sqlite3.connect(). Alembic (_ensure_schema in get_engine) is the schema
    # source of truth for both backends.
    import sqlite3 as _sq3
    if "://" not in settings.db_path:
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
    # Create engine + bring schema to head via Alembic (sqlite dev + postgres).
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

    # Control loop = queue promotion + job polling + terminal-notify sweep +
    # Telegram daemon. Exactly ONE webapp process may run it per shared DB:
    # on 2026-07-07 the dev and prod webapps (sharing one DB) both promoted
    # the same pending projects within the same second and double-launched
    # them — unit-name collision, child-process fallback, live runs falsely
    # marked failed. Secondary instances (dev) set ARK_CONTROL_LOOP=0 and
    # serve UI/API only.
    control_loop = os.environ.get("ARK_CONTROL_LOOP", "1") != "0"
    poll_task = None
    if control_loop:
        # Ensure the webapp launches AS the central launcher SA: SkyPilot's SDK
        # uses ADC (not the gcloud active account), so point ADC at the SA key if
        # unset. Warns loudly if it would otherwise launch as a user account.
        try:
            from website.dashboard.gcp_access import ensure_launcher_credentials
            ensure_launcher_credentials(settings)
        except Exception as e:
            logger.warning(f"Launcher credential check failed (non-fatal): {e}")
        # AWS analog: report which identity the launcher will assume tenant roles
        # as. Only meaningful when an AWS launcher is configured; skipped silently
        # otherwise so a GCP-only deployment logs nothing new.
        if settings.cloud_launcher_role_arn or settings.cloud_launcher_aws_credential_source:
            try:
                from website.dashboard.aws_access import ensure_launcher_credentials as _aws_ensure
                _aws_ensure(settings)
            except Exception as e:
                logger.warning(f"AWS launcher credential check failed (non-fatal): {e}")
        # Reconcile the SkyPilot per-user workspaces from the DB into the host's
        # ~/.sky/config.yaml, so launches target each user's GCP project after a
        # restart (settings-save keeps it current thereafter). Gated on the
        # control-loop owner: only the process that launches jobs writes the host
        # sky config, so a UI-only secondary can't race it. Best-effort.
        try:
            from website.dashboard.skyworkspaces import (
                render_sky_workspaces, render_aws_profiles)
            n = render_sky_workspaces(settings.db_path)
            logger.info(f"Reconciled {n} SkyPilot workspace(s) at startup.")
            # AWS analog: reconcile the per-user ~/.aws profiles the AWS workspaces
            # reference, so AWS launches resolve their tenant role after a restart.
            m = render_aws_profiles(settings.db_path)
            logger.info(f"Reconciled {m} AWS profile(s) at startup.")
        except Exception as e:
            logger.warning(f"SkyPilot workspace reconcile failed (non-fatal): {e}")
        # Start the Telegram daemon — the control-plane HITL engine (D1). It is the
        # sole Telegram poller: it notifies opened decisions, captures replies into the
        # decision/command queues, and sweeps timeouts. No-op if Telegram unconfigured.
        try:
            from ark.telegram.daemon import ensure_daemon
            ensure_daemon()
        except Exception as e:
            logger.warning(f"Telegram daemon start failed (non-fatal): {e}")
        poll_task = asyncio.create_task(_poll_jobs(app))
    else:
        logger.info("ARK_CONTROL_LOOP=0 — control loop disabled; serving UI/API only.")
    yield
    if poll_task:
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
        title="Idea2Paper Dashboard",
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
    outer = FastAPI(title="Idea2Paper Research Portal", lifespan=lifespan)

    # /v1 control-plane API for orchestrators (token-auth, no session cookie).
    # On the outer app so it's a clean top-level path outside the dashboard.
    from .api import router as control_plane_router
    outer.include_router(control_plane_router)

    # Starlette's Mount matches /dashboard/ but NOT bare /dashboard (it
    # passes empty string to the sub-app which 404s). Register a redirect
    # BEFORE the mount so /dashboard → /dashboard/ works.
    from fastapi.responses import RedirectResponse as _Redir

    @outer.get(DASHBOARD_PREFIX)
    async def _dashboard_redirect():
        return _Redir(DASHBOARD_PREFIX + "/", status_code=301)

    # When dashboard is mounted under a parent prefix (e.g. "/dev/dashboard"),
    # also redirect the parent to the dashboard. This lets idea2paper.org/dev/
    # reach the dev dashboard without the user having to spell out the full
    # /dev/dashboard/ path.
    _parent = DASHBOARD_PREFIX.rsplit("/", 1)[0]  # "/dev/dashboard" → "/dev"
    if _parent and _parent != DASHBOARD_PREFIX:
        @outer.get(_parent)
        async def _parent_redirect():
            return _Redir(DASHBOARD_PREFIX + "/", status_code=301)

        @outer.get(_parent + "/")
        async def _parent_slash_redirect():
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
