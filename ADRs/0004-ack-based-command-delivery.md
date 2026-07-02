# ADR-0004 — Ack-based (at-least-once) command delivery

- **Status:** Implemented (Phase 1) — this is decision **D2** in the boundary doc
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** [`../CONTROL_PLANE_BOUNDARY.md`](../CONTROL_PLANE_BOUNDARY.md) §4.3, D2; [ADR-0003](0003-http-v1-control-plane-boundary.md)

## Context

Commands from the control plane to the orchestrator (`pause`/`resume`/`stop`/`steer`/
`set_autonomy`) were delivered by `take_pending_commands`, which **consumed on read** —
a single DB call both returned pending commands and marked them consumed. Over a
reliable in-process DB call that is fine. Over the network ([ADR-0003](0003-http-v1-control-plane-boundary.md)),
a dropped or lost HTTP GET response would silently discard a command — losing a
`stop` or `steer` is unacceptable.

## Decision

We will split delivery into **peek + ack**:

- `GET /v1/projects/{id}/commands` returns pending commands **without** marking them
  consumed.
- The orchestrator applies a command, then explicitly acks it via
  `POST /v1/projects/{id}/commands/{cmd_id}/ack`.

Commands stay `pending` until acked. This is **at-least-once** delivery, so command
application must be **idempotent**. Backed by `db.list_pending_commands` /
`db.mark_command_consumed`.

## Consequences

- A lost response re-delivers the command on the next poll instead of dropping it —
  no silently-lost `stop`/`steer`.
- Command handlers must tolerate re-delivery (idempotent apply).
- Slightly more chatter (an extra ack round-trip per command) — negligible at command
  volumes.

## Alternatives considered

- **Keep consume-on-read.** Rejected: not safe over a lossy network; a dropped GET
  loses the command.
- **Exactly-once delivery.** Rejected: genuinely-exactly-once over an unreliable
  network requires heavy coordination; at-least-once + idempotent apply is simpler and
  sufficient.
