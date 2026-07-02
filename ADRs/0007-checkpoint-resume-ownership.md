# ADR-0007 — Keep checkpoint/resume data orchestrator-local

- **Status:** Proposed — this is decision **D3** in the boundary doc; decide before Phase 3
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** [`../CONTROL_PLANE_BOUNDARY.md`](../CONTROL_PLANE_BOUNDARY.md) D3; [`../CLOUD_BACKEND_PLAN.md`](../CLOUD_BACKEND_PLAN.md) Phase 3

## Context

Today `checkpoint_data` is stored as a blob in the control-plane DB so a restarted
orchestrator can resume. Under BYOC ([ADR-0001](0001-byoc-thin-control-plane.md)) the
orchestrator is remote and will have its own object storage (the user's bucket).
Pushing large checkpoint blobs back to the control-plane DB over the network
([ADR-0003](0003-http-v1-control-plane-boundary.md)) is wasteful and bloats the CP DB.
This decision shares the storage seam with Phase 3 (artifact storage), so it should be
settled before Phase 3 begins.

## Decision (recommended, not yet locked)

We will keep the **full checkpoint orchestrator-local** (in the user's bucket), and
have the control plane store only a **small resume pointer** — enough to relaunch and
tell the orchestrator where its checkpoint lives.

This is a *recommendation* pending final confirmation alongside the Phase 3 artifact-
storage design; the current DB-blob behavior remains until then.

## Consequences

- Large blobs stay out of the control-plane DB; less network traffic and DB growth.
- Checkpoint durability becomes the user bucket's responsibility (aligns with artifact
  storage in Phase 3).
- Resume requires the CP pointer and the orchestrator's storage to both be available;
  the pointer format must be defined with the Phase 3 `ArtifactStore` seam.

## Alternatives considered

- **Keep the full checkpoint blob in the control-plane DB.** Rejected as the default
  for remote runs: bloats the CP DB and pushes large payloads over the boundary; may be
  retained only for the local/SLURM-on-shared-mount path.
