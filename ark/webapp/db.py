"""SQLite database models and session management (SQLModel)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, delete, select


class User(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    google_id: Optional[str] = Field(default=None)
    email: str = Field(unique=True, index=True)
    name: str = ""
    picture: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Project(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_id: str = Field(index=True)
    name: str          # slug, e.g. "my-cool-paper"
    title: str = ""    # human title
    idea: str = ""
    venue: str = ""
    venue_format: str = ""
    venue_pages: int = 9
    max_iterations: int = 3
    mode: str = "paper"
    status: str = "queued"      # queued | running | done | failed | stopped
    slurm_job_id: str = ""
    pdf_path: str = ""
    score: float = 0.0
    has_pdf_upload: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


_engine = None


def get_engine(db_path: str):
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
        SQLModel.metadata.create_all(_engine)
    return _engine


def get_session(db_path: str):
    engine = get_engine(db_path)
    return Session(engine)


# ── helpers ──────────────────────────────────────────────────────────────────

def get_or_create_user_by_email(session: Session, email: str) -> User:
    user = session.exec(select(User).where(User.email == email)).first()
    if user:
        return user
    name = email.split("@")[0].split(".")[0].capitalize()
    user = User(email=email, name=name)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def get_user(session: Session, user_id: str) -> Optional[User]:
    return session.get(User, user_id)


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
        select(Project).where(Project.status.in_(["queued", "running", "pending"]))
    ).all())


def get_waiting_template_projects(session: Session) -> list[Project]:
    return list(session.exec(
        select(Project).where(Project.status == "waiting_template")
    ).all())
