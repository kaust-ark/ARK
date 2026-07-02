# ADR-0005 — Human-in-the-loop fan-out lives on the control plane

- **Status:** Implemented (Phase 1, step 6) — this is decision **D1** in the boundary doc
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** [`../CONTROL_PLANE_BOUNDARY.md`](../CONTROL_PLANE_BOUNDARY.md) §4.6, D1; commit `689b3b6`; [ADR-0003](0003-http-v1-control-plane-boundary.md), [ADR-0006](0006-server-side-decision-timeouts.md)

## Context

Human-in-the-loop decisions were handled partly by the orchestrator: it formatted and
sent Telegram messages itself (per-orchestrator `TelegramDispatcher` in
`ark/telegram/client.py`), recorded answers it received via Telegram
(`answer_decision`), and expired decisions on deadline (`expire_decision`). Under BYOC
([ADR-0001](0001-byoc-thin-control-plane.md)) that would force Telegram credentials and
account-level notification settings into the user's cloud, and scatter HITL logic
across the orchestrator and the control plane.

## Decision

We will move **all human fan-out and answer collection to the control plane**. The
orchestrator's decision surface shrinks to **open + poll**:

- Orchestrator: `POST /v1/projects/{id}/decisions` (open) and
  `GET …/decisions/{id}` (poll for the answer). Nothing else.
- Control plane owns notification *and* answer collection across all channels
  (Telegram / webapp / email). A new `website/dashboard/hitl.py` engine formats,
  notifies, applies replies, and sweeps timeouts, with the Telegram transport injected
  (unit-tested).
- The **daemon is the sole Telegram poller**: it notifies opened decisions, answers
  replies via `apply_reply`, routes other messages into the command queue, and sweeps
  timeouts; the webapp lifespan starts it and also sweeps as a backstop.
- The orchestrator's Telegram dispatcher becomes **send-only** (`start(poll=False)`).
- `answer_decision` / `expire_decision` are **removed** from the `ControlPlaneClient`
  surface and the `/v1` API.

## Consequences

- Telegram credentials and notification settings stay on our servers; nothing HITL-
  related needs to be present in the user's cloud.
- HITL logic is centralized in one engine instead of split across orchestrator and CP.
- Server-side answer collection pairs with server-side timeout enforcement
  ([ADR-0006](0006-server-side-decision-timeouts.md)).
- The live daemon/orchestrator Telegram wiring is compile-checked; end-to-end exercise
  needs a real environment (bot token + full stack).

## Alternatives considered

- **Keep Telegram send/receive on the orchestrator.** Rejected: pushes credentials and
  notification config into the user's cloud and duplicates HITL logic.
- **Defer D1 and keep transitional `answer_decision`/`expire_decision`.** Considered as
  a fallback (documented as transitional), but the clean end-state landed within
  Phase 1, so these were removed rather than carried.
