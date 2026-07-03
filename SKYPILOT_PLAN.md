# SkyPilot Provisioning Plan — folded Phase 5 + 6

> **Status:** In progress.
> **Parent doc:** [`CLOUD_BACKEND_PLAN.md`](CLOUD_BACKEND_PLAN.md) (Phases 5 & 6).
> **Decision record:** [`ADRs/0010-skypilot-provisioning.md`](ADRs/0010-skypilot-provisioning.md).

## 1. What this is

`CLOUD_BACKEND_PLAN.md` sequences Phase 5 (**[INFRA]** — run the orchestrator in
the user's cloud) and Phase 6 (**[INFRA]** — adopt SkyPilot as the cross-cloud /
K8s provisioner) separately, with Phase 5 offering a fork: either hand-roll
AWS + Azure VM provisioning (extending `ark/compute/cloud/{aws,azure}.py`), or
"jump straight to SkyPilot and treat Phase 5 as a GCP-only proof."

**We are taking the fold.** SkyPilot becomes the provisioner from the start, for
**both** compute layers, so no bespoke AWS/Azure VM code is ever written. This
merges Phases 5 and 6 into the single track described here.

- The existing hand-rolled **GCP `cloud`** path (`ark/compute/cloud/gcp.py`,
  `ark/compute/cloud/orchestrator.py`, `ark/launcher/cloud.py`) stays behind
  config, **default and untouched**, until SkyPilot proves parity (spot, retries,
  teardown, cost labels). Only then do we consider deprecating it.
- The native **`slurm`** path is never replaced (hard invariant, `CLOUD_BACKEND_PLAN.md` §6).
- Everything is **additive and default-off**, selected by `type: skypilot`.

## 2. Why SkyPilot is a new implementation, not a wrapper

The existing cloud backends speak **SSH + rsync + marker-file polling + explicit
`gcloud … delete`**. SkyPilot has its own task model — `sky.launch(task)` where a
task carries `resources` (cloud/accelerators/spot), `workdir` / `file_mounts`
(code + secrets), a `setup:` block (deps), and a `run:` command; teardown is
`sky.down()` / autostop. Shoehorning SSH into that model would forfeit the reason
to adopt SkyPilot. So the SkyPilot backends are **fresh implementations** of the
two existing seams, not adapters over `OrchestratorCloudBackend` / `CloudBackend`:

| Seam | Interface | New impl | Reference (do not wrap) |
|---|---|---|---|
| Layer 1 — experiments | `ComputeBackend` (`ark/compute/base.py`) | `SkyPilotBackend` (`ark/compute/skypilot.py`) | `ark/compute/cloud/gcp.py` |
| Layer 2 — orchestrator | `JobLauncher` (`ark/launcher/base.py`) | `SkyPilotVmJobLauncher` (`ark/launcher/skypilot.py`) | `ark/launcher/cloud.py` |

## 3. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **New top-level `type: skypilot`** for both layers (not `type: cloud` + `provider: skypilot`). | Matches the plan text (Phase 6 tasks 1–2); gives a clean `skypilot:` launcher handle prefix distinct from `cloud:`; keeps the GCP `cloud` path isolated for the parity comparison. |
| 2 | **`skypilot` orchestrator + `slurm` experiments → rejected** in `validate_config`'s matrix. `skypilot`+`skypilot`, `skypilot`+`cloud`, `cloud`+`skypilot` → valid. | Same invariant as the existing `("cloud","slurm")` rejection: a cloud-provisioned orchestrator has no network path to an on-prem SLURM cluster. |
| 3 | **`setup:` block before a baked image.** Start by installing deps via the SkyPilot `setup:` block; bake an orchestrator image later (PR4+) purely for launch speed. | Faster iteration, no container-registry infra to stand up first. Image is a drop-in optimization once the path works. |

Cost/lifecycle: lean on SkyPilot's built-in **autostop / `--down` idle teardown +
spot** rather than porting `scripts/ark_vm_reaper.sh`; keep the intervention gate
(`bulk_compute`, `spend`) at launch. Secrets keep the **long-lived-key model**
(ADR-0002) — injected via SkyPilot secret file-mounts / env, wiped on teardown.

> **Autostop is not optional for experiment clusters (learned in PR3 review).**
> A Layer-1 SkyPilot *experiment* cluster is launched **from the orchestrator VM**,
> so its SkyPilot state lives on that VM. When a run is stopped/deleted, the control
> plane tears down the *orchestrator* cluster (which it launched and therefore has
> state for) but **cannot `sky down` the experiment cluster** — it has no record of
> it, unlike the GCP `cloud` path where `gcloud delete` works from anywhere. So the
> Layer-1 backend **must** set `--down` autostop when it provisions an experiment
> cluster (PR4); there is no cross-plane teardown fallback.

## 4. PR breakdown (each independently mergeable, default-off)

Mirrors Phase 3's style (interface → impl → impl → go-live → acceptance).

| PR | Scope | Offline-testable |
|---|---|---|
| **PR1** | **Seam scaffolding.** `skypilot` extra in `pyproject.toml`; lazy SDK wrapper (`ark/compute/_sky.py`, mirroring the Phase-3 lazy object-store seam); `skypilot` added to `VALID_ORCHESTRATOR_TYPES` + `VALID_EXPERIMENT_TYPES`; `validate_config` matrix extended (reject `skypilot`+`slurm`); README config block; `skypilot` pytest marker; compute factory raises `NotImplementedError` for `skypilot`. | ✅ config-matrix + factory tests |
| **PR2 ✅** | **Layer 1 `SkyPilotBackend`** (`ark/compute/skypilot.py`) — experiments as a `sky` task: `setup()` launches a named cluster (code via `workdir`, deps via the `setup:` block, cloud/accelerators/spot via `Resources`), the experimenter drives it over SkyPilot's `ssh <cluster>` alias, `wait_for_completion` polls a `/tmp/ark_experiment_done` marker, `sync_from_backend` rsyncs results back, `teardown` → `sky.down`. Factory builds it for the experiment path; the orchestrator path still raises (PR3). ⚠️ The `skypilot` pytest marker + CI `-m "…and not skypilot"` filter added in PR1 are **inert until a test actually carries `@pytest.mark.skypilot`** — any real-provisioning test added here (or in PR3) MUST be marked, or it will run in CI. The PR2 suite is fully mocked (unmarked, CI-safe). | ✅ mock sky SDK; optional local-k8s integration test (`skypilot` mark) |
| **PR3 ✅** | **Layer 2 `SkyPilotVmJobLauncher`** (`ark/launcher/skypilot.py`) — `launch()` builds a `sky.Task` whose `run:` is `python -m ark.orchestrator --control-plane-url … --project-id …` (ARK code via `workdir`, project dir + control-plane token via `file_mounts`, API keys via task `envs`, resources via `Resources`) and `sky.launch(…, detach_run=True, retry_until_up=True)`es a named cluster → handle `skypilot:{cluster}`; the token rides as a **file-mounted secret** (never on argv) wiped after launch. `poll()` via `sky status` → normalized states (UP→running, STOPPED/no-cluster→gone, INIT→unknown; DB authoritative for outcome, mirroring the cloud path). `cancel()` → `sky down` (orchestrator cluster only) + `on_complete` in a background thread (cleanup strictly last). Wired: `launcher_from_handle` (`skypilot:` prefix), the dashboard env label + env-ready fast-path, the webapp's `orchestrator_launcher_for`, and `_resolve_compute_config` (shapes the `type: skypilot` block for both layers). Shared SDK plumbing (async-request blocking, cloud/resource shaping, cluster-name/setup shaping) factored into `ark/compute/_sky.py`. **Review hardening:** `poll()` distinguishes a swallowed transient status error (→ `unknown`, retry) from a genuine empty result (→ `gone`) so a status blip can't false-fail a live run; the project dir mounts to a fixed remote dirname (no project-id interpolation into the shell); `--max-days` is float-coerced; the webapp emits a default orchestrator `setup:` (`pip install -e '.[research]'`) and no longer forwards GCP-namespace `cloud_{region,instance_type,image_id}`; a loud warning fires when a project relies on Gemini OAuth with no API-key fallback (session provisioning is PR4). ⚠️ Orchestrator image/secret-wipe hardening + cost-safety autostop are PR4. | ✅ mock sky SDK (`test_skypilot_launcher.py`, unmarked / CI-safe) |
| **PR4** | **Go-live.** ✅ **Cost-safety autostop** — a shared `resolve_autostop` (`ark/compute/_sky.py`) shapes `{idle_minutes_to_autostop, down}` launch kwargs: **required** (no opt-out, always `down`) for Layer-1 experiment clusters — the sole reap path since no cross-plane teardown exists (§3 box) — and a default-on, opt-out crash safety-net for the Layer-2 orchestrator cluster (fires only after the detached run exits, so a live run is never reaped). Config keys `idle_minutes_to_autostop` (int / `off`) + `autostop_down` documented in README. **Baked image**: already plumbed via `image_id` (`build_resources`); documented as a drop-in launch-speed swap for the `setup:` block — no bake infra built (deferred per §3.3). **Intervention gate**: already mapped to `cloud_provision` in the Layer-1 backend (PR2). ⏸️ **Deferred**: OAuth **session** file-mounts (Gemini `gemini_oauth_json` / Claude) — that auth path is slated for deprecation, so PR4 keeps API-key auth only; spot is already exposed via `use_spot`. | ✅ mocked (autostop assertions in all 3 suites) |
| **PR5** | **Acceptance + parity.** Real run across ≥2 clouds + a BYO-K8s cluster; verify teardown/spot/no-orphans; SLURM + local suites still green. Flip ADR-0010 → Accepted. | ❌ needs real clouds |

## 5. Acceptance (inherits `CLOUD_BACKEND_PLAN.md` §Phase 5 + 6)

- `type: skypilot` runs experiments **and** the orchestrator across ≥2 clouds and
  a BYO-K8s cluster; control plane reachable only over HTTPS; dashboard + Telegram
  HITL work; no shared FS, no shared DB file.
- Teardown/spot verified — no orphaned resources after a run or a crash.
- `slurm` and `local` paths still pass their suites (hard invariant).

## 6. Key files

| Area | Path |
|---|---|
| Compute factory + `validate_config` | `ark/compute/__init__.py` |
| Lazy SkyPilot SDK seam | `ark/compute/_sky.py` (new, PR1) |
| Layer-1 SkyPilot backend | `ark/compute/skypilot.py` (new, PR2) |
| Layer-2 SkyPilot launcher | `ark/launcher/skypilot.py` (new, PR3) |
| Launcher dispatch | `ark/launcher/__init__.py`, `website/dashboard/routes.py::orchestrator_launcher_for` |
| Optional dep + marker | `pyproject.toml` (`skypilot` extra, `skypilot` marker) |
| Config docs | `README.md` (compute-backend reference) |
