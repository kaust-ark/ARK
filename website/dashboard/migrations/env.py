"""Alembic migration environment for the ARK control-plane database.

Schema source of truth for BOTH backends (Phase 2): the sqlite dev database and
the deployed Postgres control plane are migrated through the same revisions. The
target metadata is ``SQLModel.metadata`` — importing ``website.dashboard.db``
registers every table on it.

The connection URL is taken from alembic's ``sqlalchemy.url`` when set (the app
sets it programmatically; the CLI can set it via ``-x`` or ``alembic.ini``) and
otherwise falls back to the app's own resolver so ``alembic upgrade head`` works
with just ``ARK_DATABASE_URL`` / ``DB_PATH`` in the environment.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing the db module registers all SQLModel tables on SQLModel.metadata.
import website.dashboard.db  # noqa: F401
from sqlmodel import SQLModel

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        # No logging config (e.g. programmatic Config with no ini) — fine.
        pass

target_metadata = SQLModel.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    # CLI fallback: resolve the same way the app does.
    from website.dashboard.db import resolve_db_url

    return resolve_db_url()


def run_migrations_offline() -> None:
    url = _url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # sqlite can't ALTER in place — batch mode rewrites the table.
        render_as_batch=url.startswith("sqlite"),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
