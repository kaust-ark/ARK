"""Phase 2 — control-plane DB backend: DSN selection, Alembic bootstrap, adoption.

Covers the sqlite-or-DSN seam added so the control plane can run on Postgres for
concurrent remote orchestrators while keeping sqlite as the local-dev default:

  * ``_normalize_url`` / ``resolve_db_path`` treat a value with ``://`` as a full
    DSN and everything else as a sqlite file path.
  * ``get_engine`` brings a fresh DB to head via Alembic (no more create_all).
  * A pre-Alembic (legacy) sqlite DB is *adopted* (stamped at head), not rebuilt.

The Postgres concurrency/load test runs only when ``ARK_TEST_DATABASE_URL`` is
set (e.g. ``postgresql+psycopg://user:pass@host/db``); it is skipped otherwise so
the offline suite stays green.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

import website.dashboard.db as db


@pytest.fixture(autouse=True)
def _reset_engine(monkeypatch):
    """get_engine caches a module-global engine; reset around every test."""
    monkeypatch.setattr(db, "_engine", None, raising=False)
    yield
    monkeypatch.setattr(db, "_engine", None, raising=False)


def _alembic_head() -> str:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(db._alembic_config("sqlite://")).get_current_head()


# ── URL normalization / resolution ──────────────────────────────────────────

def test_normalize_url_path_becomes_sqlite():
    # Relative path → three slashes; absolute path keeps its leading "/" (→ four).
    assert db._normalize_url("webapp.db") == "sqlite:///webapp.db"
    assert db._normalize_url("/tmp/webapp.db") == "sqlite:////tmp/webapp.db"


def test_normalize_url_dsn_passthrough():
    dsn = "postgresql+psycopg://u:p@host:5432/ark_cp"
    assert db._normalize_url(dsn) == dsn
    assert db._normalize_url("sqlite:////abs/path.db") == "sqlite:////abs/path.db"


def test_resolve_db_path_prefers_database_url(monkeypatch):
    monkeypatch.setenv("ARK_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("ARK_WEBAPP_DB_PATH", "/tmp/should-be-ignored.db")
    assert db.resolve_db_path() == "postgresql+psycopg://u:p@h/db"
    assert db.resolve_db_url() == "postgresql+psycopg://u:p@h/db"


def test_resolve_db_path_sqlite_path_when_no_dsn(monkeypatch):
    monkeypatch.delenv("ARK_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ARK_WEBAPP_DB_PATH", "/tmp/dev.db")
    assert db.resolve_db_path() == "/tmp/dev.db"
    assert db.resolve_db_url() == "sqlite:////tmp/dev.db"


# ── Alembic bootstrap on a fresh sqlite DB ──────────────────────────────────

def test_fresh_sqlite_boots_through_alembic(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    with db.get_session(db_path) as s:
        user, _ = db.get_or_create_user_by_email(s, "fresh@example.com")
        assert user.id
    insp = inspect(db.get_engine(db_path))
    tables = set(insp.get_table_names())
    # Alembic stamped its version table and created the app schema.
    assert "alembic_version" in tables
    assert {"user", "project", "pendingdecision"} <= tables
    with db.get_engine(db_path).connect() as c:
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert ver == _alembic_head()


# ── Adoption of a pre-Alembic (legacy) sqlite DB ────────────────────────────

def test_legacy_db_is_adopted_not_rebuilt(tmp_path):
    """A DB created by the old create_all path has the schema but no
    alembic_version. get_engine must STAMP it (preserving existing rows), not run
    the baseline migration (which would collide with the existing tables)."""
    db_path = str(tmp_path / "legacy.db")
    legacy = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(legacy)  # old-style bootstrap, no Alembic
    from sqlmodel import Session
    with Session(legacy) as s:
        u, _ = db.get_or_create_user_by_email(s, "legacy@example.com")
        legacy_uid = u.id
    legacy.dispose()

    # No alembic_version yet — this is the pre-Alembic shape.
    assert "alembic_version" not in set(inspect(create_engine(f"sqlite:///{db_path}")).get_table_names())

    # Now boot through get_engine — must adopt without error and keep the row.
    eng = db.get_engine(db_path)
    tables = set(inspect(eng).get_table_names())
    assert "alembic_version" in tables
    with eng.connect() as c:
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert ver == _alembic_head()
    with db.get_session(db_path) as s:
        assert db.get_user(s, legacy_uid) is not None  # data preserved (stamp, not rebuild)


# ── Postgres concurrency / load test (gated) ────────────────────────────────

_PG_URL = os.environ.get("ARK_TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="set ARK_TEST_DATABASE_URL to run the Postgres load test")
def test_postgres_concurrent_orchestrators():
    """N simultaneous clients write + read through one pooled engine, no errors.

    Proves the control plane serves many concurrent remote orchestrators on
    Postgres — the core Phase 2 acceptance criterion."""
    import threading

    assert db._normalize_url(_PG_URL) == _PG_URL  # must be a real DSN
    eng = db.get_engine(_PG_URL)
    assert eng.dialect.name == "postgresql"

    run = uuid.uuid4().hex[:8]  # unique per run → no unique-email collisions
    N = 25
    errors: list = []
    made: list = []
    barrier = threading.Barrier(N)

    def client(i: int):
        try:
            barrier.wait()  # release all threads at once for max pool contention
            with db.get_session(_PG_URL) as s:
                u, _ = db.get_or_create_user_by_email(s, f"{run}-load{i}@example.com")
                p = db.create_project(s, user_id=u.id, name=f"{run}-proj-{i}", status="running")
                made.append(p.id)
            with db.get_session(_PG_URL) as s:
                db.get_running_projects(s)
        except Exception as e:  # noqa: BLE001
            errors.append((i, repr(e)))

    threads = [threading.Thread(target=client, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(set(made)) == N
