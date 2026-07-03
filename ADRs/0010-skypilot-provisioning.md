# ADR-0010 — Prefer SkyPilot for cross-cloud/K8s provisioning

- **Status:** Accepted as direction (2026-07-03) — Phases 5 & 6 folded; SkyPilot is
  the Phase-5 provisioner. Adopt behind config, default-off, prove parity before
  deprecating the GCP `cloud` path. (Was: Proposed, 2026-07-01.)
- **Date:** 2026-07-01 (folded 2026-07-03)
- **Deciders:** ARK core
- **Related:** [`../CLOUD_BACKEND_PLAN.md`](../CLOUD_BACKEND_PLAN.md) Phases 5 & 6, §7
  (build vs buy); [`../SKYPILOT_PLAN.md`](../SKYPILOT_PLAN.md) (the folded implementation
  plan); [ADR-0001](0001-byoc-thin-control-plane.md)

## Context

BYOC ([ADR-0001](0001-byoc-thin-control-plane.md)) must serve both solo researchers
(AWS/GCP/Azure VMs) and orgs (BYO-Kubernetes), for both the experiment backend and the
orchestrator launcher. Today this is hand-rolled per cloud in
`ark/compute/cloud/{aws,gcp,azure}.py` (SSH + rsync + marker-file polling + teardown),
which is significant surface area to maintain and doesn't cover K8s.

## Decision

> **Update (2026-07-03) — Phases 5 & 6 folded.** Rather than hand-roll AWS/Azure VM
> provisioning for Phase 5 and adopt SkyPilot separately in Phase 6, we make SkyPilot
> the Phase-5 provisioner directly, for both compute layers. No bespoke AWS/Azure VM
> code is written. The GCP `cloud` path stays default and untouched until parity is
> proven. Implementation plan: [`../SKYPILOT_PLAN.md`](../SKYPILOT_PLAN.md).

We will **prefer [SkyPilot](https://github.com/skypilot-org/skypilot)** as the
cross-cloud/K8s provisioner, adopted incrementally:

- Add `type: skypilot` for **Layer 1** (experiments) as a `SkyPilotBackend`, and for
  **Layer 2** (orchestrator) as a `JobLauncher`.
- Run the hand-rolled and SkyPilot paths **in parallel behind config** until SkyPilot
  reaches parity (spot, retries, teardown, cost labels). Only then consider deprecating
  the bespoke cloud backends.
- SkyPilot is **additive**: the native `slurm` backend is never replaced.

Complementary build-vs-buy calls from the plan: **Temporal** (durable orchestration)
is optional and off the critical path — evaluate only if resumability/reliability
becomes a real pain point; a **native Kubernetes client** (Phase 7) is pursued only if
SkyPilot's K8s support proves insufficient for enterprise needs (RBAC, quotas, network
policy).

## Consequences

- One abstraction yields both the VM and K8s paths, directly serving the "both users"
  requirement and shrinking Phases 5–7.
- Replaces most of `cloud/{aws,gcp,azure}.py` eventually, reducing bespoke provisioning
  code.
- Adds a third-party dependency in the compute path; we must prove parity (spot,
  retries, teardown, cost labels) before deprecating anything.
- The native `slurm` backend must remain first-class regardless.

## Alternatives considered

- **Keep hand-rolling per-cloud provisioning.** Rejected as the long-term path: high
  maintenance, no K8s coverage; retained transitionally behind config until SkyPilot
  parity.
- **Kubernetes-native client instead of SkyPilot.** Deferred to Phase 7, and only if
  SkyPilot's K8s support is insufficient for enterprise requirements.
- **Temporal for durable orchestration.** Deferred: heavyweight, overlaps the Layer-2
  launcher, not on the critical path.
