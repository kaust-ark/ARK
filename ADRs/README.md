# Architecture Decision Records

This directory records the **significant, hard-to-reverse decisions** behind ARK's
architecture — one decision per file, in the order they were made. An ADR captures
*why* a choice was made and what was traded away, so future readers (and agents)
don't re-litigate settled questions or accidentally undo them.

## Conventions

- **Filename:** `NNNN-kebab-case-title.md`, monotonically numbered. Never renumber
  or delete an ADR — supersede it instead (see below).
- **One decision per record.** If a change bundles two decisions, write two ADRs.
- **Immutable once Accepted.** To change course, add a *new* ADR and set the old
  one's status to `Superseded by [ADR-NNNN](...)`. The record of the old decision
  stays.
- **Status** is one of:
  - `Proposed` — under discussion, not yet committed.
  - `Accepted` — decided and in force.
  - `Implemented` — Accepted *and* the code has landed (we note this explicitly
    because several decisions here shipped on the `feat/byoc-cloud-backend` branch).
  - `Superseded` / `Deprecated` — no longer in force; links to what replaced it.
- Use [`0000-template.md`](0000-template.md) as the starting point for new records.

## Relationship to the design docs

The BYOC migration has two long-form design docs that ADRs here summarize and
cross-link — the docs carry the full detail; the ADRs carry the *decision*:

- [`../CLOUD_BACKEND_PLAN.md`](../CLOUD_BACKEND_PLAN.md) — the phased BYOC migration plan.
- [`../CONTROL_PLANE_BOUNDARY.md`](../CONTROL_PLANE_BOUNDARY.md) — the Phase 1 control-plane ⇄ orchestrator boundary (design decisions D1–D4).

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-byoc-thin-control-plane.md) | Adopt a thin control plane + bring-your-own-cloud model | Accepted |
| [0002](0002-long-lived-key-credentials.md) | Long-lived encrypted keys now; delegated-credential seam later | Accepted |
| [0003](0003-http-v1-control-plane-boundary.md) | Replace shared SQLite/FS with an authenticated HTTP `/v1` boundary | Implemented |
| [0004](0004-ack-based-command-delivery.md) | Ack-based (at-least-once) command delivery (D2) | Implemented |
| [0005](0005-hitl-fanout-on-control-plane.md) | Human-in-the-loop fan-out lives on the control plane (D1) | Implemented |
| [0006](0006-server-side-decision-timeouts.md) | Enforce decision timeouts server-side (D4) | Accepted |
| [0007](0007-checkpoint-resume-ownership.md) | Keep checkpoint/resume data orchestrator-local (D3) | Proposed |
| [0008](0008-two-lane-queue-concurrency.md) | Two-lane FIFO concurrency; queue instead of hard-reject | Implemented |
| [0009](0009-apptainer-experiment-sandbox.md) | Run agent-generated experiment code in an Apptainer sandbox | Implemented |
| [0010](0010-skypilot-provisioning.md) | Prefer SkyPilot for cross-cloud/K8s provisioning | Proposed |
| [0011](0011-postgres-dsn-and-unified-alembic.md) | Postgres via a DSN-or-path seam, with one Alembic history for both backends | Implemented |
