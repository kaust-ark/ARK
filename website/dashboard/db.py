"""SQLite database models and session management (SQLModel)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, create_engine, delete, select


class User(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    google_id: Optional[str] = Field(default=None)
    email: str = Field(unique=True, index=True)
    name: str = ""
    picture: str = ""
    welcome_sent: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""
    encrypted_keys: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Access tracking — "who accessed": set on each successful dashboard login.
    last_login_at: Optional[datetime] = Field(default=None)
    login_count: int = 0


class AccessRequest(SQLModel, table=True):
    """A dashboard access request — "who requested" + "who authorized".

    Persisted when someone submits /api/request-access (previously the request
    was only emailed, then lost). Flipped to ``authorized`` by ``ark access
    add`` so the request -> grant trail is queryable.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    email: str = Field(index=True)
    name: str = ""
    affiliation: str = ""
    purpose: str = ""
    ip: str = ""
    status: str = "pending"          # pending | authorized | rejected
    created_at: datetime = Field(default_factory=datetime.utcnow)
    authorized_at: Optional[datetime] = Field(default=None)
    authorized_by: str = ""          # admin email, or "cli"


class Project(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_id: str = Field(index=True)
    name: str          # slug, e.g. "my-cool-paper"
    title: str = ""    # human title
    idea: str = ""
    venue: str = ""
    venue_format: str = ""
    venue_pages: int = 9
    layout_mode: str = "relaxed"  # page fitting: off | strict | relaxed
    max_iterations: int = 2       # review iterations
    max_dev_iterations: int = 3   # dev phase iterations
    mode: str = "paper"
    figure_generation: str = "nano_banana"  # nano_banana | matplotlib_only | none
    status: str = "queued"      # queued | running | done | failed | stopped
    slurm_job_id: str = ""
    pdf_path: str = ""
    score: float = 0.0
    has_pdf_upload: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # ── Config fields (previously in config.yaml) ──
    model: str = ""                  # backend: claude, gemini
    model_variant: str = ""          # e.g. claude-sonnet-4-6
    code_dir: str = ""               # workspace directory path
    language: str = "en"             # user language preference
    paper_accept_threshold: float = 8.0
    max_days: float = 3.0
    orchestrator_compute_backend: str = "local"
    experiment_compute_backend: str = "slurm"
    compute_backend: str = "slurm"   # Legacy, keep for backward compatibility
    source: str = "webapp"           # "webapp" or "cli"

    # ── Runtime status (previously in yaml state files) ──
    phase: str = ""                  # research | dev | review | accepted
    iteration: int = 0               # current review iteration
    dev_iteration: int = 0           # current dev phase iteration
    dev_status: str = ""             # pending | in_progress | completed
    score_history: str = ""          # JSON: [{iteration, score, timestamp}, ...]

    # ── Cost tracking (previously in cost_report.yaml) ──
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_agent_calls: int = 0

    # ── Checkpoint (previously in checkpoint.yaml) ──
    checkpoint_data: str = ""        # JSON blob: {run_id, iteration, step, ...}

    # ── Process tracking ──
    pid: int = 0

    # ── Failure info ──
    error_message: str = ""

    # ── Per-project cloud overrides ──
    cloud_overrides: str = ""   # JSON: {region, instance_type, image_id, ...}

    # ── HITL control plane ──
    # How much the orchestrator checks in with the human:
    #   full_auto      — only hard blockers / ethics / irreversible gates ask
    #   collaborative  — also ask at major forks (experiment, direction, drift)  [default]
    #   hands_on       — confirm each major step
    autonomy_level: str = "collaborative"
    activity: str = ""          # current one-line activity, for live display
    control_state: str = ""     # "" (running) | pausing | paused


class ProjectCommand(SQLModel, table=True):
    """Control-plane command from webapp/Telegram to a RUNNING orchestrator.

    The detached orchestrator polls pending commands at safe checkpoints and
    applies them (pause/resume/steer/answer/set_autonomy). This is the回程
    channel the webapp previously lacked — it could only signal-kill a run.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    project_id: str = Field(index=True)
    kind: str                   # pause | resume | stop | steer | answer | set_autonomy
    payload: str = ""           # steer text / autonomy value / decision id+answer (JSON)
    status: str = "pending"     # pending | consumed
    source: str = "webapp"      # webapp | telegram | cli
    created_by: str = ""        # email / "telegram" / "system"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    consumed_at: Optional[datetime] = Field(default=None)


class PendingDecision(SQLModel, table=True):
    """A decision the orchestrator is blocked on, answerable from EITHER the
    webapp or Telegram. Replaces the old Telegram-only ask_user_decision so a
    pending question is visible and answerable in both channels."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    project_id: str = Field(index=True)
    kind: str = "decision"      # decision | clarification | gate_a | experiment_approval | drift | blocker
    question: str = ""
    options: str = "[]"         # JSON list[str]
    context: str = ""           # extra detail (markdown ok)
    default_index: int = 0
    # On deadline with no answer: proceed_default (auto-pick default) or pause
    # (park the run — for ethics / expensive / irreversible decisions).
    timeout_action: str = "proceed_default"
    deadline_at: Optional[datetime] = Field(default=None)
    status: str = "pending"     # pending | answered | timed_out | cancelled
    answer_index: int = -1
    answer_text: str = ""
    answered_by: str = ""
    source: str = ""            # channel that answered: webapp | telegram | timeout
    created_at: datetime = Field(default_factory=datetime.utcnow)
    answered_at: Optional[datetime] = Field(default=None)


class Feedback(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_id: str = Field(index=True)
    project_id: str = ""          # optional association
    message: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ShareAlias(SQLModel, table=True):
    """Short URL alias for a share link.

    Lets operators hand out `/dashboard/share/icml` instead of a long signed
    token. The alias resolves to the same (kind, ident) payload a signed
    token would carry. Expiry is enforced per row; delete the row to revoke.
    """
    alias: str = Field(primary_key=True)   # validated against ALIAS_PATTERN
    kind: str                              # "project" | "user"
    ident: str                             # project.id or user.id
    expires_at: datetime                   # absolute UTC expiry
    created_by: str = ""                   # admin user_id who minted it, or "cli"
    created_at: datetime = Field(default_factory=datetime.utcnow)


# Conservative slug regex: lowercase alphanumeric start, then dash/underscore/alnum.
# Bounded to 2–64 chars. Chosen to avoid collision with signed tokens (which
# contain "." separators) and to keep the URL easy to share aloud.
ALIAS_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,63}$"


_engine = None


def _migrate(engine):
    """Run lightweight schema migrations for SQLite."""
    with engine.connect() as conn:
        # ── User table migrations ──
        rows = conn.execute(text("PRAGMA table_info(user)")).fetchall()
        existing = {row[1] for row in rows}
        if "encrypted_keys" not in existing:
            conn.execute(text("ALTER TABLE user ADD COLUMN encrypted_keys TEXT"))
            conn.commit()
        if "last_login_at" not in existing:
            conn.execute(text("ALTER TABLE user ADD COLUMN last_login_at TIMESTAMP"))
            conn.commit()
        if "login_count" not in existing:
            conn.execute(text("ALTER TABLE user ADD COLUMN login_count INTEGER DEFAULT 0"))
            conn.commit()

        # ── Project table migrations ──
        rows = conn.execute(text("PRAGMA table_info(project)")).fetchall()
        existing = {row[1] for row in rows}
        _new_cols = {
            # Config fields
            "model": "TEXT DEFAULT ''",
            "model_variant": "TEXT DEFAULT ''",
            "code_dir": "TEXT DEFAULT ''",
            "language": "TEXT DEFAULT 'en'",
            "paper_accept_threshold": "REAL DEFAULT 8.0",
            "max_days": "REAL DEFAULT 3.0",
            "orchestrator_compute_backend": "TEXT DEFAULT 'local'",
            "experiment_compute_backend": "TEXT DEFAULT 'slurm'",
            "compute_backend": "TEXT DEFAULT 'slurm'",
            "source": "TEXT DEFAULT 'webapp'",
            "layout_mode": "TEXT DEFAULT 'relaxed'",
            "figure_generation": "TEXT DEFAULT 'nano_banana'",
            # Runtime status
            "phase": "TEXT DEFAULT ''",
            "iteration": "INTEGER DEFAULT 0",
            "dev_iteration": "INTEGER DEFAULT 0",
            "dev_status": "TEXT DEFAULT ''",
            "score_history": "TEXT DEFAULT ''",
            # Cost tracking
            "total_cost_usd": "REAL DEFAULT 0.0",
            "total_input_tokens": "INTEGER DEFAULT 0",
            "total_output_tokens": "INTEGER DEFAULT 0",
            "total_agent_calls": "INTEGER DEFAULT 0",
            # Checkpoint
            "checkpoint_data": "TEXT DEFAULT ''",
            # Process
            "pid": "INTEGER DEFAULT 0",
            # Failure info
            "error_message": "TEXT DEFAULT ''",
            # Per-project cloud overrides
            "cloud_overrides": "TEXT DEFAULT ''",
            # HITL control plane
            "autonomy_level": "TEXT DEFAULT 'collaborative'",
            "activity": "TEXT DEFAULT ''",
            "control_state": "TEXT DEFAULT ''",
        }
        added = []
        for col, typedef in _new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE project ADD COLUMN {col} {typedef}"))
                added.append(col)
        if added:
            conn.commit()


def get_engine(db_path: str):
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
        SQLModel.metadata.create_all(_engine)
        _migrate(_engine)
    return _engine


def get_session(db_path: str):
    engine = get_engine(db_path)
    return Session(engine)


# ── helpers ──────────────────────────────────────────────────────────────────

def get_or_create_user_by_email(session: Session, email: str) -> tuple[User, bool]:
    """Return (user, is_new). ``is_new`` is True when the account was just created."""
    user = session.exec(select(User).where(User.email == email)).first()
    if user:
        return user, False
    name = email.split("@")[0].split(".")[0].capitalize()
    user = User(email=email, name=name)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user, True


def get_user(session: Session, user_id: str) -> Optional[User]:
    return session.get(User, user_id)


def touch_user_login(session: Session, user: User) -> None:
    """Record a successful dashboard login on the user ("who accessed")."""
    user.last_login_at = datetime.utcnow()
    user.login_count = (user.login_count or 0) + 1
    session.add(user)
    session.commit()


def record_access_request(session: Session, email: str, name: str = "",
                          affiliation: str = "", purpose: str = "",
                          ip: str = "") -> Optional["AccessRequest"]:
    """Persist an inbound access request ("who requested").

    Upserts a single pending row per email — re-requests refresh the existing
    pending row rather than piling up duplicates.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    existing = session.exec(
        select(AccessRequest).where(AccessRequest.email == email,
                                    AccessRequest.status == "pending")
    ).first()
    if existing:
        existing.created_at = datetime.utcnow()
        existing.name = name or existing.name
        existing.affiliation = affiliation or existing.affiliation
        existing.purpose = purpose or existing.purpose
        existing.ip = ip or existing.ip
        session.add(existing)
        session.commit()
        return existing
    req = AccessRequest(email=email, name=name, affiliation=affiliation,
                        purpose=purpose, ip=ip)
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


def mark_access_authorized(session: Session, emails: list[str],
                           by: str = "cli") -> int:
    """Mark these emails as authorized ("who authorized"). Returns rows touched.

    Flips any pending request to authorized; if an email has no request on file
    (a direct ``ark access add``), inserts an authorized row so the grant is
    still recorded.
    """
    n = 0
    for email in emails:
        email = (email or "").strip().lower()
        if not email:
            continue
        rows = session.exec(
            select(AccessRequest).where(AccessRequest.email == email,
                                        AccessRequest.status == "pending")
        ).all()
        if not rows:
            rows = [AccessRequest(email=email)]
        for r in rows:
            r.status = "authorized"
            r.authorized_at = datetime.utcnow()
            r.authorized_by = by
            session.add(r)
            n += 1
    if n:
        session.commit()
    return n


def list_access_requests(session: Session, status: Optional[str] = None) -> list["AccessRequest"]:
    """List access requests, newest first; optionally filter by status."""
    q = select(AccessRequest)
    if status:
        q = q.where(AccessRequest.status == status)
    return list(session.exec(q.order_by(AccessRequest.created_at.desc())))


# ── HITL control plane ────────────────────────────────────────────────────────
# These are the shared bus between the webapp/Telegram and the detached
# orchestrator. The orchestrator polls commands + answers at checkpoints; the
# webapp/Telegram enqueue commands and answer decisions.

def enqueue_command(session: Session, project_id: str, kind: str, payload: str = "",
                    source: str = "webapp", created_by: str = "") -> "ProjectCommand":
    """Queue a control command for a running project's orchestrator."""
    cmd = ProjectCommand(project_id=project_id, kind=kind, payload=payload,
                         source=source, created_by=created_by)
    session.add(cmd)
    session.commit()
    session.refresh(cmd)
    return cmd


def take_pending_commands(session: Session, project_id: str) -> list[dict]:
    """Return + mark-consumed all pending commands for a project (FIFO).

    Returns plain dicts so callers don't hold ORM objects past the session.
    """
    rows = session.exec(
        select(ProjectCommand)
        .where(ProjectCommand.project_id == project_id,
               ProjectCommand.status == "pending")
        .order_by(ProjectCommand.created_at)
    ).all()
    out = []
    for r in rows:
        out.append({"id": r.id, "kind": r.kind, "payload": r.payload,
                    "source": r.source, "created_by": r.created_by})
        r.status = "consumed"
        r.consumed_at = datetime.utcnow()
        session.add(r)
    if rows:
        session.commit()
    return out


def create_pending_decision(session: Session, project_id: str, question: str,
                            options: list[str], kind: str = "decision",
                            context: str = "", default_index: int = 0,
                            timeout_action: str = "proceed_default",
                            deadline_at: Optional[datetime] = None) -> str:
    """Open a decision for the human. Cancels any prior open decision for the
    project first (one live question at a time). Returns the decision id."""
    for old in session.exec(
        select(PendingDecision).where(PendingDecision.project_id == project_id,
                                      PendingDecision.status == "pending")
    ).all():
        old.status = "cancelled"
        session.add(old)
    dec = PendingDecision(
        project_id=project_id, kind=kind, question=question,
        options=json.dumps(list(options or [])), context=context,
        default_index=default_index, timeout_action=timeout_action,
        deadline_at=deadline_at,
    )
    session.add(dec)
    session.commit()
    session.refresh(dec)
    return dec.id


def get_open_decision(session: Session, project_id: str) -> Optional["PendingDecision"]:
    return session.exec(
        select(PendingDecision).where(PendingDecision.project_id == project_id,
                                      PendingDecision.status == "pending")
        .order_by(PendingDecision.created_at.desc())
    ).first()


def get_decision(session: Session, decision_id: str) -> Optional["PendingDecision"]:
    return session.get(PendingDecision, decision_id)


def answer_decision(session: Session, decision_id: str, index: int = -1,
                    text: str = "", by: str = "", source: str = "webapp") -> bool:
    """Record a human's answer to a decision. Returns False if already closed."""
    dec = session.get(PendingDecision, decision_id)
    if not dec or dec.status != "pending":
        return False
    dec.status = "answered"
    dec.answer_index = index
    dec.answer_text = text or ""
    dec.answered_by = by
    dec.source = source
    dec.answered_at = datetime.utcnow()
    session.add(dec)
    session.commit()
    return True


def expire_decision(session: Session, decision_id: str) -> None:
    dec = session.get(PendingDecision, decision_id)
    if dec and dec.status == "pending":
        dec.status = "timed_out"
        dec.source = "timeout"
        dec.answered_at = datetime.utcnow()
        session.add(dec)
        session.commit()


def set_activity(session: Session, project_id: str, text: str) -> None:
    p = session.get(Project, project_id)
    if p is not None:
        p.activity = (text or "")[:300]
        session.add(p)
        session.commit()


def set_control_state(session: Session, project_id: str, state: str) -> None:
    p = session.get(Project, project_id)
    if p is not None:
        p.control_state = state or ""
        session.add(p)
        session.commit()


def set_autonomy(session: Session, project_id: str, level: str) -> None:
    if level not in ("full_auto", "collaborative", "hands_on"):
        return
    p = session.get(Project, project_id)
    if p is not None:
        p.autonomy_level = level
        session.add(p)
        session.commit()


def get_projects_for_user(session: Session, user_id: str) -> list[Project]:
    return list(session.exec(select(Project).where(Project.user_id == user_id)
                             .order_by(Project.created_at.desc())).all())


def get_project(session: Session, project_id: str) -> Optional[Project]:
    return session.get(Project, project_id)


def create_project(session: Session, **kwargs) -> Project:
    project = Project(**kwargs)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def update_project(session: Session, project: Project, **kwargs) -> Project:
    for k, v in kwargs.items():
        setattr(project, k, v)
    project.updated_at = datetime.utcnow()
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def delete_project(session: Session, project_id: str) -> int:
    """Delete a project row by ID. Returns number of rows deleted."""
    result = session.exec(delete(Project).where(Project.id == project_id))
    session.commit()
    return result.rowcount


def get_all_projects(session: Session) -> list[Project]:
    return list(session.exec(select(Project).order_by(Project.created_at.desc())).all())


def get_running_projects(session: Session) -> list[Project]:
    return list(session.exec(
        select(Project).where(Project.status.in_(["queued", "running", "pending", "initializing"]))
    ).all())


def get_waiting_template_projects(session: Session) -> list[Project]:
    return list(session.exec(
        select(Project).where(Project.status == "waiting_template")
    ).all())


# ── feedback helpers ────────────────────────────────────────────────────────

def create_feedback(session: Session, **kwargs) -> Feedback:
    fb = Feedback(**kwargs)
    session.add(fb)
    session.commit()
    session.refresh(fb)
    return fb


def get_feedbacks_for_user(session: Session, user_id: str) -> list[Feedback]:
    return list(session.exec(
        select(Feedback).where(Feedback.user_id == user_id)
        .order_by(Feedback.created_at.desc())
    ).all())


def get_all_feedbacks(session: Session) -> list[Feedback]:
    return list(session.exec(
        select(Feedback).order_by(Feedback.created_at.desc())
    ).all())


def migrate_project_data(db_path: str, projects_root: str = ""):
    """Populate new DB columns from existing YAML state files.

    Called once at webapp startup. For each project in the DB that has
    empty new-columns (phase, score, etc.), reads the YAML state files
    and fills in the data. Also discovers CLI projects not in the DB.
    """
    import json
    from pathlib import Path
    try:
        import yaml
    except ImportError:
        return

    engine = get_engine(db_path)
    with Session(engine) as session:
        projects = session.exec(select(Project)).all()
        for p in projects:
            # Skip if already migrated (has phase or score data)
            if p.phase and p.score > 0:
                continue

            # Resolve project directory
            if p.code_dir:
                pdir = Path(p.code_dir)
            elif projects_root:
                pdir = Path(projects_root) / p.user_id / p.id
            else:
                continue

            state_dir = pdir / "auto_research" / "state"
            if not state_dir.exists():
                continue

            updates = {}

            # config.yaml → model, model_variant, code_dir
            cfg_file = pdir / "config.yaml"
            if cfg_file.exists() and not p.model:
                try:
                    cfg = yaml.safe_load(cfg_file.read_text()) or {}
                    updates["model"] = cfg.get("model", "")
                    updates["model_variant"] = cfg.get("model_variant", "")
                    if not p.code_dir:
                        updates["code_dir"] = cfg.get("code_dir", str(pdir))
                    updates["language"] = cfg.get("language", "en")
                except Exception:
                    pass

            # paper_state.yaml → score, iteration, phase, score_history
            ps_file = state_dir / "paper_state.yaml"
            if ps_file.exists():
                try:
                    ps = yaml.safe_load(ps_file.read_text()) or {}
                    score = ps.get("current_score")
                    if score is not None and not p.score:
                        updates["score"] = float(score)
                    reviews = ps.get("reviews", [])
                    if reviews:
                        updates["iteration"] = int(reviews[-1].get("iteration", len(reviews)))
                        updates["score_history"] = json.dumps([
                            {"iteration": r.get("iteration", i + 1),
                             "score": float(r.get("score", 0)),
                             "timestamp": r.get("timestamp", "")}
                            for i, r in enumerate(reviews)
                            if r and r.get("score") is not None
                        ])
                    paper_status = ps.get("status", "")
                    if paper_status in ("accepted", "accepted_pending_cleanup"):
                        updates["phase"] = "accepted"
                    elif reviews:
                        updates["phase"] = "review"
                except Exception:
                    pass

            # dev_phase_state.yaml → dev_iteration, dev_status
            ds_file = state_dir / "dev_phase_state.yaml"
            if ds_file.exists():
                try:
                    ds = yaml.safe_load(ds_file.read_text()) or {}
                    updates["dev_iteration"] = int(ds.get("iteration", 0))
                    updates["dev_status"] = ds.get("status", "")
                    if not updates.get("phase"):
                        if ds.get("status") == "in_progress":
                            updates["phase"] = "dev"
                        elif ds.get("status") in ("completed", "complete"):
                            updates["phase"] = "review"
                except Exception:
                    pass

            # cost_report.yaml → totals
            cr_file = state_dir / "cost_report.yaml"
            if cr_file.exists() and not p.total_cost_usd:
                try:
                    cr = yaml.safe_load(cr_file.read_text()) or {}
                    updates["total_cost_usd"] = float(cr.get("total_cost_usd", 0))
                    updates["total_input_tokens"] = int(cr.get("total_input_tokens", 0))
                    updates["total_output_tokens"] = int(cr.get("total_output_tokens", 0))
                    updates["total_agent_calls"] = int(cr.get("total_agent_calls", 0))
                except Exception:
                    pass

            # checkpoint.yaml → checkpoint_data
            ck_file = state_dir / "checkpoint.yaml"
            if ck_file.exists() and not p.checkpoint_data:
                try:
                    ck = yaml.safe_load(ck_file.read_text()) or {}
                    updates["checkpoint_data"] = json.dumps(ck)
                except Exception:
                    pass

            if updates:
                for k, v in updates.items():
                    setattr(p, k, v)
                p.updated_at = datetime.utcnow()
                session.add(p)

        session.commit()


# ── share alias helpers ────────────────────────────────────────────────────

def get_share_alias(session: Session, alias: str) -> Optional[ShareAlias]:
    return session.get(ShareAlias, alias)


def list_share_aliases(session: Session) -> list[ShareAlias]:
    return list(session.exec(select(ShareAlias).order_by(ShareAlias.created_at.desc())).all())


def upsert_share_alias(session: Session, alias: str, kind: str, ident: str,
                       expires_at: datetime, created_by: str = "") -> ShareAlias:
    """Insert or replace an alias row. Overwriting is intentional — the CLI
    treats `--alias foo` as "point foo at this target"."""
    existing = session.get(ShareAlias, alias)
    if existing:
        existing.kind = kind
        existing.ident = ident
        existing.expires_at = expires_at
        if created_by:
            existing.created_by = created_by
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    row = ShareAlias(alias=alias, kind=kind, ident=ident,
                     expires_at=expires_at, created_by=created_by)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_share_alias(session: Session, alias: str) -> int:
    result = session.exec(delete(ShareAlias).where(ShareAlias.alias == alias))
    session.commit()
    return result.rowcount


def get_project_by_name(session: Session, name: str) -> Optional[Project]:
    """Look up a project by its slug name. Returns the most recent match."""
    return session.exec(
        select(Project).where(Project.name == name)
        .order_by(Project.created_at.desc())
    ).first()


# ── DB path resolution ─────────────────────────────────────────────────────

def resolve_db_path() -> str:
    """Resolve the webapp DB path from env / config, usable from any context."""
    import os
    from pathlib import Path
    p = os.environ.get("ARK_WEBAPP_DB_PATH")
    if p:
        return p
    # Try loading from webapp.env
    try:
        from ark.paths import get_config_dir
        env_file = get_config_dir() / "webapp.env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("DB_PATH="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    # Fallback
    try:
        from ark.paths import get_ark_root
        return str(get_ark_root() / ".ark" / "data" / "webapp.db")
    except Exception:
        return ""
