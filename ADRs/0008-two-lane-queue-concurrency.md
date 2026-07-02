# ADR-0008 — Two-lane FIFO concurrency; queue instead of hard-reject

- **Status:** Implemented (`feat/byoc-cloud-backend`)
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** commits `8bafcfb`, `efba19f`; `website/dashboard/routes.py`, `website/dashboard/app.py`

## Context

Orchestrator runs consume real resources (each project's conda env is ~1–2 GB of
disk, plus CPU/memory), so the number of concurrent runs on the shared host must be
bounded. The prior scheme let regular users and admins contend for the same slots and
**hard-rejected** submissions past the cap ("Max 1 concurrent"). That was both a poor
UX (users had to manually resubmit) and inconsistent: new-project submission queued,
but continue/restart rejected.

## Decision

We will run **two independent FIFO lanes** so regular users and admins never block
each other, and we will **queue overflow instead of rejecting it**:

- **Regular lane:** 1 active run per user, 3 global.
- **Admin lane:** 5 global.
- Overflow submissions become `status=pending` and are promoted **lane-aware, FIFO**
  by `_advance_pending_queue`; `_queue_position` is lane-aware.
- **Host disk cap** of 5 projects (was 10) bounds per-user disk footprint.
- **Conda-env GC:** delete `<project>/.conda_env` on terminal status to reclaim disk;
  Research Step 0 re-provisions idempotently on continue/restart.
- **Continue/restart queue** rather than hard-reject — they already flow through
  `_try_submit_or_pending` and the lane-aware drainer relaunches them from stored
  config. The chat-driven `/apply` path **keeps its hard-reject**, because a deferred
  apply would lose its instruction through the drainer (which relaunches with stored
  config only).

## Consequences

- Regular users and admins get isolated throughput; a busy lane can't starve the other.
- Users no longer manually resubmit at the cap — work auto-starts when a slot frees.
- Disk pressure is bounded by the host cap plus conda-env GC.
- One deliberate asymmetry: `/apply` still hard-rejects at the cap (documented above)
  because instruction-carrying submissions can't be safely deferred through the drainer.

## Alternatives considered

- **Single shared queue for all users.** Rejected: admins and regular users block each
  other; no isolation.
- **Hard-reject past the cap (status quo).** Rejected: poor UX and inconsistent with
  new-project queueing.
- **Queue the `/apply` path too.** Rejected: the drainer relaunches from stored config
  only, so a deferred apply would drop its instruction.
