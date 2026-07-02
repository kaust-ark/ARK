# ADR-0001 — Adopt a thin control plane + bring-your-own-cloud (BYOC) model

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** [`../CLOUD_BACKEND_PLAN.md`](../CLOUD_BACKEND_PLAN.md); [ADR-0002](0002-long-lived-key-credentials.md), [ADR-0003](0003-http-v1-control-plane-boundary.md)

## Context

ARK has two compute layers. Layer 1 (experiment jobs, `ark/compute/`) already runs
in the user's cloud. Layer 2 — the orchestrator (`ark/orchestrator/`): LLM agent
calls, LaTeX compilation, and execution of arbitrary agent-generated code — still
runs on our host and is coupled to the control plane by shared local resources.

Running the orchestrator on our infrastructure means we bear the cost and risk of
executing untrusted agent-generated code, and we cannot serve enterprises/labs that
require compute and data to stay in their own accounts.

## Decision

We will move ARK to a **thin control plane + bring-your-own-cloud** architecture:

- **We run only the control plane** — API, database, dashboard, auth, Telegram/HITL,
  the command + decision queues, and artifact *references*. It holds metadata and
  human-interaction state only.
- **The user brings all compute.** *Both* the orchestrator process *and* the
  experiment jobs it spawns run in the user's account (VM, SLURM, or K8s).
- **SLURM stays a first-class citizen** for both the orchestrator launcher and the
  experiment backend, at every phase.

The migration is sequenced in `CLOUD_BACKEND_PLAN.md`: Phases 1–4 are pure code
refactors (mergeable, no new infra); Phases 5+ stand up remote infrastructure.

## Consequences

- Untrusted agent code and heavy compute leave our servers; we host a small,
  stateless-ish control surface.
- Unlocks enterprise/lab customers who cannot let data or compute leave their cloud.
- Requires an authenticated network boundary in place of shared SQLite/FS
  ([ADR-0003](0003-http-v1-control-plane-boundary.md)) — this is the linchpin all
  later phases depend on.
- We must preserve single-node local dev and SLURM behavior throughout; every phase
  carries an explicit "SLURM still works" acceptance check.
- Two launch paths are needed long-term: a VM path (solo researchers) and a
  Kubernetes path (orgs), plus SLURM (HPC).

## Alternatives considered

- **Keep running the orchestrator on our servers.** Rejected: we absorb the cost and
  liability of executing arbitrary agent code, and cannot meet enterprise data-
  residency requirements.
- **Move only experiments to the user's cloud (leave the orchestrator with us).**
  Rejected: the orchestrator is where arbitrary agent code and LaTeX run; leaving it
  on our host keeps the main risk and cost with us.
