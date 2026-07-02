# ADR-0006 — Enforce decision timeouts server-side

- **Status:** Accepted — this is decision **D4** in the boundary doc
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** [`../CONTROL_PLANE_BOUNDARY.md`](../CONTROL_PLANE_BOUNDARY.md) §4.6, D4; [ADR-0005](0005-hitl-fanout-on-control-plane.md)

## Context

Each HITL decision carries a `deadline_at` and a `timeout_action`. Historically the
orchestrator enforced the timeout itself (calling `expire_decision` on the deadline)
and ran the grace/pause UX in its wait loop. Once human fan-out and answer collection
move to the control plane ([ADR-0005](0005-hitl-fanout-on-control-plane.md)), the CP
already knows `deadline_at` and is the party notifying and collecting answers, so
timeout enforcement naturally belongs there too.

## Decision

We will let the **control plane enforce decision timeouts**: on `deadline_at`, the CP
marks the decision `timed_out` and applies the `timeout_action` (via the HITL engine's
`sweep`, run by the daemon with the webapp lifespan as a backstop).

The orchestrator's wait loop simplifies to: **poll until `answered` or `timed_out`**.
It no longer computes or enforces deadlines.

## Consequences

- A remote orchestrator's wait loop gets simpler and has no timekeeping responsibility.
- Timeout behavior is consistent regardless of orchestrator state (even if the
  orchestrator is briefly unreachable, the CP still expires on schedule).
- The CP must run a reliable sweeper; we cover it from both the daemon and the webapp
  lifespan to avoid a single point of failure.

## Alternatives considered

- **Keep timeout enforcement in the orchestrator.** Rejected: splits deadline
  knowledge across both sides and complicates the remote orchestrator's wait loop; a
  stalled/unreachable orchestrator would fail to expire decisions.
