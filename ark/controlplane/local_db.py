"""In-process control-plane client backed by the webapp SQLite DB.

This is the ONLY module under ``ark/`` that imports ``website.dashboard.db``. It
exists so single-node dev and SLURM-on-a-shared-DB keep working with zero new
infra while the HTTP boundary is built out. It will be deleted once remote
hosting (an out-of-process control plane) is the only supported path.

Behavior is a faithful port of the old ``Orchestrator._sync_db`` / ``_hitl_db``:
lazy import with a repo-root sys.path shim (the pipeline chdirs into the project
dir), a one-time sqlalchemy availability check, and fail-soft error counting that
logs the first few failures then goes quiet.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .base import ControlPlaneClient
from .types import Command, DecisionView, ProjectView

# Repo root = the dir containing both ``ark/`` and ``website/``. Needed on
# sys.path so ``website.dashboard.db`` imports even after a chdir.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])


def _default_log(msg: str, level: str = "INFO") -> None:
    print(f"[{level}] {msg}")


def default_db_path() -> Optional[str]:
    """Best-effort discovery of the webapp DB path (explicit arg > env > default).

    Wraps ``website.dashboard.db.resolve_db_path`` so bootstrap code doesn't
    import the webapp directly. Returns None if the webapp deps are absent
    (e.g. running on a remote VM)."""
    try:
        if _REPO_ROOT not in sys.path:
            sys.path.insert(0, _REPO_ROOT)
        from website.dashboard.db import resolve_db_path
        return resolve_db_path()
    except Exception:
        return None


def resolve_project_id_by_name(db_path: str, name: str) -> Optional[str]:
    """Resolve a project id from a name (or a bare id). Legacy CLI/bootstrap
    convenience used only on the LocalDb path."""
    try:
        if _REPO_ROOT not in sys.path:
            sys.path.insert(0, _REPO_ROOT)
        from website.dashboard.db import get_session, get_project_by_name, get_project
        with get_session(db_path) as session:
            p = get_project_by_name(session, name) or get_project(session, name)
            return p.id if p else None
    except Exception:
        return None


class LocalDbControlPlaneClient(ControlPlaneClient):
    def __init__(self, db_path: str, project_id: str, log_fn=None):
        self._db_path = db_path
        self._project_id = project_id
        self.log = log_fn or _default_log
        self._db_module = None       # cached website.dashboard.db module
        self._import_failed = False  # sqlalchemy/webapp deps unavailable
        self._errors = 0

    # ── internals ───────────────────────────────────────────────────────────────
    def _db(self):
        """Lazily import and cache ``website.dashboard.db``; None if unavailable."""
        if self._import_failed:
            return None
        if self._db_module is not None:
            return self._db_module
        try:
            import sqlalchemy  # noqa: F401 — availability check (mirrors old _sync_db)
        except ImportError:
            self._import_failed = True
            return None
        try:
            if _REPO_ROOT not in sys.path:
                sys.path.insert(0, _REPO_ROOT)
            from website.dashboard import db as _db
            self._db_module = _db
            return _db
        except Exception:
            self._import_failed = True
            return None

    def _note_error(self, e: Exception) -> None:
        self._errors += 1
        if self._errors <= 3:
            self.log(f"control-plane (localdb) call failed ({self._errors}): {e}", "WARN")

    # ── capability ──────────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return bool(self._db_path and self._project_id and self._db() is not None)

    # ── bootstrap / reads ────────────────────────────────────────────────────────
    def fetch_project(self) -> Optional[ProjectView]:
        db = self._db()
        if not (db and self._db_path and self._project_id):
            return None
        try:
            with db.get_session(self._db_path) as s:
                p = db.get_project(s, self._project_id)
                if p is None:
                    return None
                return ProjectView(
                    id=p.id,
                    name=getattr(p, "name", "") or "",
                    autonomy_level=getattr(p, "autonomy_level", "") or "collaborative",
                    status=getattr(p, "status", "") or "",
                    phase=getattr(p, "phase", "") or "",
                    raw=dict(p.__dict__) if hasattr(p, "__dict__") else {},
                )
        except Exception as e:
            self._note_error(e)
            return None

    def get_autonomy(self) -> Optional[str]:
        db = self._db()
        if not (db and self._db_path and self._project_id):
            return None
        try:
            with db.get_session(self._db_path) as s:
                p = db.get_project(s, self._project_id)
                return (p.autonomy_level or None) if p else None
        except Exception:
            return None

    # ── status writes ─────────────────────────────────────────────────────────────
    def report_status(self, **fields) -> None:
        db = self._db()
        if not (db and self._db_path and self._project_id and fields):
            return
        try:
            with db.get_session(self._db_path) as s:
                p = db.get_project(s, self._project_id)
                if p:
                    db.update_project(s, p, **fields)
            self._errors = 0
        except Exception as e:
            self._note_error(e)

    def set_activity(self, text: str) -> None:
        db = self._db()
        if not (db and self._db_path and self._project_id):
            return
        try:
            with db.get_session(self._db_path) as s:
                db.set_activity(s, self._project_id, text)
        except Exception:
            pass

    def set_control_state(self, state: str) -> None:
        db = self._db()
        if not (db and self._db_path and self._project_id):
            return
        try:
            with db.get_session(self._db_path) as s:
                db.set_control_state(s, self._project_id, state)
        except Exception:
            pass

    def set_autonomy(self, level: str) -> None:
        db = self._db()
        if not (db and self._db_path and self._project_id):
            return
        try:
            with db.get_session(self._db_path) as s:
                db.set_autonomy(s, self._project_id, level)
        except Exception:
            pass

    # ── commands ──────────────────────────────────────────────────────────────────
    def poll_commands(self) -> list[Command]:
        db = self._db()
        if not (db and self._db_path and self._project_id):
            return []
        try:
            with db.get_session(self._db_path) as s:
                rows = db.take_pending_commands(s, self._project_id)
            return [Command(id=r.get("id", ""), kind=r.get("kind", ""),
                            payload=r.get("payload", ""), source=r.get("source", "webapp"),
                            created_by=r.get("created_by", "")) for r in rows]
        except Exception:
            return []

    def ack_command(self, cmd_id: str) -> None:
        # LocalDb consumes on read (take_pending_commands), so ack is a no-op.
        pass

    # ── conversation ────────────────────────────────────────────────────────────────
    def append_message(self, role: str, text: str, kind: str = "message",
                       meta: Optional[dict] = None) -> None:
        db = self._db()
        if not (db and self._db_path and self._project_id):
            return
        try:
            with db.get_session(self._db_path) as s:
                db.add_message(s, self._project_id, role, text, kind=kind, meta=meta)
        except Exception:
            pass

    # ── decisions ─────────────────────────────────────────────────────────────────
    def open_decision(self, question: str, options: list[str], *,
                      kind: str = "decision", context: str = "",
                      default_index: int = 0,
                      timeout_action: str = "proceed_default",
                      deadline_at: Optional[datetime] = None) -> Optional[str]:
        db = self._db()
        if not (db and self._db_path and self._project_id):
            return None
        try:
            with db.get_session(self._db_path) as s:
                return db.create_pending_decision(
                    s, self._project_id, question, options, kind=kind,
                    context=context, default_index=default_index,
                    timeout_action=timeout_action, deadline_at=deadline_at)
        except Exception as e:
            self._note_error(e)
            return None

    def get_decision(self, decision_id: str) -> Optional[DecisionView]:
        db = self._db()
        if not (db and self._db_path):
            return None
        try:
            with db.get_session(self._db_path) as s:
                dec = db.get_decision(s, decision_id)
                if dec is None:
                    return None
                return DecisionView(
                    id=dec.id, status=dec.status,
                    answer_index=dec.answer_index if dec.answer_index is not None else -1,
                    answer_text=dec.answer_text or "",
                    source=getattr(dec, "source", "") or "",
                )
        except Exception:
            return None

    # ── live output (no-ops on shared-FS single-node) ───────────────────────────────
    def append_events(self, lines: list[dict]) -> None:
        # Single-node dashboard reads agent_steps.jsonl from disk directly.
        pass

    def register_artifact(self, **ref) -> None:
        # Phase 3 wires real object storage; LocalDb serves from the shared FS.
        pass
