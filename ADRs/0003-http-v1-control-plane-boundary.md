# ADR-0003 — Replace shared SQLite/FS with an authenticated HTTP `/v1` boundary

- **Status:** Implemented (Phase 1, `feat/byoc-cloud-backend`)
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** [`../CONTROL_PLANE_BOUNDARY.md`](../CONTROL_PLANE_BOUNDARY.md); [ADR-0001](0001-byoc-thin-control-plane.md); commits `8d8114b`, `17a1d14`, `5da8c55`, `8dfdc06`

## Context

The orchestrator was bound to the control plane by **shared local resources**, not
an API: it was handed `--db-path` and imported `website.dashboard.db` to sync
status, poll `ProjectCommand`, and read/write `PendingDecision`; the dashboard read
the orchestrator's working files (state YAML, `agent_steps.jsonl`, PDFs) directly. A
remote orchestrator ([ADR-0001](0001-byoc-thin-control-plane.md)) cannot share a
SQLite file or a filesystem. This coupling is the linchpin that blocks every later
BYOC phase.

## Decision

We will route **all** orchestrator ↔ control-plane crossings through a single,
versioned, authenticated **HTTP `/v1` API** and a single code seam.

- **Interaction model: outbound-only, orchestrator-initiated.** The control plane
  never calls into the orchestrator; the orchestrator *reports* state up and *pulls*
  commands and decision answers. This is firewall-friendly (egress-only from the
  user's cloud) and matches today's polling design — we swap the transport
  (SQLite → HTTPS), not the model.
- **Ownership split:** the control plane is the system of record for identity, human
  interaction, and durable project metadata; the orchestrator is the system of record
  for the live run and the files it produces (surfaced up as *events* and *artifact
  references*, not via a shared mount).
- **Code seam:** a single `ControlPlaneClient` interface (`ark/controlplane/`). After
  Phase 1, nothing under `ark/orchestrator/` imports `website.dashboard.db` except the
  one `LocalDbControlPlaneClient` adapter. Three implementations selected at launch:
  - `HttpControlPlaneClient` — talks to `/v1` (`--control-plane-url` + a per-run,
    project-scoped bearer token carried in the job env). The real target.
  - `LocalDbControlPlaneClient` — wraps the existing `db.py` helpers in-process
    (`--db-path`, legacy). The only remaining `website.dashboard.db` importer; keeps
    single-node dev + SLURM-on-shared-DB working with zero new infra. Deleted when
    remote hosting is the only path.
  - `NullControlPlaneClient` — no-op (headless / Telegram-only).
- **Contract rules:** every method is fail-soft (log, never raise) except
  `fetch_project()` at bootstrap. Auth is a short-lived, project-scoped bearer token
  minted by the CP at launch — never passed on argv.
- **Non-breaking + SLURM-safe:** transport is opt-in. Empty `CONTROL_PLANE_URL`
  preserves today's `--db-path` behavior, so local and SLURM paths are unchanged until
  an operator flips it. The `/v1` router is a thin wrapper over the same `db.py`
  helpers, so the LocalDb and Http paths share one implementation.

## Consequences

- The orchestrator can run anywhere and talk home over the network only — unblocks
  Phases 2–7.
- The `/v1` API is DB-agnostic, so the Postgres swap (Phase 2) does not touch the
  orchestrator.
- Live logs no longer require a shared filesystem: pushed `ProjectEvent`s back the
  dashboard `/log` + `/stream`, falling back to on-disk `.out` files for legacy runs.
- Adds an HTTP surface to secure, version, and keep backward-compatible; status/event
  writes must batch/coalesce to avoid chatty HTTP.
- Carries a temporary `LocalDbControlPlaneClient` adapter that exists only to be
  deleted once remote hosting is the sole path.

## Alternatives considered

- **Keep the shared SQLite file / rsync the working dir back each poll.** Rejected: a
  remote orchestrator cannot share a file; the rsync bridge is fragile and not a real
  boundary.
- **Let the control plane call into the orchestrator (inbound).** Rejected: requires
  ingress into the user's cloud (hostile to firewalls/NAT) and inverts today's polling
  model.
- **A message bus / queue broker instead of HTTP polling.** Rejected for Phase 1 as
  new infrastructure that violates the "code before infrastructure" principle; HTTP
  polling reuses the existing interaction model.
