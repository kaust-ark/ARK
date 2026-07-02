# ADR-0013 — Control-plane state as a DB projection of orchestrator-local files

- **Status:** Proposed (Phase 3, `feat/byoc-cloud-backend`)
- **Date:** 2026-07-02
- **Deciders:** ARK core
- **Related:** [`../CLOUD_BACKEND_PLAN.md`](../CLOUD_BACKEND_PLAN.md) §Phase 3; [ADR-0003](0003-http-v1-control-plane-boundary.md); [ADR-0007](0007-checkpoint-resume-ownership.md); [ADR-0012](0012-artifact-store-seam.md)

## Context

The orchestrator maintains its run state as YAML under `auto_research/state/`. The
dashboard reaches some of it by reading those files off local disk — viable today only
because of the rsync-back bridge ([ADR-0012](0012-artifact-store-seam.md)) that Phase 3
removes. A precise trace of *who consumes what* changes the shape of the fix:

- **`paper_state.yaml`** — its live fields (`current_score`, `score_history`,
  `iteration`, `phase`, `status`) **already flow to the control plane** via the Phase-1
  status endpoint (`POST /v1/projects/{id}/status`) into `Project` columns;
  `save_paper_state()` already calls `_sync_db()` (`ark/orchestrator/core.py:1867`). The
  disk reads in `_read_project_score()` / `_read_score_history()` /
  `_read_current_iteration()` (`website/dashboard/routes.py:903–960`) are **legacy
  fallback**.
- **`findings.yaml`, `action_plan.yaml`, `memory.yaml`** — the **only** dashboard
  consumer is the export ZIP (`routes.py:3119–3128`). Not rendered live, no API exposes
  them.
- **`agent_steps.jsonl`** — **not consumed by the dashboard at all** (not even in the
  ZIP); it is purely orchestrator-local observability.

Crucially, the orchestrator **reads several of these files back for its own crash
recovery** — resuming iteration from `paper_state.yaml` (`core.py:1674`) and reloading
`action_plan.yaml` (`ark/execution.py:205`). The coupling Phase 3 removes is *the
control plane reading the orchestrator's disk* — **not** the orchestrator using local
files on its own VM, which is not "shared" and is consistent with keeping
checkpoint/resume data orchestrator-local ([ADR-0007](0007-checkpoint-resume-ownership.md)).

## Decision

We will treat control-plane state as a **projection** pushed by the orchestrator, not
as the authoritative store — mirroring how the event/log store already works (local
logs → pushed lines → `ProjectEvent` → dashboard reads DB).

- **Orchestrator keeps its local YAML unchanged** as its own working state and crash-
  recovery source. Phase 3 changes are **additive** (push calls), not a rewrite of
  `StateManager` / `memory.py` / `execution.py`.
- **One generic projection table.** `ProjectStateDoc(project_id, name, data: JSON,
  updated_at)` with a unique `(project_id, name)`, upserted via
  `PUT /v1/projects/{id}/state/{name}` (`name` ∈ {`paper_state`, `action_plan`,
  `findings`, `memory`}). Full-rewrite upsert matches how these files are written
  (whole-file rewrite per iteration/phase). `GET /v1/projects/{id}/state` returns all
  docs for ZIP assembly; `GET …/state/{name}` reads one (available for a future
  resume-from-DB, not required now).
- **`paper_state` live fields stay on the status endpoint;** the full `paper_state` doc
  is *also* pushed as a `ProjectStateDoc` so the ZIP is complete without disk.
- **Drop the dashboard's YAML-on-disk fallbacks** (`routes.py:903–960`, `3119–3128`)
  in favor of `Project` columns + `ProjectStateDoc`.
- **Exclude `agent_steps.jsonl`** from the projection — it has no dashboard consumer, so
  no table, no endpoint, no push (YAGNI).

## Consequences

- The dashboard renders project state and builds the export ZIP from the DB, with **no
  read of the orchestrator's disk** — unblocking rsync-bridge removal
  ([ADR-0012](0012-artifact-store-seam.md)).
- Orchestrator changes stay small and low-risk: local behavior (including crash
  recovery) is untouched; we only add fire-and-forget projection pushes (fail-soft, like
  the existing event flusher).
- New surface is minimal: one `ProjectStateDoc` model + Alembic revision and a small
  `/v1/state` handler; no per-document schema.
- "Projection, not source of truth" means a projection push can lag or be lost without
  corrupting a run — the local YAML remains authoritative. If a run resumes on a *fresh*
  VM (VM death), state must be rehydrated from the checkpoint mechanism, not this
  projection; that path is governed by [ADR-0007](0007-checkpoint-resume-ownership.md),
  not changed here.

## Alternatives considered

- **DB as the source of truth; remove local YAML from the orchestrator.** Rejected:
  invasive across `StateManager`/`memory.py`/`execution.py`, turns every state read into
  a network call, and risks crash-recovery correctness — all to remove a local-disk use
  that is not the shared-FS coupling in question.
- **Treat state files as artifact blobs** ([ADR-0012](0012-artifact-store-seam.md)
  store). Rejected: re-uploading a whole blob on every field change is chatty, the
  dashboard would have to download+parse a blob to show a score, the data isn't
  queryable, and object stores' eventual consistency is wrong for state the dashboard
  polls.
- **A dedicated table per document** (a `paper_state` table, a `findings` table, …).
  Rejected: a generic `name` + JSON table matches the uniform full-rewrite semantics
  with one migration and no schema churn as documents evolve.
- **A `ProjectStep` table for `agent_steps.jsonl`.** Rejected now: no consumer exists.
  Revisit if a live agent-activity feed is built.
