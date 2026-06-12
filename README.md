<p align="center">
  <strong>English</strong> &bull; <a href="README_zh.md">中文</a> &bull; <a href="README_ar.md">العربية</a>
</p>

<p align="center">
  <img src="https://idea2paper.org/assets/logo_ark_transparent.png" alt="ARK" width="260">
</p>

<h1 align="center">ARK &mdash; Automatic Research Kit</h1>

<p align="center">
  <em>Offload the labour. Steer the science.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-6-orange.svg" alt="6 Agents">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="11+ Venues">
</p>

<p align="center">
  <a href="https://idea2paper.org/"><strong>Website</strong></a> &bull;
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#requirements">Requirements</a> &bull;
  <a href="#ark-pipeline">Pipeline</a> &bull;
  <a href="#ark-agents">Agents</a> &bull;
  <a href="#cloud-compute">Cloud</a> &bull;
  <a href="#cli-reference">CLI</a>
</p>

---

ARK orchestrates **6 specialized AI agents** to turn a research idea into a paper &mdash; proposal analysis, literature search, Slurm experiments, LaTeX drafting, and iterative peer review &mdash; while you stay in control via **CLI**, **Dashboard**, or **Telegram**.

```
Give it an idea and a venue. ARK handles the rest.
```

## Papers Written by ARK

<table align="center">
<tr>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/marco.pdf"><img src="https://idea2paper.org/assets/paper-marco.png" alt="Budget-Constrained Multi-Modal Research Synthesis" width="320"></a>
<br>
<strong>Budget-Constrained Multi-Modal Research Synthesis via Iterative-Deepening Agentic Search</strong>
<br>
<sub>Template: EuroMLSys</sub>
</td>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/heteroserve.pdf"><img src="https://idea2paper.org/assets/paper-heteroserve.png" alt="HeteroServe" width="320"></a>
<br>
<strong>HeteroServe: Capability-Weighted Batch Scheduling for Heterogeneous GPU Clusters in LLM Inference</strong>
<br>
<sub>Template: ICML</sub>
</td>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/tierkv.pdf"><img src="https://idea2paper.org/assets/paper-tierkv.png" alt="TierKV" width="320"></a>
<br>
<strong>TierKV: Prefetch-Aware Memory Tiering for KV Cache in LLM Serving</strong>
<br>
<sub>Template: NeurIPS</sub>
</td>
</tr>
</table>

---

## Quick Start

```bash
curl -fsSL https://idea2paper.org/install.sh | bash
```

The script:

1. Detects your OS, installs miniforge if missing, builds the `ark-base` and `ark` conda envs, pip-installs ARK editable into `~/ARK`, and installs the OpenHands CLI (the agent runtime, via `uv` — it bundles its own Python 3.12).
2. Asks you for **Anthropic / OpenAI / Gemini API keys** (fill whichever provider(s) you'll use; the Gemini key also powers Deep Research) and an **email for dashboard login**. Press Enter to skip any.
3. Installs the dashboard as a `systemd --user` service on port `9527` (use `--no-webapp` to opt out).
4. Prints a one-time **magic-link URL** for your email — click it once and you're logged into the local dashboard. No SMTP, no Google OAuth.

After that the dashboard at <http://localhost:9527> is the primary UX — create projects, set the model, run, monitor. The CLI also works:

```bash
ark doctor          # verify install
ark new myproject   # interactive project wizard
ark run  myproject
ark monitor myproject
```

Re-run `ark webapp login <email>` anytime for a fresh sign-in link. Full installer flags: [`website/homepage/install.sh --help`](website/homepage/install.sh).

### Start from an Existing PDF

```bash
ark new myproject --from-pdf proposal.pdf
```

ARK parses the PDF with PyMuPDF + Claude Haiku, pre-fills the wizard, and kicks off from the extracted spec.

---

## Requirements

- **Python 3.10+** with `pyyaml` and `PyMuPDF`
- **Agent runtime**: [OpenHands CLI](https://github.com/OpenHands/OpenHands-CLI) (installed via `uv`, bundles its own Python 3.12) &mdash; one runtime that drives Claude / GPT / Gemini / any [LiteLLM](https://docs.litellm.ai/docs/providers) model, selected per project via `model` in `config.yaml`
- **Optional**: LaTeX (`pdflatex` + `bibtex`), Slurm, `google-genai` for AI figures

<details>
<summary><strong>Manual Installation</strong></summary>

The fastest path is the one-line installer in [Quick Start](#quick-start). It runs the steps below for you and prints onboarding hints. To do it by hand:

```bash
# 1. Create the project research-stack template (no ARK code in here —
#    each new project clones this env, so it must stay clean).
conda env create -f environment.yml         # Linux (creates "ark-base")
# OR for macOS:
conda env create -f environment-macos.yml   # macOS (creates "ark-base")

# 2. Install ARK itself into a SEPARATE env (not ark-base).
conda create -n ark python=3.11 -y
conda activate ark
pip install -e .                    # Core
pip install -e ".[research]"       # + Gemini Deep Research & Nano Banana
pip install -e ".[webapp]"         # + dashboard / systemd service support

# 3. Install the OpenHands CLI (the agent runtime). It is a standalone `uv`
#    tool with its OWN bundled Python 3.12 — NOT a pip dependency — and must be
#    on PATH for the orchestrator subprocess to find it.
pip install uv && uv tool install --python 3.12 openhands

# 4. Verify (checks the openhands runtime is on PATH, keys are present, etc.)
ark doctor
```

</details>

---

## ARK Framework

<p align="center">
  <img src="assets/framework.png" alt="ARK Framework" width="900">
</p>

ARK orchestrates three phases &mdash; **Initialization &amp; Research**, **Iterative Development**, and **Iterative Review** &mdash; coordinated through shared memory, a persistent **Goal Anchor** re-injected into every agent call to prevent drift, and human-in-the-loop steering via the web dashboard or Telegram.

---

## ARK Pipeline

ARK runs three phases in sequence. The Review phase loops until the paper reaches the target score.

| Phase | What Happens |
|:------|:-------------|
| **Research** | 5-step pipeline: Setup (conda env) &rarr; Analyze Proposal (researcher) &rarr; Deep Research (Gemini) &rarr; Specialization (researcher) &rarr; Bootstrap (skills &amp; citations) |
| **Dev** | Iterative experiment cycle: plan &rarr; run on Slurm &rarr; analyze &rarr; write initial draft |
| **Review** | Compile &rarr; Review &rarr; Plan &rarr; Execute &rarr; Validate, repeating until score &ge; threshold |

<details>
<summary><strong>Review Loop details</strong></summary>

Each iteration of the Review phase runs **5 steps**:

| Step | Description |
|:-----|:------------|
| **Compile** | LaTeX &rarr; PDF, page count, page images |
| **Review** | AI reviewer scores 1&ndash;10, lists Major &amp; Minor issues |
| **Plan** | Planner creates a prioritized action plan |
| **Execute** | Researcher + Experimenter run in parallel; Writer revises LaTeX |
| **Validate** | Verify changes compile; recompile PDF |

The loop repeats until the score reaches the acceptance threshold &mdash; or you intervene via Telegram.

</details>

---

## ARK Agents

| Agent | Role |
|:------|:-----|
| **Researcher** | Analyzes the proposal, runs the Gemini-backed literature survey, and specializes agent prompts for the project |
| **Reviewer** | Scores the paper against venue standards, generates improvement tasks |
| **Planner** | Turns review feedback into a prioritized action plan; analyzes Dev-phase results |
| **Writer** | Drafts and refines LaTeX sections with DBLP-verified references |
| **Experimenter** | Designs experiments, submits Slurm jobs, analyzes results |
| **Coder** | Writes and debugs experiment code and analysis scripts |

---

## What Sets ARK Apart

| | Other Tools | ARK |
|---|:------------|:----|
| **Control** | Fully autonomous &mdash; drifts from intent, no mid-run correction | Human-in-the-loop: pause at key decisions, steer via Telegram or web |
| **Formatting** | Broken layouts, LaTeX errors, manual cleanup | Venue templates + sub-page length control to hit page limits exactly |
| **Citations** | LLMs fabricate plausible-looking references | API-first BibTeX (DBLP / CrossRef / arXiv) with content&ndash;claim alignment |
| **Review** | Text-only review of the LaTeX source | Visual-grounded: page images **and** source, scored against venue standards |
| **Figures** | Default styles, wrong sizes, no page awareness | Nano Banana + venue-aware canvas, column widths, and fonts |
| **Isolation** | Shared env &mdash; projects interfere with each other | Per-project conda env, sandboxed HOME, full multi-tenant isolation |
| **Integrity** | LLMs simulate results instead of running real experiments | Anti-simulation prompts + builtin skills enforce real execution |

---

<details>
<summary><strong>Environment Isolation</strong></summary>

Each project runs in its own **per-project conda environment**, cloned from a base env at project creation. This ensures full multi-tenant isolation:

- **Sandboxed Python** &mdash; per-project `.env/` directory with its own packages
- **Isolated HOME** &mdash; each orchestrator runs with `HOME` set to the project directory
- **No cross-contamination** &mdash; `PYTHONNOUSERSITE=1` prevents leaking user-site packages
- **Automatic provisioning** &mdash; `ark run` and the Web Portal detect and use the project conda env; the pipeline bootstraps it if missing

```bash
# The conda env is created automatically on first run.
# ark run will detect and use it:
ark run myproject
#   Conda env: /path/to/projects/myproject/.env
```

</details>

<details>
<summary><strong>Skills System</strong></summary>

ARK ships with **builtin skills** &mdash; modular instruction sets that agents load at runtime to enforce best practices:

| Skill | Purpose |
|:------|:--------|
| **research-integrity** | Anti-simulation prompts: agents must run real experiments, not fabricate outputs |
| **human-intervention** | Escalation protocol: agents pause and ask via Telegram before irreversible actions |
| **env-isolation** | Enforces per-project environment boundaries |
| **runtime-sandbox** | Locks each project to its own conda env, `HOME`, and tmp dir at runtime |
| **figure-integrity** | Validates figure content matches data; prevents placeholder or hallucinated plots |
| **page-adjustment** | Maintains page limits by adjusting content density, not deleting sections |

Skills live in `skills/builtin/` and are auto-installed during pipeline bootstrap. Domain skills (e.g., HPC) live in `skills/library/` and are pulled in by the Researcher when relevant.

</details>

---

## CLI Reference

| Command | Description |
|:--------|:------------|
| `ark new <name>` | Create project via interactive wizard |
| `ark run <name>` | Launch the pipeline (auto-detects per-project conda env) |
| `ark status [name]` | Score, iteration, phase, cost |
| `ark monitor <name>` | Live dashboard: agent activity, score trend |
| `ark update <name>` | Inject a mid-run instruction |
| `ark stop <name>` | Gracefully stop |
| `ark restart <name>` | Stop + restart |
| `ark research <name>` | Run Gemini Deep Research standalone |
| `ark config <name> [key] [val]` | View or edit config |
| `ark clear <name>` | Reset state for a fresh start |
| `ark delete <name>` | Remove project entirely |
| `ark setup-bot` | Configure Telegram bot |
| `ark list` | List all projects with status |
| `ark doctor` | Diagnose a self-host install (envs, API keys, webapp) |
| `ark cite-check <name>` | Verify project citations against DBLP / CrossRef |
| `ark cite-search <query>` | Search academic databases for papers |
| `ark webapp install` | Install web dashboard service |
| `ark access {list,add,remove,add-domain,remove-domain}` | Manage Dashboard Cloudflare Access allowlist |

---

## Dashboard

ARK includes a web-based dashboard for managing projects, viewing scores, and steering agents. The dashboard shows **live phase badges** (Research / Dev / Review), per-project conda env status, and real-time cost tracking. It is served from a single FastAPI process that also hosts the homepage &mdash; one port, one systemd unit.

### Configuration

Configured via `.ark/webapp.env` (auto-created on first `ark webapp` run). Set `SMTP_*` for magic-link login, `ALLOWED_EMAILS` / `EMAIL_DOMAINS` to restrict access, and optionally `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` for Google OAuth.

### Management Commands

| Command | Description |
|:--------|:------------|
| `ark webapp` | Start the dashboard in the foreground (useful for debugging). |
| `ark webapp release` | Tag the current code and deploy to the production worktree. |
| `ark webapp install [--dev]` | Install and start as a `systemd` user service. |
| `ark webapp status` | Show status of the systemd service. |
| `ark webapp restart` | Restart the dashboard service. |
| `ark webapp logs [-f]` | View or tail service logs. |

<details>
<summary><strong>Service Details (Prod vs. Dev)</strong></summary>

| | Prod | Dev |
|---|:-----|:----|
| **Port** | 9527 | 1027 |
| **Service Name** | `ark-webapp` | `ark-webapp-dev` |
| **Conda Env** | `ark-prod` | `ark-dev` |
| **Code Source** | `~/.ark/prod/` (pinned) | Current repository (live) |

</details>

### Team Deployment (shared prod)

A team can serve one production instance from a shared, group-writable
directory while every member develops in their own clone and releases with a
single command. Set in each member's shell rc:

```bash
export ARK_RELEASE_ROOT=/shared/path/ARK    # shared checkout: prod worktree, DB, projects
export ARK_CONDA_ROOT=/shared/path/conda    # shared conda (ark-prod / ark-base envs)
export ARK_TOOLS_BIN=/shared/path/tools/bin # shared OpenHands CLI
umask 002                                   # keep new files group-writable
```

With these set, `ark webapp release` from **any member's clone** tags the
release, pushes the tag, updates the shared prod worktree, and installs into
the shared env; the running webapp notices the new `.deployed-tag` marker and
recycles itself within ~30s — no service ownership needed. `ark webapp
install` bakes the shared paths (and the shared DB under
`$ARK_RELEASE_ROOT/.ark/data`) into the generated unit.

<details>
<summary><strong>Direct orchestrator invocation</strong></summary>

```bash
python -m ark.orchestrator --project myproject --mode paper --max-iterations 20
python -m ark.orchestrator --project myproject --mode dev
```

</details>

---

<details>
<summary><strong>Docker Usage</strong></summary>

> [!IMPORTANT]
> The ARK research runtime depends on scientific libraries that are most stable on x86_64. If you are building on an **Apple Silicon (M1/M2/M3)** Mac, you must build for the `linux/amd64` platform.
>
> All ARK Dockerfiles and the `docker-compose.yml` are configured to force `linux/amd64` by default.

**Running with Docker Compose**

```bash
# Start the web portal (builds the image automatically for amd64)
docker compose -f docker/docker-compose.yml up --build -d
```

The web portal will be accessible at `http://localhost:9527`. All databases, configurations, and project data are persisted in a Docker named volume (`ark_data`).

```bash
docker compose -f docker/docker-compose.yml logs -f webapp
```

**Configuration**

```bash
cp .ark/webapp.env.example .ark/webapp.env
# Edit .ark/webapp.env with your credentials
```

Then uncomment the environment volume mapping in `docker/docker-compose.yml` under the `webapp` service:

```yaml
      - ../.ark/webapp.env:/data/.ark/webapp.env:ro
```

**Running Individual Jobs**

Uncomment the `job` service in `docker/docker-compose.yml`, then run:

```bash
docker compose -f docker/docker-compose.yml run --rm job \
  --project myproject \
  --project-dir /data/projects/<user-id>/myproject \
  --mode research \
  --iterations 10
```

*Pass required API keys (e.g., `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) as environment variables.*

**Running Standalone Containers**

```bash
# Build images (force amd64)
docker build --platform linux/amd64 -f docker/Dockerfile.webapp -t ark-webapp .
docker build --platform linux/amd64 -f docker/Dockerfile.job -t ark-job .

# Run the Web Portal
docker run -d --name ark-webapp \
  --platform linux/amd64 \
  -p 9527:9527 \
  -v ark_data:/data \
  ark-webapp

# Run a Research Job
docker run --rm -it \
  --platform linux/amd64 \
  -v ark_data:/data \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  ark-job \
  --project myproject \
  --project-dir /data/projects/myproject \
  --mode research
```

**Pushing to GCP**

```bash
# Push to Artifact Registry (recommended)
./docker/push-gcp.sh --project [PROJECT_ID] --region [REGION] --repo [REPO] --build

# Push to Legacy Container Registry (gcr.io)
./docker/push-gcp.sh --project [PROJECT_ID] --legacy --build
```

The `--build` flag automatically builds the images for `linux/amd64` even when running on macOS.

</details>

---

## Cloud Compute

ARK's **v2 cloud architecture** decouples the *Control Plane* from the *Execution Plane*, enabling the full orchestrator to run on a remote cloud VM while you interact with a lightweight local webapp.

**How it works:**
1. The local webapp (or CLI) acts as a lightweight launcher — it provisions a remote **Orchestrator VM**, syncs your project code and API keys over SSH, and triggers the orchestrator process.
2. The **Orchestrator VM** runs all high-level logic (Researcher, Planner, Writer, LaTeX, figures) remotely in a detached session.
3. Experiments can run on the same orchestrator VM or on a separate GPU VM (configurable independently).
4. The webapp streams logs and syncs state back periodically. The orchestrator VM self-terminates when the run completes.

> [!TIP]
> Cloud credentials are encrypted at rest using your `SECRET_KEY`. Your keys are never logged or transmitted to third parties.

<details>
<summary><strong>Configuration Hierarchy</strong></summary>

ARK uses a three-tier configuration model for cloud compute:
1. **System Defaults**: Set in `webapp.env` (e.g., `CLOUD_REGION`, `CLOUD_NETWORK`).
2. **Global User Defaults**: Set in the **Settings** panel (⚙️). These apply to all your projects.
3. **Project Overrides**: Set during project creation or restart. These have the highest priority.

This hierarchy lets you define standard machine types and VPC settings once, while easily swapping to a powerful GPU instance for a specific experiment.

</details>

---

### Enabling Cloud Compute via the Dashboard

1. Open the **Settings** panel (⚙️ icon in the top navigation bar).
2. Scroll down to the **Cloud Compute** section.
3. Enter your credentials for your preferred provider (GCP, AWS, or Azure).
4. Click **Save**.

When creating a new project you can now independently select:
- **Orchestrator Backend** — `cloud` (GCP) to run the control plane remotely, or `local` to run it on the same machine as the webapp.
- **Experiment Backend** — `cloud` (GCP/AWS/Azure) for GPU experiments, or `local` to run them on the Orchestrator VM itself.

---

<details>
<summary><strong>Creating a Project</strong></summary>

Once cloud compute is configured, launch a project through the dashboard:

1. Click **New Project** from the dashboard home.
2. Fill in the research goal, target venue, and any additional instructions.
3. Click **Submit** — the webapp generates a `config.yaml`, provisions the Orchestrator VM, syncs your project, and starts the run.

The generated `config.yaml` is stored at:

```
~/.ark/data/projects/<user_id>/<project_id>/config.yaml
```

You can inspect or hand-edit this file at any time (e.g., to tune instance type or add `setup_commands`). Changes take effect on the next run or restart.

> [!NOTE]
> If `PROJECTS_ROOT` is set in your `.ark/webapp.env`, the path above is replaced by `$PROJECTS_ROOT/<user_id>/<project_id>/config.yaml`.

</details>

---

### Cloud Provider Setup

<details>
<summary><strong>☁️ Google Cloud Platform (GCP)</strong></summary>

#### 1. Create a Service Account

```bash
export PROJECT_ID=your-gcp-project-id

# Create a service account for ARK
gcloud iam service-accounts create ark-runner \
  --display-name="ARK Research Runner"

# Grant required roles (Compute Admin + Service Account User)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ark-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/compute.admin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ark-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Download the JSON key
gcloud iam service-accounts keys create ~/ark-gcp-key.json \
  --iam-account=ark-runner@${PROJECT_ID}.iam.gserviceaccount.com
```

#### 2. Enable Required APIs

```bash
gcloud services enable compute.googleapis.com --project=$PROJECT_ID
```

#### 3. Build the Machine Image

ARK uses a pre-baked Machine Image containing all system dependencies (Conda, LaTeX, Node.js) for fast boot times. Build it once:

```bash
./scripts/build_ark_gcp_image.sh [GCP_PROJECT_ID] [ZONE]
```

This script spins up a temporary VM, installs TeX Live, Miniforge, Node.js, and the `ark-base` environment, then saves a Machine Image named `ark-job-v1-[timestamp]` tagged with the `ark-job` family.

#### 4. Configure in Dashboard

Paste the contents of `~/ark-gcp-key.json` into the **GCP Service Account JSON** field and set your **GCP Project ID** in the Settings panel.

#### 5. Finding GCP Parameters (Optional)

```bash
# List available zones
gcloud compute zones list

# List available machine types in a specific zone
gcloud compute machine-types list --zones=us-central1-a

# List networks and subnets
gcloud compute networks list
gcloud compute networks subnets list --regions=us-central1
```

Alternatively, use the **Google Cloud Console**:
- **Zones/Machine Types**: Compute Engine &rarr; VM Instances &rarr; Create Instance
- **Networks**: VPC Network &rarr; VPC Networks
- **Images**: Compute Engine &rarr; Images

#### 6. `config.yaml` Reference (advanced / CLI only)

The webapp generates this automatically from your Settings. For manual or CLI-driven projects, see [`config.example.yaml`](config.example.yaml) for the full template. The model + keys (all agents run through OpenHands → LiteLLM):

```yaml
model: anthropic/claude-sonnet-4-6     # agents run this — any LiteLLM model
                                       # (gemini/… , openai/… , deepseek/… , …)
bot_model: anthropic/claude-haiku-4-5  # cheap model for light helpers (titles, summaries)
anthropic_api_key: "sk-ant-..."        # fill the provider(s) you use — the model
openai_api_key:    "sk-..."            #   prefix picks which key is used
gemini_api_key:    "..."               # gemini key also powers Deep Research (optional)
```

Compute uses two backends (orchestrator VM + experiment VM):

```yaml
# Orchestrator VM: runs Researcher, Planner, Writer, LaTeX (no GPU needed)
orchestrator_compute_backend:
  type: cloud
  provider: gcp
  region: us-central1-a
  instance_type: n1-standard-2
  image_family: ark-job              # Pre-baked ARK image

# Experiment VM: runs GPU-intensive workloads
experiment_compute_backend:
  type: cloud
  provider: gcp
  region: us-central1-a
  instance_type: g2-standard-4
  accelerator_type: nvidia-l4
  accelerator_count: 1
  # Optional: post-boot setup
  setup_commands:
    - conda activate base && pip install -r requirements.txt
```

> To run experiments on the Orchestrator VM instead of a separate instance, set `experiment_compute_backend.type: local`.

</details>

---

<details>
<summary><strong>☁️ Amazon Web Services (AWS)</strong></summary>

#### 1. Create an IAM User

```bash
# Create an IAM user for ARK
aws iam create-user --user-name ark-runner

# Attach policy (EC2 full access is sufficient)
aws iam attach-user-policy \
  --user-name ark-runner \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess

# Create access keys
aws iam create-access-key --user-name ark-runner
# Note the AccessKeyId and SecretAccessKey from the output
```

#### 2. Create an SSH Key Pair

```bash
# Create a key pair and save locally
aws ec2 create-key-pair \
  --key-name ark-key \
  --query 'KeyMaterial' \
  --output text > ~/.ssh/ark-key.pem
chmod 600 ~/.ssh/ark-key.pem
```

#### 3. Configure in Dashboard

Enter your **AWS Access Key ID**, **AWS Secret Access Key**, and **AWS Region** (e.g., `us-east-1`) in the Settings panel.

#### 4. `config.yaml` Reference (advanced / CLI only)

```yaml
experiment_compute_backend:
  type: cloud
  provider: aws
  region: us-east-1
  instance_type: g4dn.xlarge        # 1x T4 GPU, 4 vCPUs, 16 GB RAM
  image_id: ami-0c7c51e8edb7b66d3   # Deep Learning AMI (Ubuntu 22.04)
  ssh_key_name: ark-key              # Key pair name in AWS Console
  ssh_key_path: ~/.ssh/ark-key.pem
  ssh_user: ubuntu
  security_group: sg-xxxxxxxx        # Must allow inbound SSH (port 22)
  setup_commands:
    - conda activate pytorch && pip install -r requirements.txt
```

> [!IMPORTANT]
> Ensure your security group allows **inbound SSH (port 22)** from the IP of the machine running the webapp. Without this, ARK cannot connect to the provisioned instance.

</details>

---

<details>
<summary><strong>☁️ Microsoft Azure</strong></summary>

#### 1. Create a Service Principal

```bash
# Login
az login

# Create a service principal with Contributor role
az ad sp create-for-rbac \
  --name "ark-runner" \
  --role Contributor \
  --scopes /subscriptions/YOUR_SUBSCRIPTION_ID
# Note: appId (Client ID), password (Client Secret), and tenant (Tenant ID)
```

#### 2. Register the SSH Public Key

```bash
# Generate an SSH key if you don't have one
ssh-keygen -t rsa -b 4096 -f ~/.ssh/ark-azure-key

# The public key (~/.ssh/ark-azure-key.pub) will be used automatically
```

#### 3. Configure in Dashboard

Enter your **Azure Client ID**, **Azure Client Secret**, **Azure Tenant ID**, and **Azure Subscription ID** in the Settings panel.

#### 4. `config.yaml` Reference (advanced / CLI only)

```yaml
experiment_compute_backend:
  type: cloud
  provider: azure
  region: eastus                     # Azure location
  instance_type: Standard_NC6s_v3    # 1x V100 GPU, 6 vCPUs, 112 GB RAM
  image_id: UbuntuLTS                # OS image alias
  ssh_key_path: ~/.ssh/ark-azure-key
  ssh_user: azureuser
  resource_group: ark-resources      # Will be created if it doesn't exist
  setup_commands:
    - pip install -r requirements.txt
```

</details>

---

<details>
<summary><strong>Log Streaming &amp; Re-attachment</strong></summary>

- **Log Streaming** — the Orchestrator VM maintains a `logs/latest.log` symlink; the webapp polls it periodically to show live progress.
- **State Sync** — every 60 seconds, the launcher pulls the `auto_research/` state directory back from the VM to update the Dashboard UI.
- **Re-attachment** — if you restart your local webapp, ARK detects the existing `orchestrator_instance.yaml`, probes the remote VM via SSH, and re-attaches to the running process without re-provisioning.

</details>

<details>
<summary><strong>Cost Control</strong></summary>

> [!WARNING]
> Cloud VMs are billed by the hour. ARK includes several safeguards to prevent runaway costs:
>
> - **Launcher Heartbeat** — every time the webapp polls the VM for state, it touches a `launcher_heartbeat` file on the remote VM.
> - **VM Reaper** — a background daemon (`ark_vm_reaper.sh`) runs on the Orchestrator VM. If the orchestrator process finishes and the launcher heartbeat is stale for >30 minutes, the VM **automatically shuts itself down**.
> - **Manual Stop** — clicking **Stop** in the dashboard performs a final sync of all results and then terminates the GCP instance.
>
> If the webapp process is killed unexpectedly, the VM Reaper will self-terminate after the heartbeat timeout. Always verify no stray instances are running in your cloud console after unexpected shutdowns. Check `reaper.log` in the remote project directory if a VM shuts down unexpectedly.

</details>

---

<details>
<summary><strong>Telegram Integration</strong></summary>

```bash
ark setup-bot    # one-time: paste BotFather token, auto-detect chat ID
```

What you get:
- **Rich notifications** &mdash; formatted score changes, phase transitions, agent activity, and errors
- **Send instructions** &mdash; steer the current iteration in real time
- **Request PDFs** &mdash; latest compiled paper sent to chat
- **Human intervention** &mdash; agents escalate decisions to you before irreversible actions
- **HPC-friendly** &mdash; handles self-signed SSL certificates on enterprise/HPC networks

</details>

---

## Supported Venues

LaTeX templates ship for **NeurIPS, ICML, ICLR, ACL, EMNLP, CVPR, MLSys, EuroMLSys, INFOCOM, OSDI, SOSP**, with format detection for the **IEEE**, **ACMART (SIGPLAN)**, **LNCS**, and **USENIX** families. Custom templates are accepted &mdash; ARK scans `.tex` / `.aux` / `.sty` to learn the layout, fixes compile errors, and enforces the venue page limit.

## License

[Apache 2.0](LICENSE)
