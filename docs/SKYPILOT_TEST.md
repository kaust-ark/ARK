# SkyPilot Acceptance — Test Runbook

End-to-end procedure for the PR5 acceptance gate (folded Phases 5+6, ADR-0010).
Runs on **a machine with cloud credentials** (not a bare CI box). The offline
half is free and runs anywhere; the real cross-cloud legs cost money.

Driver: [`scripts/skypilot_acceptance.sh`](../scripts/skypilot_acceptance.sh) ·
Suite: [`tests/integration/test_skypilot_acceptance.py`](../tests/integration/test_skypilot_acceptance.py) ·
Plan: [`SKYPILOT_PLAN.md` §5](../SKYPILOT_PLAN.md).

---

## Phase 0 — Base prerequisites (one-time)

```bash
python3 --version                 # must be ≥ 3.10
git clone <ark-repo> && cd ARK    # or cd into your checkout
python3 -m venv .venv312 && source .venv312/bin/activate
pip install -e '.[skypilot]'                 # ark + skypilot>=0.6
pip install 'skypilot[gcp,aws,kubernetes]'   # SDK plugins for the clouds you'll test
```
> For the **orchestrator** leg (Phase 7) also: `pip install -e '.[research,webapp]'`.

---

## Phase 1 — Install the cloud CLIs

Install a CLI **only for each cloud you'll test** (need ≥2 clouds + one K8s
context for §5). SkyPilot shells out to these, so `sky check` fails without them.

**AWS CLI v2**
```bash
# macOS:
brew install awscli
# Linux (x86_64):
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install
aws --version
```

**Google Cloud SDK (`gcloud`)**
```bash
# macOS:
brew install --cask google-cloud-sdk
# Linux (Debian/Ubuntu):
curl -sSL https://sdk.cloud.google.com | bash && exec -l $SHELL
gcloud --version
```

**Azure CLI (`az`)** — only if testing Azure
```bash
# macOS:
brew install azure-cli
# Linux (Debian/Ubuntu):
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
az --version
```

**kubectl** — for the BYO-Kubernetes leg
```bash
# macOS:
brew install kubectl socat
# Linux:
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
sudo apt-get install -y socat        # SkyPilot's K8s path uses socat for port-forward
kubectl version --client
```

---

## Phase 2 — Authenticate each cloud

```bash
# AWS
aws configure                         # access key, secret, default region
# GCP
gcloud auth login
gcloud auth application-default login
gcloud config set project <your-project-id>
# Azure
az login
# BYO-Kubernetes (EKS/GKE/on-prem — any cluster)
kubectl config use-context <your-cluster-context>
kubectl get nodes                     # confirm reachability
```

---

## Phase 3 — Let SkyPilot verify it can reach them

```bash
sky check
```
Each target cloud must print **`enabled`**. If one is disabled, `sky check` names
what's missing (a CLI, a credential, or `socat`). Fix it before continuing — the
driver auto-detects clouds from this output.

---

## Phase 4 — Offline invariant first (free, no clouds)

```bash
scripts/skypilot_acceptance.sh --offline-only
```
Expect `PARITY: green`. If it fails, stop and fix — do not provision.

---

## Phase 5 — Real cross-cloud acceptance (Layer-1 experiments)

```bash
scripts/skypilot_acceptance.sh                              # auto-detect enabled clouds
scripts/skypilot_acceptance.sh --clouds aws,gcp,kubernetes  # or name them explicitly
```
Per cloud, automatically: provision cheapest CPU node → assert **UP** + state
persisted → prove `ssh <cluster>` reachability → tear down → assert **no orphan**.

Optional tuning (env vars):
```bash
ARK_SKYPILOT_ACCEPTANCE_SPOT=1                 # provision on spot
ARK_SKYPILOT_ACCEPTANCE_INSTANCE_AWS=t3.small  # pin an instance type
ARK_SKYPILOT_ACCEPTANCE_REGION_GCP=us-central1 # pin a region
ARK_SKYPILOT_ACCEPTANCE_IDLE=5                 # idle-autostop backstop (minutes)
```

---

## Phase 6 — Verify teardown / no orphans (money safety)

The driver ends with an orphan sweep:
- **`no orphaned ark-* clusters — teardown verified`** → good.
- **`ORPHANS DETECTED`** → confirm the reap prompt, or `--yes` to auto-reap.

Always eyeball it once yourself:
```bash
sky status --refresh              # should list NO ark-* clusters
```

---

## Phase 7 — Orchestrator launcher + crash teardown (manual)

Phases 5–6 cover the **Layer-1 experiment** backend. Phase 7 covers the two §5
bullets the automated suite can't: the **Layer-2 orchestrator** running on a
SkyPilot cluster and reporting home over HTTPS, and **crash-path teardown** via
autostop (no orphan when the run dies without a clean Stop).

### 7a — Orchestrator end-to-end (dashboard + /v1 + Telegram HITL)

The orchestrator runs `python -m ark.orchestrator` **on a SkyPilot VM**. That VM
shares no filesystem/DB with your dashboard — it reports status/state/artifacts
back over the `/v1` HTTP API. So the single hard requirement is:

> **The control-plane URL must be reachable over HTTPS *from the cloud VM*.**
> `localhost` will not work — the VM is remote.

**Step 1 — install the orchestrator extras and bootstrap ark.** The dashboard
(`ark webapp`) and orchestrator ship in the `research,webapp` extras, not the base
`skypilot` install. Install them, then run `ark webapp` **once** to generate the
config file you'll edit in Step 3 — it doesn't exist until the first run:
```bash
source .venv312/bin/activate                 # the venv from Phase 0
pip install -e '.[research,webapp]'          # ark webapp + orchestrator deps
ark webapp                                   # first run writes .ark/webapp.env, then serves
# Ctrl-C once you see it listening — you'll restart it in Step 4 with the URL set.
```
> `ark webapp` creates `.ark/webapp.env` on **first-ever** run (`_write_default_env`),
> including a blank `CONTROL_PLANE_URL=` line. That's the chicken-and-egg: you must
> start ark once to get the file before you can point it at your public URL. `.ark/`
> resolves to the `.ark` dir under the ARK root (`get_config_dir`).
>
> **If `.ark/webapp.env` already exists** (an older build wrote it before this key
> existed), the defaults are **not** re-merged — the `CONTROL_PLANE_URL` line will be
> absent entirely. Don't assume it's there; you'll add it in Step 3. Confirm with:
> ```bash
> grep -n CONTROL_PLANE_URL .ark/webapp.env || echo "absent — add it in Step 3"
> ```

**Step 2 — expose the control plane over HTTPS.** Pick one:

- **A) Use a deployed dashboard** (e.g. the prod idea2paper host) — already public
  HTTPS, nothing to tunnel. Set its URL in Step 3.
- **B) Tunnel your local dashboard.** The webapp listens on port **9527**. Open a
  public HTTPS tunnel to it:
  ```bash
  # Cloudflare (no account needed for a quick tunnel):
  cloudflared tunnel --url http://localhost:9527
  # …or ngrok:
  ngrok http 9527
  ```
  Copy the `https://…` URL it prints.

**Step 3 — point the control plane at that URL.** Edit `.ark/webapp.env` and set the
public URL **with the `/v1` suffix**. If the `CONTROL_PLANE_URL` line is missing
(see the Step 1 note — likely on an older install), **add it**; if present, edit it:
```bash
# .ark/webapp.env
CONTROL_PLANE_URL=https://<your-public-host>/v1
```
> If `CONTROL_PLANE_URL` is empty, `control_plane_transport` returns `("","")` and
> the launcher warns *"launched without a control-plane URL … the dashboard will
> not see progress"*. Seeing that warning means Step 3 didn't take — the run will
> be blind. Fix it before launching.

**Step 4 — (re)start the dashboard** with the URL now set (foreground is easiest
for watching logs):
```bash
ark webapp                        # serves on 0.0.0.0:9527
# (or run it as a service: ark webapp install ; ark webapp logs -f)
```

**Step 5 — configure Telegram HITL** (one-time):
```bash
ark setup-bot                     # paste BotFather token; it auto-detects chat ID
```

**Step 6 — create a project on SkyPilot.** In the dashboard create form, add the API
keys the run needs (e.g. `ANTHROPIC_API_KEY`), pick **🚀 SkyPilot** under *Experiments
Backend*, then **Launch**. That one choice runs the whole project on SkyPilot — it also
sets the (hidden) orchestrator backend to `skypilot`, so both layers land on SkyPilot
and the cloud is auto-selected. Restart/Continue expose the same **🚀 SkyPilot** option.

> Behind the scenes, selecting SkyPilot submits `compute_backend=skypilot` **and**
> `orchestrator_compute_backend=skypilot`. `_resolve_compute_config` shapes a
> `{type: skypilot, conda_env: …}` block into the project's `config.yaml` for both
> layers, and `orchestrator_launcher_for` hands the orchestrator block to
> `SkyPilotVmJobLauncher`, which `sky.launch`es cluster `ark-orch-<project>` detached.
> Auth note: use **API-key** auth. Gemini *OAuth-session* files aren't provisioned
> onto the cluster yet (deferred) — if a project is Gemini-OAuth-only you'll see a
> loud warning and agent calls will fail auth. Set a Gemini/Google API key instead.

> **Fallback for a pre-existing project** (created before it was on SkyPilot, or if you
> prefer not to use the form): set the backend via API or DB, then **Launch**.
> ```bash
> # API (also launches) — needs your session cookie; skypilot:gcp pins a cloud:
> curl -X POST http://localhost:9527/api/projects/<project_id>/restart \
>   -H 'Content-Type: application/json' -b <your-session-cookie> \
>   -d '{"compute_backend":"skypilot","orchestrator_compute_backend":"skypilot"}'
> # …or DB (no cookie):
> sqlite3 .ark/data/webapp.db "UPDATE project SET compute_backend='skypilot', \
>   orchestrator_compute_backend='skypilot' WHERE id='<project_id>';"
> ```

**Step 7 — verify the acceptance criteria.** Tick each:

| Check | How |
|---|---|
| Cluster provisioned | `sky status` shows `ark-orch-<project>` **UP** |
| Reports home over /v1 (no shared FS/DB) | Dashboard shows **live progress / iterations** — the only way it can, since the VM shares nothing with the dashboard except the HTTP API |
| HTTPS-only boundary | The VM reaches you solely via `CONTROL_PLANE_URL`; no shared DB file, no shared filesystem |
| Telegram notifications | You receive score/phase/agent messages in the chat |
| HITL steering | Send an instruction from the dashboard chat (or Telegram); confirm the running iteration picks it up |

**Step 8 — clean stop → no orphan.** Click **Stop** in the dashboard. This calls
`cancel()` → `sky down ark-orch-<project>` (in a background thread) → cleanup.
Verify:
```bash
sky status --refresh              # ark-orch-<project> must be GONE
```

### 7b — Crash-path teardown (autostop, no orphan)

Phase 7a tests the **clean** Stop. This tests the **crash** path — the run dies
without `teardown()`/Stop ever firing — which relies purely on the **autostop-down**
window SkyPilot applies at launch. Two clusters, two policies:

- **Experiment cluster (Layer-1):** autostop-down is **required, no opt-out** — it's
  launched *from the orchestrator VM*, so the control plane has no record to reap it
  with. Autostop is the **only** reaper. This is the important one to prove.
- **Orchestrator cluster (Layer-2):** autostop-down is a default-on safety-net that
  starts counting only **after** the detached orchestrator job exits, so a live run
  is never reaped mid-flight.

**Confirm the window is actually set** (before crashing anything):
```bash
sky status                        # the AUTOSTOP column shows e.g. "5m (down)"
```
The launch log also prints *"Cluster '…' will auto-down after N idle minutes"*.

**Procedure (experiment cluster — the critical case):**

1. Use a **short** window so the wait is minutes, not an hour:
   ```bash
   ARK_SKYPILOT_ACCEPTANCE_IDLE=5 \
     ARK_SKYPILOT_ACCEPTANCE_SPOT=1 \
     ARK_SKYPILOT_ACCEPTANCE_CLOUDS=aws \
     pytest tests/integration/test_skypilot_acceptance.py -m skypilot -s \
       -k provision_reachable --no-header -x &
   TEST_PID=$!
   ```
2. Once `sky status` shows the cluster **UP**, **simulate a crash** — kill the
   driver hard so neither `teardown()` nor its `atexit` hook runs:
   ```bash
   kill -9 $TEST_PID
   ```
   (Equivalently, for a real run: `kill -9` the webapp/orchestrator process and do
   **not** click Stop.)
3. Confirm the cluster is orphaned *at this instant* (nothing reaped it yet):
   ```bash
   sky status                     # still UP — no clean teardown happened
   ```
4. Wait the idle window plus SkyPilot's autostop check cadence (~the window +
   a few minutes). Then:
   ```bash
   sky status --refresh           # the cluster must now be auto-DOWNED / GONE
   ```
   A `GONE` result with no manual `sky down` proves the required autostop-down
   backstop reaped a crashed run — **no orphan after a crash**, not just after Stop.

**Spot pre-emption (optional, best-effort).** True pre-emption is hard to force.
`use_spot: true` + `retry_until_up` rides out capacity loss *during provisioning*.
To observe a mid-run pre-emption, terminate the spot instance from the cloud
console, then confirm either SkyPilot's managed recovery re-provisions or the
autostop backstop still cleans up — and that `sky status --refresh` ends clean.

**Always finish with an orphan sweep** after crash testing (short windows make
leftovers cheap, but verify):
```bash
sky status --refresh
scripts/skypilot_acceptance.sh --offline-only   # re-confirm the invariant still green
```

---

## Phase 8 — Flip the ADR (the gate)

Only after Phases 4–7 are green **across ≥2 clouds + a BYO-K8s context, zero
orphans**:
- `ADRs/0010-skypilot-provisioning.md`: status `Accepted as direction` → **`Accepted`**.
- `SKYPILOT_PLAN.md` PR5 row → ✅.

Do **not** flip on an offline-only or single-cloud pass.

---

## Quick reference

| Command | Purpose |
|---|---|
| `aws --version` / `gcloud --version` / `az --version` / `kubectl version --client` | Confirm CLIs installed |
| `sky check` | Confirm SkyPilot can reach each cloud |
| `scripts/skypilot_acceptance.sh --offline-only` | Free parity check |
| `scripts/skypilot_acceptance.sh --clouds aws,gcp,kubernetes` | Full Layer-1 gate |
| `ark webapp` / `ark setup-bot` | Start dashboard (:9527) / configure Telegram |
| `cloudflared tunnel --url http://localhost:9527` | Expose local control plane over HTTPS |
| `sky status` / `sky status --refresh` | Check AUTOSTOP column / find orphans |
| `sky down -y <cluster>` | Manually reap a cluster |
