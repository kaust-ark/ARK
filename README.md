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
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-6-orange.svg" alt="6 Agents">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="11+ Venues">
  <img src="https://img.shields.io/badge/tests-114-brightgreen.svg" alt="114 Tests">
</p>

<p align="center">
  <a href="https://idea2paper.org/"><strong>Website</strong></a> &bull;
  <a href="#quick-start">Quick Start</a> &bull;
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

<p align="center">
<img src="https://idea2paper.org/assets/paper-example.png" alt="MMA Paper" width="480">
<br>
<a href="https://github.com/JihaoXin/mma"><em>CPU Matrix Multiplication: From Naive to Efficient</em></a>
<br>
<sub>NeurIPS format &bull; 6 pages &bull; 14 iterations</sub>
</p>

---

## ARK Pipeline

ARK runs three phases in sequence. The Review phase loops until the paper reaches the target score.

<p align="center">
  <img src="https://idea2paper.org/assets/pipeline_overview.png" alt="ARK Pipeline" width="700">
</p>

| Phase | What Happens |
|:------|:-------------|
| **Research** | 5-step pipeline: Setup (conda env) &rarr; Analyze Proposal (researcher) &rarr; Deep Research (Gemini) &rarr; Specialization (researcher) &rarr; Bootstrap (skills &amp; citations) |
| **Dev** | Iterative experiment cycle: plan &rarr; run on Slurm &rarr; analyze &rarr; write initial draft |
| **Review** | Compile &rarr; Review &rarr; Plan &rarr; Execute &rarr; Validate, repeating until score &ge; threshold |

### Review Loop

Each iteration of the Review phase runs **5 steps**:

<p align="center">
  <img src="https://idea2paper.org/assets/review_loop.png" alt="Review Loop" width="700">
</p>

| Step | Description |
|:-----|:------------|
| **Compile** | LaTeX &rarr; PDF, page count, page images |
| **Review** | AI reviewer scores 1&ndash;10, lists Major &amp; Minor issues |
| **Plan** | Planner creates a prioritized action plan |
| **Execute** | Researcher + Experimenter run in parallel; Writer revises LaTeX |
| **Validate** | Verify changes compile; recompile PDF |

The loop repeats until the score reaches the acceptance threshold &mdash; or you intervene via Telegram.

---

## ARK Agents

<p align="center">
  <img src="https://idea2paper.org/assets/architecture_overview.png" alt="ARK Architecture" width="600">
</p>

| Agent | Role |
|:------|:-----|
| **Researcher** | Analyzes proposal &rarr; writes `idea.md`; Gemini-backed literature survey; specializes agent prompts for the project |
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
| **Formatting** | Broken layouts, LaTeX errors, manual cleanup | Hard-coded LaTeX + venue templates (NeurIPS, ACL, IEEE&hellip;) |
| **Citations** | LLMs fabricate plausible-looking references | Every citation verified against DBLP &mdash; no fake references |
| **Figures** | Default styles, wrong sizes, no page awareness | Nano Banana + venue-aware canvas, column widths, and fonts |
| **Isolation** | Shared env &mdash; projects interfere with each other | Per-project conda env, sandboxed HOME, full multi-tenant isolation |
| **Integrity** | LLMs simulate results instead of running real experiments | Anti-simulation prompts + builtin skills enforce real execution |

---

## Environment Isolation

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

## Skills System

ARK ships with **builtin skills** &mdash; modular instruction sets that agents load at runtime to enforce best practices:

| Skill | Purpose |
|:------|:--------|
| **research-integrity** | Anti-simulation prompts: agents must run real experiments, not fabricate outputs |
| **human-intervention** | Escalation protocol: agents pause and ask via Telegram before irreversible actions |
| **env-isolation** | Enforces per-project environment boundaries |
| **figure-integrity** | Validates figure content matches data; prevents placeholder or hallucinated plots |
| **page-adjustment** | Maintains page limits by adjusting content density, not deleting sections |

Skills live in `skills/builtin/` and are auto-installed during pipeline bootstrap.

---

## Quick Start

```bash
# Install
pip install -e .

# Create a project (interactive wizard)
ark new mma

# Run — ARK takes it from here
ark run mma

# Monitor in real time
ark monitor mma

# Check progress
ark status mma
```

The wizard walks you through: code directory, venue, research idea, authors, compute backend, figure generation, and Telegram setup.

### Start from an Existing PDF

```bash
ark new mma --from-pdf proposal.pdf
```

ARK parses the PDF with PyMuPDF + Claude Haiku, pre-fills the wizard, and kicks off from the extracted spec.

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
| `ark webapp install` | Install web dashboard service |
| `ark access list` | Show Dashboard Cloudflare Access allowlist |
| `ark access add <email>` | Add email(s) to CF Access allowlist |
| `ark access remove <email>` | Remove email(s) from CF Access allowlist |
| `ark access add-domain <domain>` | Add email domain rule to CF Access |
| `ark access remove-domain <domain>` | Remove email domain rule from CF Access |

---

## Dashboard

ARK includes a web-based dashboard for managing projects, viewing scores, and steering agents. The dashboard shows **live phase badges** (Research / Dev / Review), per-project conda env status, and real-time cost tracking. It is served from a single FastAPI process that also hosts the homepage &mdash; one port, one systemd unit.

### Configuration

The dashboard is configured via `webapp.env` located in your ARK config directory (default: `.ark/webapp.env` in the project root). This file is created automatically on the first run of `ark webapp`.

#### Authentication & Access
- **SMTP**: Required for "Magic Link" login. Set `SMTP_HOST`, `SMTP_USER`, and `SMTP_PASSWORD`.
- **Restrictions**: Use `ALLOWED_EMAILS` (specific users) or `EMAIL_DOMAINS` (entire organizations) to limit access.
- **Google OAuth**: Optional. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

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
|--|:-----|:----|
| **Port** | 9527 | 1027 |
| **Service Name** | `ark-webapp` | `ark-webapp-dev` |
| **Conda Env** | `ark-prod` | `ark-dev` |
| **Code Source** | `~/.ark/prod/` (pinned) | Current repository (live) |

</details>

<details>
<summary><strong>Direct orchestrator invocation</strong></summary>

```bash
python -m ark.orchestrator --project mma --mode paper --max-iterations 20
python -m ark.orchestrator --project mma --mode dev
```

</details>

---

## Docker Usage

### Architecture Requirements

> [!IMPORTANT]
> The ARK research runtime depends on scientific libraries that are most stable on x86_64. If you are building on an **Apple Silicon (M1/M2/M3)** Mac, you must build for the `linux/amd64` platform.
>
> All ARK Dockerfiles and the `docker-compose.yml` are configured to force `linux/amd64` by default.

### Running with Docker Compose

The easiest way to run the ARK Web Portal is using `docker-compose`. From the root of the project:

```bash
# Start the web portal (builds the image automatically for amd64)
docker compose -f docker/docker-compose.yml up --build -d
```

The web portal will be accessible at `http://localhost:9527`. All databases, configurations, and project data are persisted automatically in a Docker named volume (`ark_data`).

To view the live logs for the web portal:
```bash
docker compose -f docker/docker-compose.yml logs -f webapp
```

### Configuration

To customize the web portal configuration (e.g., setting up SMTP for magic-link logins or OAuth):

```bash
# Create a custom config
cp .ark/webapp.env.example .ark/webapp.env
# Edit .ark/webapp.env with your credentials
```
Then uncomment the environment volume mapping in `docker/docker-compose.yml` under the `webapp` service:
```yaml
      - ../.ark/webapp.env:/data/.ark/webapp.env:ro
```

### Running Individual Jobs

You can run isolated research jobs alongside the web app using the ARK job container. Uncomment the `job` service in `docker/docker-compose.yml`, then run:

```bash
docker compose -f docker/docker-compose.yml run --rm job \
  --project myproject \
  --project-dir /data/projects/<user-id>/myproject \
  --mode research \
  --iterations 10
```

*Note: You must pass your required API keys (e.g., `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) as environment variables.*

### Running Standalone Containers (Directly)

If you prefer to run the containers manually without Docker Compose:

#### 1. Build the Images (Force amd64)
```bash
# Build Web Portal
docker build --platform linux/amd64 -f docker/Dockerfile.webapp -t ark-webapp .

# Build Job Container
docker build --platform linux/amd64 -f docker/Dockerfile.job -t ark-job .
```

#### 2. Run the Web Portal
```bash
docker run -d --name ark-webapp \
  --platform linux/amd64 \
  -p 9527:9527 \
  -v ark_data:/data \
  ark-webapp
```

#### 3. Run a Research Job
```bash
docker run --rm -it \
  --platform linux/amd64 \
  -v ark_data:/data \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  ark-job \
  --project myproject \
  --project-dir /data/projects/myproject \
  --mode research
```

### Pushing to Google Cloud Platform (GCP)

ARK includes a script to build and push images to Google Artifact Registry or GCR.

```bash
# Push to Artifact Registry (recommended)
./docker/push-gcp.sh --project [PROJECT_ID] --region [REGION] --repo [REPO] --build

# Push to Legacy Container Registry (gcr.io)
./docker/push-gcp.sh --project [PROJECT_ID] --legacy --build
```
The `--build` flag automatically builds the images for `linux/amd64` even when running on macOS.

---

## Cloud Compute

ARK supports running experiments on remote cloud VMs (AWS, GCP, Azure) while keeping the orchestrator and web portal running **locally**. This is the recommended setup if you want elastic compute capacity without managing an HPC cluster.

**How it works:**
1. The webapp runs locally or on a small server, handling project management and the UI.
2. When a project is submitted, ARK provisions a cloud VM, transfers the project code over SSH, and manages the full experiment lifecycle remotely.
3. Results are synced back automatically. The VM is terminated when the run completes.

### Enabling Cloud Compute via the Dashboard

1. Open the **Settings** panel (⚙️ icon in the top navigation bar).
2. Scroll down to the **Cloud Compute** section.
3. Enter your credentials for your preferred provider (AWS, GCP, or Azure).
4. Click **Save**. All subsequent project submissions will automatically dispatch to the cloud.

> [!TIP]
> Cloud credentials are encrypted at rest using your `SECRET_KEY`. Your keys are never logged or transmitted to third parties.

---

### Creating a Project

Once cloud compute is configured, the recommended way to launch a project is through the dashboard:

1. Click **New Project** from the dashboard home.
2. Fill in the research goal, target venue, and any additional instructions.
3. Click **Submit** — the webapp automatically generates a `config.yaml` for the project and provisions the cloud VM.

The generated `config.yaml` is stored at:

```
~/.ark/data/projects/<user_id>/<project_id>/config.yaml
```

You can inspect or hand-edit this file at any time (e.g., to tune instance type or add `setup_commands`). Changes take effect on the next run or restart.

> [!NOTE]
> If `PROJECTS_ROOT` is set in your `.ark/webapp.env`, the path above is replaced by `$PROJECTS_ROOT/<user_id>/<project_id>/config.yaml`.

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

#### 3. Configure in Dashboard

Paste the contents of `~/ark-gcp-key.json` into the **GCP Service Account JSON** field and set your **GCP Project ID** in the Settings panel.

#### 4. `config.yaml` Reference (advanced / CLI only)

The webapp generates this automatically from your Settings. For manual or CLI-driven projects, add the following to your project's `config.yaml`:

```yaml
compute_backend:
  type: cloud
  provider: gcp
  region: us-central1-a          # zone, not region
  instance_type: n1-standard-8
  image_id: common-cu121          # Deep Learning VM image family
  ssh_key_path: ~/.ssh/id_rsa
  ssh_user: user
  # Optional: GPU accelerator
  accelerator_type: nvidia-tesla-t4
  accelerator_count: 1
  # Optional: run these commands on the instance after boot
  setup_commands:
    - conda activate base && pip install -r requirements.txt
```

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

The webapp generates this automatically from your Settings. For manual or CLI-driven projects, add the following to your project's `config.yaml`:

```yaml
compute_backend:
  type: cloud
  provider: aws
  region: us-east-1
  instance_type: g4dn.xlarge        # 1x T4 GPU, 4 vCPUs, 16 GB RAM
  image_id: ami-0c7c51e8edb7b66d3   # Deep Learning AMI (Ubuntu 22.04)
  ssh_key_name: ark-key              # Key pair name in AWS Console
  ssh_key_path: ~/.ssh/ark-key.pem
  ssh_user: ubuntu
  security_group: sg-xxxxxxxx        # Must allow inbound SSH (port 22)
  # Optional: post-boot setup
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

The webapp generates this automatically from your Settings. For manual or CLI-driven projects, add the following to your project's `config.yaml`:

```yaml
compute_backend:
  type: cloud
  provider: azure
  region: eastus                     # Azure location
  instance_type: Standard_NC6s_v3    # 1x V100 GPU, 6 vCPUs, 112 GB RAM
  image_id: UbuntuLTS                # OS image alias
  ssh_key_path: ~/.ssh/ark-azure-key
  ssh_user: azureuser
  resource_group: ark-resources      # Will be created if it doesn't exist
  # Optional: post-boot setup
  setup_commands:
    - pip install -r requirements.txt
```

</details>

---

### Cost Control

> [!WARNING]
> Cloud VMs are billed by the hour. ARK automatically terminates instances after each run completes. However, if the webapp process is killed unexpectedly, the **Orphan Rescue** mechanism will detect stale instances on the next restart and mark them as failed — but **will not terminate the cloud VM automatically**. Always verify no stray instances are running in your cloud console after unexpected shutdowns.

---

## Telegram Integration

```bash
ark setup-bot    # one-time: paste BotFather token, auto-detect chat ID
```

What you get:
- **Rich notifications** &mdash; formatted score changes, phase transitions, agent activity, and errors
- **Send instructions** &mdash; steer the current iteration in real time
- **Request PDFs** &mdash; latest compiled paper sent to chat
- **Human intervention** &mdash; agents escalate decisions to you before irreversible actions
- **HPC-friendly** &mdash; handles self-signed SSL certificates on enterprise/HPC networks

---

## Requirements

- **Python 3.9+** with `pyyaml` and `PyMuPDF`
- **Agent CLI**: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (recommended, Claude Max subscription) **or** [Gemini CLI](https://github.com/google-gemini/gemini-cli) &mdash; selectable per project
- Optional: LaTeX (`pdflatex` + `bibtex`), Slurm, `google-genai` for AI figures

```bash
# Set up the conda base environment
conda env create -f environment.yml         # Linux (creates "ark-base")
# OR for macOS:
conda env create -f environment-macos.yml   # macOS (creates "ark-base")

pip install -e .                    # Core
pip install -e ".[research]"       # + Gemini Deep Research & Nano Banana
```

## Supported Venues

NeurIPS &bull; ICML &bull; ICLR &bull; AAAI &bull; ACL &bull; IEEE &bull; ACM SIGPLAN &bull; ACM SIGCONF &bull; LNCS &bull; MLSys &bull; USENIX &mdash; plus aliases for PLDI, ASPLOS, SOSP, EuroSys, OSDI, NSDI, INFOCOM, and more.

## License

[Apache 2.0](LICENSE)

<p align="center">
  <sub>Built by <a href="https://sands.kaust.edu.sa/">SANDS Lab, KAUST</a></sub>
</p>
