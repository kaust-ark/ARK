# ARK Cloud Backend Plan — Control Plane + Bring-Your-Own-Cloud

> **Status:** Planning / in progress.
> **Audience:** Engineers and coding agents implementing the phases below.
> **Companion docs:** [`ARCHITECTURE.md`](ARCHITECTURE.md), [`TODO.md`](TODO.md).

## 1. Goal

Move ARK to a **thin control plane + bring-your-own-cloud (BYOC)** model:

- **We run the control plane** on our servers: API, database, dashboard, auth,
  Telegram / human-in-the-loop (HITL), the command + decision queues, and
  references to artifacts.
- **The user brings their own compute.** *All* heavy work runs in the user's
  account: both the **orchestrator process** (the driver that makes LLM agent
  calls, compiles LaTeX, and executes arbitrary agent-generated code) **and the
  experiment jobs** it spawns.
- **SLURM stays a first-class citizen** throughout. On-prem HPC users must keep
  the exact same experience they have today, for both the orchestrator and
  experiments.

### Decisions locked (2026-07-01)

| Question | Decision | Consequence |
|---|---|---|
| How much moves to the user's cloud? | **Whole orchestrator + experiments** | Everything in `ark/orchestrator/`, `ark/engines/`, `ark/latex/`, `ark/compute/` runs remotely; control plane holds only metadata + HITL. |
| Credential model? | **Long-lived keys (current)** | Keys stay encrypted in our DB and are injected into the launched job. Design the seam so **delegated roles** can slot in later (Phase 8) — required for enterprise. |
| Target users? | **Both** individual researchers and enterprises/labs | Need a VM launch path (solo) **and** a Kubernetes launch path (orgs), plus SLURM (HPC). |

## 2. Guiding principles

1. **Code before infrastructure.** Phases 1–4 are *pure code refactors*,
   mergeable to `main` behind flags/config, testable on a single laptop with **no
   new infra**. Only Phases 5+ stand up remote infrastructure. Do not start an
   infra phase until its code prerequisites have landed.
2. **The API boundary is the linchpin.** The orchestrator must talk to the
   control plane over an authenticated HTTP API — never a shared SQLite file or
   a shared filesystem. Everything else depends on this.
3. **SLURM never regresses.** Every phase must keep `type: slurm` working for
   both the orchestrator launcher and the experiment backend. Each phase has an
   explicit "SLURM still works" acceptance check.
4. **Always shippable, always backward compatible.** Single-node local dev
   (SQLite + local FS + local subprocess) must keep working at every phase.
   New backends are additive, selected by config, defaulted off.
5. **Reuse the existing seams.** We already have `ComputeBackend`
   (`ark/compute/base.py`), a backend factory with an `is_orchestrator` flag
   (`ark/compute/__init__.py`), and the `orchestrator_compute_backend` /
   `experiment_compute_backend` config split. Build on these.

## 3. Current architecture (baseline)

There are **two compute layers**, at very different stages of readiness.

### Layer 1 — Experiment jobs (`ark/compute/`)
Abstracted behind `ComputeBackend` (`ark/compute/base.py`). Backends:
`local` (`local.py`), `slurm` (`slurm.py`), `cloud` → AWS/GCP/Azure
(`cloud/{aws,gcp,azure}.py` under `cloud/base.py::CloudBackend`), and `custom`
(`custom.py`). The cloud path already provisions a VM in the user's account,
`rsync`s code over, runs experiments over SSH, polls a marker file, pulls
results back, and tears down. **This layer already runs on the user's cloud
today.**

### Layer 2 — The orchestrator (`ark/orchestrator/`)
The long-running driver: LLM agent calls (OpenHands / Claude / Gemini CLIs),
LaTeX compilation, the plan→review→execute loop, and execution of arbitrary
agent-generated code during dev/review. Launched by the webapp via
`website/dashboard/jobs.py`:
- `submit_job()` → SLURM (`sbatch`, renders `slurm_template.sh`)
- `launch_local_job()` → detached `systemd --user` transient service, or child process
- `launch_cloud_job()` → thin wrapper; today still launches *locally* to *manage* a cloud VM
- `OrchestratorCloudBackend` (`ark/compute/cloud/orchestrator.py`, **GCP-only**)
  actually runs the orchestrator *on* a remote VM.

### The coupling we must remove
The orchestrator is bound to the control plane by **shared local resources**,
not an API:

1. **Direct SQLite writes.** The orchestrator is handed `--db-path` and imports
   `website.dashboard.db` to sync status, poll `ProjectCommand`, and post/read
   `PendingDecision`. See `jobs.py::submit_job` / `launch_local_job`
   (`--db-path`, `resolve_db_path()`). A remote orchestrator **cannot share a
   SQLite file.**
2. **Shared filesystem.** The dashboard renders state and serves PDFs by reading
   the project dir (`auto_research/state/*.yaml`, `latex/*.pdf`,
   `agent_steps.jsonl`) that the orchestrator writes locally. The current remote
   path papers over this by rsyncing `auto_research/` back to the control
   plane's local FS every poll (`orchestrator.py::poll_orchestrator`,
   ~lines 398–405). That is a fragile bridge, not a boundary.
3. **Secrets flow through our host.** Keys are written into the orchestrator's
   env / `EnvironmentFile` on our box (`jobs.py`, ~lines 757–806) and, for the
   remote VM, rsynced to `/dev/shm/.env` (`orchestrator.py`, ~lines 194–233).

> Inline `# Phase 4` / `# Phase 6` comments in `orchestrator.py` refer to an
> **older, superseded** plan. This document is now the canonical phase map.

## 4. Target architecture

```
  OUR SERVERS (control plane)              USER'S CLOUD / CLUSTER (all compute)
┌──────────────────────────────┐        ┌────────────────────────────────────┐
│ FastAPI  API + Dashboard     │        │  Orchestrator (Layer 2)              │
│ Postgres (was SQLite)        │◀─HTTPS─▶│   • LLM agent calls (OpenHands/CLIs) │
│ Auth · Telegram · HITL       │  poll  │   • LaTeX · arbitrary agent code     │
│ Command + Decision queues    │   +    │   • spawns experiments (Layer 1) ────┼──▶ experiment
│ Artifact references          │  push  │  ComputeBackend: local/slurm/cloud/… │    VMs / Slurm /
└──────────────────────────────┘        │  Object storage (user's bucket)      │    K8s Jobs
        ▲  presigned URLs                └────────────────────────────────────┘
        └── artifacts served to dashboard without a shared filesystem
```

The control plane keeps exactly what it has today (auth, `Project`,
`ProjectCommand`, `PendingDecision`, messages, Telegram). All of
`ark/orchestrator/`, `ark/engines/`, `ark/latex/`, `ark/compute/` runs in the
user's environment and talks home over the API.

### Backend matrix (target)

**Layer 2 — orchestrator launcher** (`orchestrator_compute_backend.type`):

| type | Where the orchestrator runs | Status |
|---|---|---|
| `local` | Our host (systemd/subprocess) — single-node dev + current default | Exists (`jobs.py`) |
| `slurm` | Our / on-prem SLURM cluster (`sbatch`) | Exists (`jobs.py::submit_job`) — **must keep** |
| `cloud` | A VM in the user's cloud | Partial (GCP only, `orchestrator.py`) → extend |
| `k8s` | A Job in the user's cluster | New (Phase 7) |

**Layer 1 — experiment backend** (`experiment_compute_backend.type`):

| type | Where experiments run | Status |
|---|---|---|
| `local` | Same host as orchestrator | Exists |
| `slurm` | SLURM cluster | Exists — **must keep** |
| `cloud` | VMs in user's cloud (AWS/GCP/Azure) | Exists |
| `custom` | User-defined | Exists |
| `skypilot` | Any cloud / K8s via SkyPilot | New (Phase 6) |
| `k8s` | Child Jobs in a cluster | New (Phase 7) |

**Valid combinations** are enforced by `validate_config()`
(`ark/compute/__init__.py`). Current rule: orchestrator `cloud` +
experiments `slurm` is rejected. Keep and extend this — the invariant is that
the orchestrator must be able to reach whatever runs the experiments (co-located,
or reachable network/API). Add matrix tests in Phase 4.

## 5. Phases

Legend: **[CODE]** = no new infra, mergeable to `main`. **[INFRA]** = stands up
remote infrastructure.

---

### Phase 0 — Baseline (partially done)
Document reality (this doc, §3). No new work; establishes the starting line.
- ✅ Layer 1 cloud experiment backends (AWS/GCP/Azure).
- ✅ `OrchestratorCloudBackend` (GCP only) that runs the orchestrator on a VM.
- ✅ `orchestrator_compute_backend` / `experiment_compute_backend` config split;
  factory `is_orchestrator` flag; `validate_config`.
- ⚠️ Remote path still couples via rsync-back + direct SQLite (the thing Phases 1–3 remove).

---

### Phase 1 — Control-plane API boundary  **[CODE]**  ← the linchpin
**Goal:** the orchestrator talks to the control plane only over an authenticated
HTTP API. No `website.dashboard.db` import, no `--db-path`, no shared-FS reads.
Everything keeps running **on-box** (loopback API) so SLURM and local paths are
unaffected in behavior.

> **Detailed design:** [`CONTROL_PLANE_BOUNDARY.md`](CONTROL_PLANE_BOUNDARY.md) —
> ownership table, the full inventory of current boundary crossings, the `/v1`
> API surface, the `ControlPlaneClient` contract, and the key design decisions
> (HITL fan-out ownership, ack-based commands, checkpoint/timeout ownership).

**Tasks**
1. Define the control-plane API (FastAPI, alongside the existing dashboard in
   `website/dashboard/`). Endpoints map 1:1 onto today's DB access:
   - `POST /v1/projects/{id}/status` — replaces direct writes to
     `Project.status` / `phase` / `score_history` / cost.
   - `GET  /v1/projects/{id}/commands` — replaces polling `ProjectCommand`
     (pause/resume/steer/set_autonomy).
   - `POST /v1/projects/{id}/decisions` and
     `GET /v1/projects/{id}/decisions/{decision_id}` — replaces
     `PendingDecision` write/poll. **HITL + Telegram stay on the control plane,
     unchanged**; the orchestrator just posts a decision and polls for the answer.
   - `POST /v1/projects/{id}/events` — stream `agent_steps.jsonl` lines / log tail.
   - `POST /v1/projects/{id}/artifacts` — register artifacts (real upload lands in Phase 3).
2. Build a thin **orchestrator-side client** (`ark/controlplane/client.py`, new)
   with a matching **local/in-process implementation** so single-node dev needs
   no network hop.
3. Replace the orchestrator's DB/FS coupling: drop `--db-path` and the
   `website.dashboard.db` import; add `--control-plane-url` + a scoped
   per-job token. Update `ark/orchestrator/core.py` and the arg parsing.
4. Update `jobs.py` launchers to pass `--control-plane-url` + token instead of
   `--db-path`. Issue a short-lived, project-scoped job token.
5. Dashboard reads project state through the API/DB models, not by re-reading
   the orchestrator's working files.

**SLURM check:** `submit_job()` renders a SLURM script that now passes
`--control-plane-url` (reaching the control plane over the network) instead of a
shared `--db-path`. A SLURM run updates status/decisions purely via the API.

**Acceptance**
- Orchestrator runs given only `--control-plane-url` + token; grep confirms no
  `import website...` and no `--db-path` anywhere under `ark/`.
- Full local run works end-to-end via the loopback API.
- SLURM run works end-to-end via the API.
- HITL decisions still route through Telegram and resolve the orchestrator's poll.

---

### Phase 2 — Postgres  **[CODE]**  ✅ DONE
**Goal:** back the control plane with Postgres so it can serve many concurrent
remote orchestrators. SQLite is single-node and unsuitable once the orchestrator
is remote.

> **Decision record:** [`ADRs/0011-postgres-dsn-and-unified-alembic.md`](ADRs/0011-postgres-dsn-and-unified-alembic.md)
> — DSN-or-path seam + one Alembic history across both backends.

**Tasks**
1. ✅ Parameterize the DB connection. `get_engine()` / `get_session()` /
   `resolve_db_path()` now accept a **DSN or a sqlite path**: a value containing
   `://` is a full SQLAlchemy DSN (Postgres, pooled with `pool_pre_ping`),
   anything else stays a sqlite file. Every existing `get_session(settings.db_path)`
   caller is unchanged. `DATABASE_URL` / `ARK_DATABASE_URL` select Postgres.
2. ✅ Add migrations (Alembic). `website/dashboard/migrations/` + `alembic.ini`;
   a baseline revision covers all 9 tables (incl. the `ALTER TABLE` columns like
   `orchestrator_compute_backend`, `cloud_overrides`). Alembic is the schema
   source of truth for **both** backends. A pre-Alembic dev DB is *adopted*
   (stamped at head), not rebuilt, so existing sqlite DBs boot untouched.
3. ✅ Keep **SQLite as the default for local dev** (DSN-selected); Postgres for
   deployed control planes.

**SLURM check:** unaffected — the launcher talks to the API, not the DB. The
remote orchestrator never sees the DSN (it uses HTTP `/v1`); only the
control-plane process opens DB connections.

**Acceptance**
- ✅ Control plane runs on Postgres and serves N simultaneous orchestrators —
  verified live against Postgres 16 with 25 concurrent clients, 0 errors
  (`tests/unit/test_db_backend.py::test_postgres_concurrent_orchestrators`,
  gated on `ARK_TEST_DATABASE_URL`).
- ✅ `sqlite` dev path still boots and passes the existing test suite (control-plane
  DB suite green; full-suite failure set identical before/after this change).
- ✅ Migration up/down verified (`alembic upgrade head` → `downgrade base` →
  re-`upgrade head` on live Postgres; `projectevent.id` is SERIAL).

---

### Phase 3 — Artifact storage + state projection  **[CODE]**
**Goal:** remove the last shared-FS assumption. The dashboard no longer reads the
orchestrator's disk. Binary artifacts (PDFs, figures) move to an object store served
via a proxy (presigned later); the state the dashboard needs is **projected** into
the control-plane DB by the orchestrator.

> **Decision records:** [`ADRs/0012-artifact-store-seam.md`](ADRs/0012-artifact-store-seam.md)
> (artifact blobs via an `ArtifactStore` seam, proxy now / presigned later) and
> [`ADRs/0013-state-db-projection.md`](ADRs/0013-state-db-projection.md)
> (control-plane state as a DB projection of orchestrator-local files).

**Design decisions (locked 2026-07-02)**

| Question | Decision | Why |
|---|---|---|
| State: blob, DB, or FS? | **DB projection** ([ADR-0013](ADRs/0013-state-db-projection.md)) | Structured, polled, queryable state belongs in the DB, not an opaque blob; extends the existing event-store pattern. |
| Serve blobs how? | **Proxy now, presigned later** ([ADR-0012](ADRs/0012-artifact-store-seam.md)) | Proxy needs nothing of the user's bucket (no CORS) and unifies Local + Object; presigned is a drop-in via `url()` for scale. |
| State ownership? | **Projection, not source of truth** | Orchestrator keeps local YAML for its own crash recovery ([ADR-0007](ADRs/0007-checkpoint-resume-ownership.md)); it *pushes* a copy for the dashboard. Changes stay additive. |
| Bucket ownership? | **Dedicated `artifact_store` config, default `local`** | Orthogonal to launcher × experiment-backend; creds default to the cloud backend's if unset. |

**Consumer trace that scoped this phase** (what the dashboard actually reads):
- `paper_state.yaml` live fields (`score`, `score_history`, `iteration`, `phase`,
  `status`) **already** flow via the Phase-1 status endpoint into `Project` columns;
  the disk reads (`routes.py:903–960`) are legacy fallback to delete.
- `findings.yaml` / `action_plan.yaml` / `memory.yaml` are **export-ZIP-only**
  (`routes.py:3119–3128`) — no live render → one generic projection table suffices.
- `agent_steps.jsonl` is **not consumed by the dashboard at all** → excluded from
  this phase (no table, no endpoint).

**Tasks**
1. **`ArtifactStore` seam** (`ark/artifacts/`, new): `put(key, stream)` /
   `open(ref)` / `url(ref)` (`None` ⇒ caller proxies). Implementations:
   - `LocalArtifactStore` (filesystem, rooted at the project dir; `url()` → `None`) —
     default for local dev / SLURM on a shared mount.
   - `ObjectArtifactStore` (S3 / GCS / Azure Blob — **prefer the user's own bucket**);
     `url()` returns `None` for now so it proxies, presigned-ready.
   - Local goes through the **same seam** (no disk-read shortcut) so CI exercises it.
2. **Blob path:** `Artifact` DB model + Alembic revision; activate the stubbed
   `POST /v1/projects/{id}/artifacts` (`api.py:213`) + `register_artifact`
   (`ark/controlplane/`); orchestrator **pushes eagerly** (upload + register right
   after each PDF/figure). Dashboard PDF/figure/ZIP routes resolve via the store:
   `store.url()` → redirect, else proxy `store.open()`.
3. **State path:** generic `ProjectStateDoc(project_id, name, data, updated_at)` table
   + Alembic revision; `PUT /v1/projects/{id}/state/{name}` (upsert) and
   `GET …/state` (all, for ZIP). Orchestrator pushes `paper_state`/`action_plan`/
   `findings`/`memory` as projections (local YAML unchanged). Drop the dashboard's
   YAML fallbacks and the disk reads in the ZIP builder.
4. **Config:** new `artifact_store` block (`type: local` default), validated in
   `validate_config()` (`ark/compute/__init__.py`), orthogonal to the compute matrix.
5. **Delete the rsync-back bridge** in `orchestrator.py::poll_orchestrator` /
   `teardown` once blobs + state flow through the API + store.

**PR breakdown** (each independently mergeable, default-off):
1. `ArtifactStore` interface + `LocalArtifactStore` + `artifact_store` config +
   `validate_config` (scaffolding, no behavior change).
2. `Artifact` model + migration + activate `/v1/artifacts` + orchestrator
   publishes PDF/figures + dashboard resolves the PDF/uploaded-PDF via the store
   with a disk fallback (regression-identical on local). ✅ DONE
3. `ProjectStateDoc` + migration + `/v1/state` + orchestrator push + dashboard
   readers/ZIP read state from the DB projection + ZIP PDF from the store (the
   ZIP mixes blobs and state, so it converges here). ✅ DONE. *Remaining:* the
   ZIP still reads source/code/results off disk (fine for local/SLURM); a full
   no-shared-FS bundle for remote needs server-side store config (PR4/PR5).
4. `ObjectArtifactStore` (S3 + GCS + Azure) behind config — the acceptance-bar PR.
   ✅ DONE. Lazy provider-client seam (SDK imported on first `put`/`open`, so the
   factory never needs a cloud SDK); `url()` still `None` (proxy). Creds come from
   each SDK's standard env chain — the same env the cloud backend runs in (ADR-0012)
   — with optional config overrides (region/endpoint_url/project/account_url/
   connection_string). SDKs behind the `object` extra. *Remaining for the acceptance
   bar:* an end-to-end run wiring the store into a remote orchestrator (Phase 5) —
   the code path is exercised by unit tests against an in-memory client here.
5. Delete the rsync bridge. ✅ DONE. The cloud orchestrator is now control-plane-
   wired like the SLURM/local paths: `run_orchestrator` passes `--control-plane-url`
   + `--project-id` on argv and carries the bearer token only in the RAM-disk `.env`,
   so status/state/artifacts flow over the /v1 API during the run. `poll_orchestrator`
   is now a pure SSH liveness probe (RUNNING/STOPPED/UNKNOWN); the poller reads the
   terminal outcome from the DB (the orchestrator POSTs done/failed/stopped at run
   end) and only marks `failed` when a remote process vanishes *without* a terminal
   report (crash safety-net). Every `sync_from_backend` call for orchestrator state is
   gone — `orchestrator.py` poll/teardown and the four `app.py` poller syncs. The
   experiment-`results` sync (`execution.py`/`pipeline.py`) is a different feature and
   untouched. *Requires* a configured `control_plane_url` for cloud (no shared FS/DB);
   without it the run is blind and is warned at launch. *Behavior change:* cloud
   done/accept notifications now follow the orchestrator's self-reported DB status
   (same as SLURM/local CP-wired), so the poller no longer emits the "done" telegram/
   email on that path. *Remaining (Phase 5):* the export ZIP's source/code/results
   still read off disk, so a fully no-shared-FS remote ZIP needs the server-side store
   wiring.
6. *(later, non-blocking)* presigned `url()` + dashboard redirect.

**SLURM check:** on a shared HPC filesystem, `LocalArtifactStore` points at the
existing project dir and its `url()` returns `None` (dashboard proxies from disk) —
zero behavior change for SLURM users.

**Acceptance**
- A run with `ObjectArtifactStore` produces a dashboard-viewable PDF with **no
  shared filesystem** between orchestrator and control plane.
- Project state + export ZIP render from the DB, with no read of the orchestrator's
  disk.
- SLURM/local runs still work via `LocalArtifactStore` (proxy from disk).

---

### Phase 4 — Unify the Layer-2 launcher seam  **[CODE]**
**Goal:** generalize the ad-hoc launch/poll/cancel logic in `jobs.py` into a
`JobLauncher` abstraction that parallels `ComputeBackend`, so launching the
orchestrator on `local` / `slurm` / `cloud` / `k8s` is a config switch. Fold the
`OrchestratorCloudBackend` behavior in behind it. Still no new infra — this is a
refactor that makes Phases 5–7 small.

**Tasks**
1. Define `JobLauncher` (`ark/launcher/base.py`, new): `launch()`, `poll()`,
   `cancel()`, returning an opaque handle (generalizing today's `local:{pid}` /
   `cloud:{pid}` / SLURM job-id strings).
2. Implement `LocalJobLauncher` (systemd/subprocess — port `launch_local_job` /
   `poll_local_job` / `cancel_local_job`) and `SlurmJobLauncher` (port
   `submit_job` / `poll_job` / `cancel_job` / `cancel_project_sub_jobs`),
   **behavior-identical** to today.
3. Wrap `OrchestratorCloudBackend` as `CloudVmJobLauncher`.
4. Route the webapp through a launcher factory keyed on
   `orchestrator_compute_backend.type` (mirror `ComputeBackend.from_config`'s
   `is_orchestrator` dispatch).
5. Extend `validate_config()` for the full Layer-2 × Layer-1 matrix + add tests.

**SLURM check:** `SlurmJobLauncher` is a straight port; a golden test asserts the
rendered `sbatch` script and submission path are unchanged.

**Acceptance**
- `launch/poll/cancel` dispatch purely by config type.
- Regression tests prove local + SLURM launchers behave exactly as before.
- Config-matrix validation tests pass (including the existing
  orchestrator-cloud-with-slurm-experiments rejection).

---

### Phase 5 — Remote orchestrator over the API  **[INFRA]**
**Goal:** actually run the orchestrator in the user's cloud, talking home only
via the Phase-1 API and Phase-3 artifact store. This is where the vision goes
live for VM users.

**Tasks**
1. Generalize `CloudVmJobLauncher` beyond GCP to AWS + Azure (reuse
   `cloud/{aws,azure}.py` provisioning), **or** jump straight to SkyPilot
   (Phase 6) as the provisioner and treat this phase as GCP-only proof.
2. Bake an orchestrator image (extend `docker/Dockerfile.job`) or a VM image with
   OpenHands + Claude/Gemini CLIs + LaTeX preinstalled.
3. Inject secrets via the **current long-lived-key model** (encrypted at rest in
   the control-plane DB → job env / RAM disk, as in `orchestrator.py` today).
   Ensure teardown wipes them.
4. Wire the reaper (`scripts/ark_vm_reaper.sh`) for VM lifecycle / cost safety;
   integrate with the intervention gate (`bulk_compute`, `spend`).

**SLURM check:** `type: slurm` orchestrator + experiments remains fully
selectable and untouched.

**Acceptance**
- End-to-end run with orchestrator **and** experiments in the user's cloud;
  control plane reachable only over HTTPS; dashboard + Telegram HITL work.
- No shared FS, no shared DB file.
- SLURM and local paths still pass their suites.

---

### Phase 6 — SkyPilot provisioning  **[INFRA]**
**Goal:** replace hand-rolled `cloud/{aws,gcp,azure}.py` SSH/rsync/marker-file
provisioning with [SkyPilot](https://github.com/skypilot-org/skypilot), which
does cross-cloud VM + K8s provisioning, spot, and teardown out of the box —
covering the "both users" requirement (AWS/GCP/Azure for solo, BYO-K8s for orgs)
from a single abstraction.

**Tasks**
1. Add `type: skypilot` for **Layer 1** (experiments): a `SkyPilotBackend`
   (`ark/compute/skypilot.py`) implementing the `ComputeBackend` interface via
   SkyPilot task launch + `sky down`.
2. Add `type: skypilot` for **Layer 2** (orchestrator) as a `JobLauncher`.
3. Run both hand-rolled and SkyPilot paths **in parallel behind config** until
   SkyPilot reaches parity (spot, retries, teardown, cost labels). Only then
   consider deprecating the bespoke cloud backends.
4. Map the intervention gate + reaper concepts onto SkyPilot lifecycle.

**SLURM check:** SkyPilot is additive; SLURM backends untouched. (Note: SkyPilot
can also target existing K8s/Slurm-like clusters — evaluate, but do not replace
the native `slurm` backend.)

**Acceptance**
- `type: skypilot` runs experiments and the orchestrator across ≥2 clouds and a
  BYO-K8s cluster.
- Teardown/spot verified; no orphaned resources after a run or a crash.

---

### Phase 7 — Kubernetes launcher (enterprise)  **[INFRA]**
**Goal:** first-class BYO-Kubernetes for orgs — orchestrator as a K8s `Job`,
experiments as child Jobs. May be delivered *via* SkyPilot's K8s support or a
native client; decide in Phase 6.

**Tasks**
1. Accept a user-supplied kubeconfig / in-cluster service account.
2. `K8sJobLauncher` (Layer 2) + `K8sBackend` (Layer 1): submit Jobs, stream logs,
   handle completion/failure, clean up.
3. Secrets as K8s `Secret`s (still the long-lived-key model for now).
4. Helm chart for a repeatable install in the org's cluster.

**Acceptance**
- An org with only a kubeconfig runs a project end-to-end (orchestrator + child
  experiment Jobs) with the control plane external.

---

### Phase 8 — Delegated credentials (future, flagged)  **[FUTURE]**
**Not scheduled.** Long-lived keys (per the locked decision) block most
enterprise buyers, who won't store cloud keys in a vendor DB. Design the
Phase-5/7 secret-injection seam so these can drop in **without launcher rework**:
- AWS **AssumeRole** (external-ID), GCP **service-account impersonation** /
  workload identity, Azure **managed identity**.
- A self-hosted "ARK runner" (CI-runner model) so keys never transit our servers.

Track as the enterprise-readiness follow-up; revisit after Phase 7.

## 6. Cross-cutting concerns

### SLURM preservation (hard invariant)
- `type: slurm` must remain selectable for **both** the orchestrator launcher and
  the experiment backend at every phase.
- Phase 1 changes *how* a SLURM job reports state (API, not shared DB) but not
  *that* SLURM is used. Phase 4 ports the SLURM launcher behind `JobLauncher`
  with a golden-file test on the rendered `sbatch` script.
- SkyPilot / K8s are **additive** and must never replace the native `slurm` path.

### Secrets
- **Now:** long-lived keys, encrypted at rest (the `User.encrypted_keys` seam
  exists), injected into the job env / RAM disk, wiped on teardown. Keep scoping
  tight and support rotation.
- **Later (Phase 8):** delegated roles / runner so keys never leave the user's
  account.

### HITL / decisions
Unchanged for users. The orchestrator posts a `PendingDecision` over the API and
polls for the answer; the control plane relays via Telegram / dashboard exactly
as today. The intervention gate (`ark/intervention/`, config in
`config.example.yaml`) continues to guard `bulk_compute` (VM provisioning),
`spend`, `credentials`, etc.

### Backward compatibility
Single-node local dev — SQLite + `LocalArtifactStore` + `LocalJobLauncher` +
loopback API — must work at **every** phase. All new backends are config-selected
and default off.

### Testing
- Regression/golden tests for the SLURM and local paths (Phase 4).
- Config-matrix validation tests (Phase 4).
- A fake control-plane client + fake `ArtifactStore` for orchestrator unit tests
  without network.
- CI already skips `network`/`gcp` marks (`.github/workflows/ci.yml`); add
  `skypilot`/`k8s` marks similarly.

## 7. Build vs. buy

- **SkyPilot — recommended (Phase 6).** Purpose-built for "run jobs on whatever
  cloud/cluster the user brings." Replaces most of `cloud/{aws,gcp,azure}.py` and
  yields the VM *and* K8s paths from one abstraction — directly serving the "both
  users" requirement. Adopt behind config, prove parity, then deprecate bespoke
  backends.
- **Temporal — optional, evaluate later.** Durable orchestration; its worker
  model overlaps with the Layer-2 launcher and would replace the hand-rolled
  `checkpoint.yaml` crash-recovery. Heavyweight — only pursue if
  resumability/reliability becomes a real pain point. Not on the critical path.
- **Kubernetes native client — Phase 7.** Only if SkyPilot's K8s support proves
  insufficient for enterprise needs (RBAC, quotas, network policy).

## 8. Key files (reference map)

| Area | Path |
|---|---|
| Compute factory + `validate_config` | `ark/compute/__init__.py` |
| Compute base (Layer 1) | `ark/compute/base.py` |
| Experiment cloud backends | `ark/compute/cloud/{base,aws,gcp,azure}.py` |
| Orchestrator-on-VM (GCP) | `ark/compute/cloud/orchestrator.py` |
| SLURM / local launchers (Layer 2) | `website/dashboard/jobs.py` |
| Control-plane DB + models | `website/dashboard/db.py` |
| Dashboard routes / API host | `website/dashboard/routes.py`, `app.py` |
| Orchestrator core | `ark/orchestrator/core.py`, `ark/orchestrator/state.py` |
| Intervention gate | `ark/intervention/` |
| Containers | `docker/Dockerfile.job`, `docker/Dockerfile.webapp` |
| VM reaper | `scripts/ark_vm_reaper.sh` |
| Config template | `config.example.yaml` (`orchestrator_compute_backend`, `experiment_compute_backend`, legacy `compute_backend`) |

## 9. New components introduced by this plan

| Component | Path (new) | Phase |
|---|---|---|
| Control-plane API client | `ark/controlplane/client.py` | 1 |
| Artifact store interface | `ark/artifacts/` | 3 |
| State projection (`ProjectStateDoc`) + `/v1/state` | `website/dashboard/{db,api}.py` | 3 |
| Artifact model + `/v1/artifacts` (activate stub) | `website/dashboard/{db,api}.py` | 3 |
| Job launcher abstraction | `ark/launcher/base.py` + impls | 4 |
| SkyPilot backend/launcher | `ark/compute/skypilot.py` | 6 |
| K8s backend/launcher + Helm chart | `ark/compute/k8s.py`, `deploy/` | 7 |

---
*Phases 1–4 are code-only and unblock the infra work; do them first, in order.
Keep this document updated as the source of truth for the BYOC migration.*
