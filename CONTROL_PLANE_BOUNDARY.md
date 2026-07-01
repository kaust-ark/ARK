# Phase 1 — Control Plane ⇄ Orchestrator Boundary

> **Part of:** [`CLOUD_BACKEND_PLAN.md`](CLOUD_BACKEND_PLAN.md) Phase 1 (the linchpin).
> **Status:** Design — not yet implemented.
> **Goal:** Replace the orchestrator's *shared SQLite + shared filesystem* coupling
> with an **authenticated HTTP API boundary**, so the orchestrator can run anywhere
> (our host, SLURM, or the user's cloud) and talk home over the network only.

This document defines **where the line is** — what each side owns, exactly what
crosses it today, and the v1 API + client contract that will carry those crossings.

## 1. Core principles

1. **Outbound-only, orchestrator-initiated.** The control plane **never** calls
   into the orchestrator. The orchestrator *reports* state up and *pulls* commands
   and decision answers. This is firewall-friendly (only egress from the user's
   cloud) and matches today's polling design — we are swapping the transport
   (SQLite → HTTPS), not the interaction model.
2. **The control plane is the system of record for identity and human
   interaction; the orchestrator is the system of record for the live run.** The
   CP owns *who/what/who-may* and *how we reach the human*. The orchestrator owns
   *what the run is doing right now* and the working files it produces.
3. **One narrow, versioned contract.** All crossings go through a single
   `ControlPlaneClient` interface (§5) and a single `/v1` API (§4). Nothing under
   `ark/` imports `website.dashboard.db` after this phase — except the one
   `LocalDbControlPlaneClient` adapter that exists precisely to be deleted when
   remote hosting lands.
4. **Non-breaking + SLURM-safe.** Local single-node dev and SLURM keep working at
   every step (see §7 migration + §8 acceptance).

## 2. Ownership — where the line is

| Concern | Owner | Notes |
|---|---|---|
| Identity, auth, users, quotas, access requests | **Control plane** | Never leaves our servers. |
| Project durable record (name, idea, venue, owner, config) | **Control plane** | Source of truth for metadata; orchestrator reads it at bootstrap. |
| Run progress (status, phase, iteration, score, cost, pid, activity) | **Orchestrator** reports → CP stores | CP copy is a *display cache*; the orchestrator is live truth. |
| Command queue (pause/resume/stop/steer/set_autonomy) | **Control plane** enqueues → orchestrator pulls + acks | |
| Decision lifecycle + human fan-out (Telegram / webapp / email) | **Control plane** | Orchestrator *opens* a decision and *polls* the answer; CP notifies channels and records the answer (see Decision D1). |
| Conversation thread (chat bubbles) | **Control plane** stores | Orchestrator, webapp, and CP all append. |
| Working dir / project files (state YAML, `agent_steps.jsonl`, LaTeX, PDF) | **Orchestrator** (in the user's cloud) | Surfaced to CP as *events* (§4.7) and *artifacts* (§4.8), not via shared FS. |
| Checkpoint / resume data | **Decision D3** — recommend orchestrator-local | Today a CP DB blob; revisit for remote. |
| Secrets (LLM + cloud keys) | **Control plane** stores encrypted → injected into the job | Delegated creds are Phase 8. |
| Artifact bytes (PDFs, figures) | Object storage (Phase 3); CP holds references | Phase 1 registers refs only. |

## 3. Current boundary crossings (evidence base)

Every crossing today is a call from `ark/orchestrator/core.py` into a
`website/dashboard/db.py` helper (via `_sync_db` / `_hitl_db`). Enumerated so the
API in §4 is a faithful 1:1 mapping, nothing invented, nothing missed.

### 3a. Orchestrator → CP (writes / reports)
| Call site (`core.py`) | `db.py` helper | Purpose | Fields |
|---|---|---|---|
| `_sync_db(**kwargs)` (l.321) | `update_project` | Report run state | `status`, `pid`, `phase`, `iteration`, `dev_iteration`, `dev_status`, `score`, `score_history` (JSON), `language`, `checkpoint_data` (JSON), `total_cost_usd`, `total_input_tokens`, `total_output_tokens`, `total_agent_calls`, `pdf_path`, `has_pdf_upload`, `error_message` |
| `_set_activity` (l.373) | `set_activity` | Live one-line status | `activity` (≤300 chars) |
| `_set_control_state` (l.383) | `set_control_state` | Run state for UI | `control_state` ∈ `""`/`paused`/`awaiting` |
| `_apply_control_commands` set_autonomy (l.477) | `set_autonomy` | Persist autonomy echo | `autonomy_level` |
| `_chat` (l.518) | `add_message` | Append chat bubble | `role`, `kind`, `text`, `meta` (JSON) |

### 3b. Orchestrator ← CP (reads / pulls)
| Call site (`core.py`) | `db.py` helper | Purpose | Returns |
|---|---|---|---|
| bootstrap `main()` (l.3125) | `get_project_by_name` / `get_project` | Resolve project + load config | full `Project` record |
| `autonomy()` (l.393) | `get_project` | Current autonomy level | `autonomy_level` |
| `_poll_control` (l.433) | `take_pending_commands` | Drain command queue (FIFO, marks consumed) | `[{id, kind, payload, source, created_by}]`; kinds: `pause`/`resume`/`stop`/`steer`/`set_autonomy` |

### 3c. Decisions (bidirectional today)
| Call site (`core.py`) | `db.py` helper | Purpose |
|---|---|---|
| `ask_user_decision` open (l.2862) | `create_pending_decision` | Open a decision → returns `decision_id`; cancels any prior open one |
| wait loop poll (l.2899) | `get_decision` | Poll `status` (`answered`/`cancelled`/`timed_out`), `answer_index`, `answer_text` |
| Telegram answer (l.2923) | `answer_decision` | Record an answer the **orchestrator** received via Telegram |
| timeout (l.2977, 2992) | `expire_decision` | Mark `timed_out` on deadline |

> Note the coupling beyond the DB: today the orchestrator *also* talks to Telegram
> directly (`self.telegram.send`, formats the rich HTML message) and reads/writes
> project **files** that the dashboard renders. Both are part of the boundary and
> are addressed by Decision D1 (§6) and the events/artifacts endpoints (§4.7–4.8).

## 4. The `/v1` API surface

Base: `POST/GET https://<control-plane>/v1/projects/{project_id}/…`. Auth: §7.
All request/response bodies are JSON. Every write is **idempotent** where noted.

### 4.1 Bootstrap — `GET /v1/projects/{id}`
Returns the record the orchestrator needs to start: `autonomy_level`, `model`,
`model_variant`, `paper_accept_threshold`, `max_iterations`, `max_dev_iterations`,
`max_days`, `language`, `figure_generation`, `orchestrator_compute_backend`,
`experiment_compute_backend`, `cloud_overrides`, plus resume hints
(`phase`, `iteration`, `checkpoint_data`). Replaces `get_project` /
`get_project_by_name` (a `?name=` resolver covers the CLI's by-name lookup).

### 4.2 Status report — `PATCH /v1/projects/{id}/run`
Partial, idempotent update of the runtime fields from §3a (`update_project` +
`set_activity` + `set_control_state` folded into one call). Body carries only
changed keys. Small/high-frequency fields (`activity`, `control_state`) may also
be sent on a lightweight `POST …/heartbeat` to avoid large PATCH bodies.

### 4.3 Commands (pull) — `GET /v1/projects/{id}/commands`
Returns pending commands **without** marking them consumed. The orchestrator acks
after applying: `POST /v1/projects/{id}/commands/{cmd_id}/ack`. (Change from
today's consume-on-read `take_pending_commands` — see Decision D2: at-least-once
delivery is safer over a lossy network.)

### 4.4 Autonomy — folded in
Read via §4.1/§4.2 response; the `set_autonomy` command arrives via §4.3. The
orchestrator's echo write becomes part of the §4.2 PATCH (or is dropped if the CP
already owns it authoritatively — the CP is the source of truth for autonomy).

### 4.5 Messages — `POST /v1/projects/{id}/messages`
Append a chat bubble: `{role, kind, text, meta}`. Replaces `add_message`.

### 4.6 Decisions
- `POST /v1/projects/{id}/decisions` — open: `{question, options[], kind, context,
  default_index, timeout_action, deadline_at}` → `{decision_id}`. The CP cancels
  any prior open decision **and fans out to Telegram/webapp/email** (D1).
- `GET /v1/projects/{id}/decisions/{decision_id}` — poll → `{status, answer_index,
  answer_text, source}`. Replaces `get_decision`.
- Answering + expiry move **server-side** (D1): the CP records webapp/Telegram
  answers and enforces `deadline_at`. This removes `answer_decision` /
  `expire_decision` from the orchestrator's surface. (If D1 is deferred, expose
  `POST …/decisions/{id}/answer` and `…/expire` transitionally.)

### 4.7 Events / logs — `POST /v1/projects/{id}/events`  *(new capability)*
Append `agent_steps.jsonl` lines / log tail so the dashboard shows live progress
without reading the orchestrator's filesystem. Today the webapp reads these files
directly; this endpoint is what removes that shared-FS dependency for logs.

### 4.8 Artifacts — `POST /v1/projects/{id}/artifacts`  *(stub in Phase 1)*
Register an artifact reference `{kind, path|url, sha256, bytes}`. Real byte
upload / object storage lands in Phase 3; Phase 1 just establishes the endpoint
and reference model so PDFs stop being served from a shared mount.

## 5. The `ControlPlaneClient` contract (code seam)

New module `ark/controlplane/client.py`. The orchestrator holds **one**
`self.cp` and never touches `website.dashboard.db` again.

```python
class ControlPlaneClient(Protocol):
    # bootstrap / config
    def fetch_project(self) -> ProjectView: ...
    # status
    def report_status(self, **fields) -> None: ...          # 4.2  (fail-soft)
    def set_activity(self, text: str) -> None: ...          # 4.2
    def set_control_state(self, state: str) -> None: ...    # 4.2
    # commands
    def poll_commands(self) -> list[Command]: ...           # 4.3
    def ack_command(self, cmd_id: str) -> None: ...         # 4.3
    # conversation
    def append_message(self, role, text, kind="message", meta=None) -> None: ...  # 4.5
    # decisions
    def open_decision(self, question, options, *, kind, context,
                      default_index, timeout_action, deadline_at) -> str: ...     # 4.6
    def get_decision(self, decision_id: str) -> DecisionView | None: ...          # 4.6
    # live output
    def append_events(self, lines: list[dict]) -> None: ...  # 4.7
    def register_artifact(self, **ref) -> None: ...          # 4.8  (Phase 1 stub)
```

**Three implementations**, selected at launch:

| Implementation | When | Behavior |
|---|---|---|
| `HttpControlPlaneClient` | `--control-plane-url` present | Talks to the `/v1` API. The real target. |
| `LocalDbControlPlaneClient` | `--db-path` present (legacy) | Wraps the existing `db.py` helpers in-process. **The only remaining `website.dashboard.db` importer.** Keeps single-node dev + SLURM-on-shared-DB working with zero new infra. Deleted when remote hosting is the only path. |
| `NullControlPlaneClient` | neither | No-op (today's "no channel" case: Telegram-only or fully headless). |

**Contract rules:** every method is **fail-soft** (log, never raise — mirrors
today's `_sync_db` behavior) except `fetch_project()` at bootstrap, which may hard
fail. `report_status`/`append_events` should batch/coalesce to avoid chatty HTTP.

## 6. Key design decisions

- **D1 — Move human fan-out (Telegram/webapp/email) to the control plane.**
  **ACCEPTED (clean end-state, 2026-07-01).** Today the orchestrator formats and
  sends Telegram messages and records answers itself (per-orchestrator
  `TelegramDispatcher` in `ark/telegram/client.py`, plus the standalone
  `TelegramDaemon` mailbox router in `ark/telegram/daemon.py`). Under BYOC that
  would force Telegram creds and account-level notification settings into the
  user's cloud. End-state: the orchestrator only `open_decision` + `get_decision`;
  the CP owns notification *and* answer collection across all channels. This
  removes `answer_decision`/`expire_decision` from the orchestrator's surface,
  keeps creds on our servers, and centralizes HITL.
  - *Scope note:* fully relocating Telegram send/receive off the orchestrator is a
    **large workstream of its own** (touches `client.py`, `daemon.py`'s mailbox
    routing, webapp routes, `notify.py`). It is a **later step within Phase 1**,
    not part of the initial client scaffold.
  - *Transitional:* until that migration lands, the orchestrator still owns its
    Telegram channel, so `answer_decision` / `expire_decision` remain on the
    `ControlPlaneClient` **marked transitional** and are called only by the
    not-yet-migrated Telegram path in `ask_user_decision`. They are deleted when
    HITL fan-out moves server-side.

- **D2 — Ack-based command delivery** instead of consume-on-read. A dropped/lost
  HTTP GET must not silently discard a `stop`/`steer`. Commands stay `pending`
  until an explicit `ack` (at-least-once; commands must be idempotent to apply).

- **D3 — Checkpoint/resume ownership.** Today `checkpoint_data` is a CP DB blob so
  a restarted orchestrator can resume. For a remote orchestrator with its own
  object storage, keeping checkpoints orchestrator-local (in the user's bucket) is
  cleaner and keeps large blobs out of the control-plane DB. *Recommend:* CP
  stores only a small resume pointer; full checkpoint lives with the orchestrator.
  Decide before Phase 3 (artifact storage) since they share the storage seam.

- **D4 — Timeouts server-side.** With `deadline_at` known to the CP, let the CP
  enforce decision timeouts (mark `timed_out`, apply `timeout_action`). The
  orchestrator's grace/pause UX (in `ask_user_decision`) becomes: poll until
  `answered`/`timed_out`. Simplifies the remote orchestrator's wait loop.

## 7. Auth & migration

**Auth.** Replace "trusted local process with a DB file" with a **per-run,
project-scoped bearer token** minted by the CP when it launches the job (passed as
`--control-plane-token`, injected via env/EnvironmentFile like other secrets).
Short-lived + refreshable; scope = one project's endpoints only.

**Migration (ordered, each step independently mergeable):**
1. ✅ *Done.* Add `ark/controlplane/` with the ABC, `ProjectView`/`Command`/
   `DecisionView` dataclasses, and `LocalDbControlPlaneClient` (wraps current
   `db.py` calls) + `NullControlPlaneClient`.
2. ✅ *Done.* Refactor `core.py` to route **all** boundary calls through
   `self.cp`; delete `_sync_db`/`_hitl_db` direct `website.dashboard.db` usage.
   Wire client selection in `main()` (`--control-plane-url` → Http, `--db-path` →
   LocalDb, else Null). **Behavior identical** on the LocalDb path.
3. ✅ *Done.* Build the `/v1` FastAPI router (`website/dashboard/api.py`) as a
   **thin wrapper over the same `db.py` helpers**, mounted on the outer app.
   Per-project bearer-token auth (`auth.make_job_token`/`verify_job_token`) with a
   reportable-fields whitelist. Command delivery is peek + ack (D2) via new
   `db.list_pending_commands` / `db.mark_command_consumed`.
4. ✅ *Done.* `HttpControlPlaneClient` (stdlib urllib) against `/v1`;
   `--control-plane-url` / `--control-plane-token` CLI args wired into `main()` +
   constructor. Event storage + live-log rendering complete: new `ProjectEvent`
   table + `db.append_events`/`list_events` (integer-id cursor); `/events` stores;
   the orchestrator mirrors `log()` lines to the control plane via a buffered
   background flusher, but only when the transport has no shared FS
   (`cp.emits_events`, True for Http). The dashboard `/log` + `/stream` routes
   prefer the pushed event store when present and fall back to on-disk `.out`
   files otherwise — so HTTP/remote runs get live logs with no shared filesystem.
5. ✅ *Done.* Launchers (`jobs.py`: `submit_job` SLURM + `launch_local_job`, which
   `launch_cloud_job` wraps) pick the transport via `control_plane_transport`:
   when `settings.control_plane_url` (`CONTROL_PLANE_URL`) is set they pass
   `--control-plane-url` and mint a per-project token carried in the job **env**
   (`ARK_CONTROL_PLANE_TOKEN`, never argv); otherwise legacy `--db-path`. SLURM
   template is transport-conditional. Opt-in: empty `CONTROL_PLANE_URL` preserves
   today's behavior, so SLURM/local stay unchanged until an operator flips it.
   *(The `ark run` CLI launcher still uses `--db-path` — a small follow-up.)*
6. ⏳ HITL fan-out migration (D1) — move Telegram send/receive to the CP; delete
   the transitional `answer_decision`/`expire_decision`.

## 8. Acceptance criteria

- Within the orchestrator runtime, the sole importer of `website.dashboard.db`
  is `ark/controlplane/local_db.py`: `grep -rn "website.dashboard.db"
  ark/orchestrator/` finds only comments, and all crossings route through
  `self.cp`. (CLI/webapp-adjacent helpers — `cli.py`, `share.py`, `access.py` —
  still use the DB directly; they are out of scope for the orchestrator boundary.)
  ✅ *Done in the scaffold.*
- ✅ The `/v1` API + `HttpControlPlaneClient` round-trip over a real socket with
  token auth (`tests/integration/test_control_plane_api.py`): fetch/report/
  autonomy/commands-peek-ack/decisions, plus 401 (no token), 403 (wrong project),
  401 (bad signature), 200 (valid).
- ✅ Launchers wired (step 5): `control_plane_transport` selection + SLURM
  template both modes render valid bash and mint a scoped token
  (`tests/unit/test_job_transport.py`). ⏳ A full *live* orchestrator run over Http
  (and a SLURM node reaching the API) still needs an integrated environment to
  exercise end-to-end.
- ⏳ HITL: opening a decision notifies the human (Telegram/webapp) and the answer
  resolves the orchestrator's poll — via the CP (D1, step 6).
- ✅ Live logs: `/events` stores pushed lines (`ProjectEvent`), the orchestrator
  buffers+flushes `log()` output over Http, and the dashboard `/log` + `/stream`
  render from the store (fallback to `.out` files) — **no shared filesystem** for
  HTTP/remote runs. (db + `/events` endpoint + orchestrator glue covered by tests;
  dashboard-route glue is a thin `list_events` call, verified by compile.)
- ✅ New tests: LocalDb round-trip over the real `db.py` helpers + Null +
  `build_client` selection (`tests/unit/test_controlplane.py`), and the live
  `/v1` integration suite above.

## 9. Out of scope for Phase 1 (later phases)
- Postgres (Phase 2) — the `/v1` API is DB-agnostic; SQLite stays for now.
- Artifact **bytes** / object storage (Phase 3) — Phase 1 registers refs only.
- Actually **hosting** the orchestrator remotely (Phase 5) — Phase 1 only makes it
  *possible* by removing the shared-resource coupling.
- Delegated credentials (Phase 8) — Phase 1 keeps the current long-lived-key
  injection, just carried as a scoped token for the API.
```
