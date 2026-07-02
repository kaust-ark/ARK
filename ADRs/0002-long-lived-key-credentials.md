# ADR-0002 — Long-lived encrypted keys now; delegated-credential seam later

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** [`../CLOUD_BACKEND_PLAN.md`](../CLOUD_BACKEND_PLAN.md) §Secrets, Phase 8; [ADR-0001](0001-byoc-thin-control-plane.md)

## Context

Under BYOC ([ADR-0001](0001-byoc-thin-control-plane.md)) the orchestrator runs in
the user's cloud but needs LLM and cloud credentials to do its work. There are two
credential models: **long-lived keys** (user stores keys with us, we inject them
into the launched job) and **delegated roles** (AWS AssumeRole, GCP service-account
impersonation, Azure managed identity, or a self-hosted runner) where keys never
transit our servers.

Delegated credentials are what most enterprise buyers ultimately require, but they
add significant launcher and IAM complexity and are not needed to ship the first
BYOC milestones.

## Decision

We will ship with the **current long-lived-key model**: keys stay encrypted at rest
in the control-plane DB (the `User.encrypted_keys` seam already exists), are injected
into the launched job's env / RAM disk, and are wiped on teardown.

We will **design the secret-injection seam so delegated roles can drop in later
without launcher rework** (tracked as Phase 8). We do not build delegated credentials
now.

## Consequences

- No new credential infrastructure is required to reach the first BYOC phases —
  keeps Phases 1–7 focused.
- Keys transit and rest on our servers (encrypted), which blocks the most security-
  strict enterprise buyers until Phase 8 lands.
- The Phase 5/7 launchers must keep secret injection behind a seam so AssumeRole /
  impersonation / managed identity / runner can be added without reworking launch.
- We must keep key scoping tight, support rotation, and guarantee teardown wipes
  injected secrets.

## Alternatives considered

- **Build delegated credentials now.** Rejected: large IAM/launcher workstream that
  isn't on the critical path to a working BYOC product; deferred to Phase 8.
- **Self-hosted "ARK runner" (CI-runner model) from the start.** Rejected for now as
  the same premature complexity; retained as a Phase 8 option so keys never leave the
  user's account.
