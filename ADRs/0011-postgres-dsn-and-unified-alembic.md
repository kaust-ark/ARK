# ADR-0011 — Postgres via a DSN-or-path seam, with one Alembic history for both backends

- **Status:** Implemented (Phase 2, `feat/byoc-cloud-backend`)
- **Date:** 2026-07-02
- **Deciders:** ARK core
- **Related:** [`../CLOUD_BACKEND_PLAN.md`](../CLOUD_BACKEND_PLAN.md) §Phase 2; [ADR-0001](0001-byoc-thin-control-plane.md); [ADR-0003](0003-http-v1-control-plane-boundary.md)

## Context

The control plane stored its state in **SQLite**, opened directly as a file
(`create_engine("sqlite:///…")`) and schema-evolved by a hand-rolled `_migrate()`
that `PRAGMA`s each table and `ALTER TABLE … ADD COLUMN`s the missing ones. SQLite is
single-node: one writer, a local file. Once the orchestrator runs remotely
([ADR-0003](0003-http-v1-control-plane-boundary.md) made it talk HTTP-only), a single
control plane must serve **many concurrent orchestrators**, which SQLite cannot do.

Constraints we must not break: every existing `get_session(settings.db_path)` caller
(65+ sites) keeps working; the SLURM path is untouched (it talks to the API, not the
DB); single-node local dev keeps working with **no new infrastructure**; and existing
developer sqlite DBs — created by the old `create_all` + `_migrate` path, with no
Alembic version table — must boot without a manual reset.

## Decision

We will make the DB location a **DSN-or-path** value and adopt **Alembic as the single
schema history for both backends**.

- **One value, two meanings.** `get_engine()` / `get_session()` / `resolve_db_path()`
  accept either a filesystem path (legacy sqlite) or a full SQLAlchemy DSN. A value
  containing `://` is used verbatim; anything else becomes `sqlite:///<path>`. So no
  caller signature changes — `settings.db_path` simply carries a DSN when Postgres is
  configured. `DATABASE_URL` / `ARK_DATABASE_URL` (env or `webapp.env`) select
  Postgres and take priority over `DB_PATH`.
- **Dialect-branched engine.** sqlite keeps today's exact engine construction;
  Postgres (or any client/server DB) gets a real pool (`pool_pre_ping`, `pool_size`,
  `pool_recycle`) so the control plane serves concurrent orchestrators.
- **Alembic owns the schema for both backends** (unified, not Postgres-only). One
  revision history under `website/dashboard/migrations/`; `get_engine()` brings the DB
  to head at boot. `_migrate()`'s `ALTER TABLE` set is captured in the baseline
  revision. Migrations are applied programmatically so a fresh install needs no manual
  `alembic` step.
- **Adopt, don't rebuild, legacy DBs.** A database that already has our tables but no
  `alembic_version` (the pre-Alembic shape) is **stamped at head** rather than run
  through the baseline (which would collide with the existing tables). Fresh and
  already-managed DBs run `upgrade head`.
- **The DSN never leaves the control plane.** Only the control-plane process opens DB
  connections; the remote orchestrator reaches state through `/v1`. The legacy
  `--db-path` subprocess channel stays sqlite-only and is left untouched.

## Consequences

- The control plane runs on Postgres and serves many concurrent remote orchestrators —
  verified live (Postgres 16, 25 concurrent clients, 0 errors) and with `alembic
  upgrade/downgrade/upgrade` proving up/down.
- Local dev stays on sqlite with zero new infrastructure; existing dev DBs are adopted
  silently.
- Schema changes are now real, reviewable, reversible migrations instead of an
  append-only `_migrate()`. The cost: contributors must generate a revision
  (`alembic revision --autogenerate`) when they change a model, and the baseline
  migration is metadata-shaped, so the first hand-written column change should be
  eyeballed on both dialects (sqlite uses batch mode).
- Adds `alembic` + `psycopg[binary]` to the `webapp` extra.
- The raw pre-ORM `sqlite3.connect()` fixup in the app lifespan is now guarded to run
  only for a sqlite backend, so a DSN string can never be handed to `sqlite3.connect()`.

## Alternatives considered

- **Alembic for Postgres only; keep `create_all` + `_migrate` for sqlite.** Lower risk,
  but leaves two divergent schema mechanisms and a dev/prod schema drift risk. Rejected
  in favor of a single source of truth; the legacy-adoption stamp neutralizes the main
  downside (existing sqlite DBs) cheaply.
- **A separate settings field / second code path for Postgres.** Rejected: it would
  fork all 65+ call sites. The DSN-or-path convention keeps one code path.
- **Managed/remote Postgres as a requirement.** Rejected as a Phase 2 concern: co-located
  Postgres satisfies the concurrency need; where Postgres physically runs is a
  deployment knob (the DSN), not an architectural choice.
- **psycopg2.** Rejected in favor of psycopg 3 (`postgresql+psycopg://`), the current
  driver line for SQLAlchemy 2.0.
