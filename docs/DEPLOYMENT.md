# Deploying ARK — Operator / Hosting Guide

This is the step-by-step runbook for **standing up a hosted ARK instance and
serving it to clients**. It covers everything the deployer does on their side:
the host machine, the web app, and the GCP / AWS setup that lets tenants run
cloud compute in *their own* accounts through your instance.

> **Audience:** operators / self-hosters. If you just want to *use* a hosted
> ARK instance (create projects, run them), see the [README](../README.md) —
> you only do the in-dashboard grant/verify covered in
> [§6 Onboarding a client tenant](#6-onboarding-a-client-tenant).

**Related design docs** (read for the *why*, not the *how*):
[`ARCHITECTURE.md`](../ARCHITECTURE.md) ·
[`SKYPILOT_PLAN.md`](../SKYPILOT_PLAN.md) (multi-tenancy §6, multi-cloud §6.1) ·
[`CONTROL_PLANE_BOUNDARY.md`](../CONTROL_PLANE_BOUNDARY.md) ·
[`ADRs/`](../ADRs/).

---

## Contents

1. [Architecture in one screen](#1-architecture-in-one-screen)
2. [Prerequisites](#2-prerequisites)
3. [Host setup](#3-host-setup)
4. [GCP provider setup (operator side)](#4-gcp-provider-setup-operator-side)
5. [AWS provider setup (operator side)](#5-aws-provider-setup-operator-side)
6. [Onboarding a client tenant](#6-onboarding-a-client-tenant)
7. [Operations](#7-operations)
8. [Reference](#8-reference)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Architecture in one screen

ARK separates a **control plane** (the web app you host) from an **execution
plane** (the compute that runs the research orchestrator and experiments):

- **Control plane** — the FastAPI web app (dashboard + homepage + `/v1` API).
  It authenticates users, stores project state, launches jobs, and streams
  logs. This is what you host and operate.
- **Execution plane** — where a run actually executes. Either **local**
  (same host as the web app), **SLURM** (an on-prem cluster), or **SkyPilot**
  (cloud VMs on GCP / AWS / Azure / Kubernetes). Cloud runs report home to the
  control plane over the `/v1` HTTP API, so the cluster needs no shared
  filesystem or database with your host.

**Multi-tenancy is key-less.** You never store a client's cloud credentials.
Instead you run **one central launcher identity**, and each tenant grants *that
identity* scoped access to *their own* cloud account via IAM:

| | GCP | AWS |
|---|---|---|
| Isolation boundary | per **project** | per **account** |
| Central identity | `ark-launcher` **service account** (in your project) | `ark-launcher` **IAM identity** (in your account) |
| How a tenant grants access | IAM role bindings on their project naming your SA | An `ark-launcher` **role** in their account whose trust policy names your identity |
| How ARK reaches the tenant | launches as the SA against `active_workspace = tenant_project` | **STS AssumeRole** into the tenant's role |
| Key material stored in ARK's DB | none | none |

See [`SKYPILOT_PLAN.md` §6–6.1](../SKYPILOT_PLAN.md) for the full design.

---

## 2. Prerequisites

**Host machine**
- A Linux VM/server you control (Debian/Ubuntu recommended). 4+ vCPU, 8+ GB RAM,
  30+ GB disk for a modest instance; more if you run *local* orchestrators on it.
- A domain + TLS if you're exposing it to remote clients (see [§3.6](#36-tls--reverse-proxy)).
- Outbound network to the LLM providers, GCP/AWS APIs, and SkyPilot.

**Accounts & CLIs**
- **GCP**: a project to hold the central launcher SA + the baked ARK image, with
  billing enabled. Install [`gcloud`](https://cloud.google.com/sdk/docs/install).
- **AWS** (optional, if you offer AWS to tenants): an account for the central
  launcher identity. Install the
  [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
- **SkyPilot** is installed as part of the web app env (below).

**Provider LLM keys** are supplied per-user (each tenant enters their own
Anthropic/OpenAI/Gemini keys in Settings); the operator does **not** need to
provision them centrally.

---

## 3. Host setup

### 3.1 Install ARK

The one-line installer does envs + OpenHands CLI + a `systemd --user` service:

```bash
curl -fsSL https://idea2paper.org/install.sh | bash
```

Or install by hand (see the README's *Manual Installation* for the annotated
version):

```bash
# 1. Project research-stack template env (kept clean; each project clones it)
conda env create -f environment.yml            # → "ark-base"

# 2. ARK itself in a SEPARATE env
conda create -n ark-prod python=3.11 -y
conda activate ark-prod
pip install -e ".[research,webapp]"            # research extras + dashboard/systemd

# 3. OpenHands CLI — the agent runtime (own bundled Python 3.12, must be on PATH)
pip install uv && uv tool install --python 3.12 openhands

# 4. Verify
ark doctor
```

> **Env layout.** ARK uses three conda envs by convention: `ark-base` (the
> clean per-project template that new projects clone), `ark-prod` (the deployed
> web app), and `ark-dev` (a dev instance). Keep `ark-base` free of ARK code.

### 3.2 Install the cloud provider CLIs

Install whichever provider(s) you'll offer tenants — both the setup scripts in
[§4](#4-gcp-provider-setup-operator-side)/[§5](#5-aws-provider-setup-operator-side)
and the onboarding **Verify** probe shell out to these CLIs.

```bash
# Google Cloud CLI (Debian/Ubuntu)
sudo apt-get update && sudo apt-get install -y apt-transport-https ca-certificates gnupg curl
curl https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
  | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt-get update && sudo apt-get install -y google-cloud-cli

# AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install && aws --version
```

### 3.3 Configure `.ark/webapp.env`

The first `ark webapp` run writes `.ark/webapp.env` with placeholders. Edit it,
then restart. The variables that matter for a hosted deployment:

```bash
# Public base URL — used in emails, magic links, and ${BASE_URL} expansion below.
BASE_URL=https://ark.example.org

# Login: magic-link email (SMTP) + optional Google OAuth.
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=contact@example.org
# GOOGLE_CLIENT_ID=...          # optional; redirect URI = <BASE_URL>/auth/google/callback
# GOOGLE_CLIENT_SECRET=...

# Access control (pick one or combine):
ALLOWED_EMAILS=                 # exact allowlist; if set, ONLY these can log in
EMAIL_DOMAINS=example.org       # or allow by domain (ignored if ALLOWED_EMAILS set)
ADMIN_EMAILS=you@example.org    # admins can disable submissions, kill all jobs,
                                # and publish the maintenance banner

# Storage
PROJECTS_ROOT=/srv/ark/projects
SECRET_KEY=<64-hex — generated on first write; also encrypts stored cloud creds>
DB_PATH=/srv/ark/webapp.db
```

See the [full variable table](#81-webappenv-variables) in §8. Set cloud
variables in [§4](#4-gcp-provider-setup-operator-side) /
[§5](#5-aws-provider-setup-operator-side).

### 3.4 Database: SQLite vs Postgres

- **Single-node / dev** → leave `DB_PATH` as a SQLite file. Fine for one web app
  process.
- **Concurrent remote orchestrators / a real control plane** → set a Postgres
  DSN in `DATABASE_URL` (it takes priority over `DB_PATH`):

  ```bash
  DATABASE_URL=postgresql+psycopg://ark:password@localhost:5432/ark_cp
  ```

  Alembic owns the schema for both backends (`_ensure_schema` runs on startup).

### 3.5 Control-plane URL (remote/cloud runs)

For **cloud/BYOC** runs, launched orchestrators must reach your `/v1` API over
HTTP. Set `CONTROL_PLANE_URL` to the `/v1` base — the `${BASE_URL}` idiom keeps
the host in one place:

```bash
CONTROL_PLANE_URL=${BASE_URL}/v1
```

Leave it blank only for the legacy single-host, shared-DB path. See
[`CONTROL_PLANE_BOUNDARY.md`](../CONTROL_PLANE_BOUNDARY.md).

### 3.6 TLS / reverse proxy

Terminate TLS at a reverse proxy (nginx/Caddy) in front of the web app on
`9527`, and forward to it. If you front it with Cloudflare, see the
[urllib User-Agent gotcha](#9-troubleshooting) — remote orchestrators' status
reports can be blocked at the edge (a 403, not an auth failure).

### 3.7 Run as a service

```bash
ark webapp install          # installs + starts a systemd --user unit (prod, port 9527)
ark webapp status
ark webapp logs -f
```

| Command | Description |
|:--|:--|
| `ark webapp` | Foreground (debugging). |
| `ark webapp install [--dev]` | Install/start as a `systemd --user` service. |
| `ark webapp release` | Tag current code + deploy to the prod worktree. |
| `ark webapp status` / `restart` / `logs [-f]` | Manage the service. |

| | Prod | Dev |
|---|:--|:--|
| Port | 9527 | 1027 |
| Service | `ark-webapp` | `ark-webapp-dev` |
| Conda env | `ark-prod` | `ark-dev` |
| Code source | `~/.ark/prod/` (pinned) | current repo (live) |

> **Only ONE process may run the control loop** (queue promotion, job polling,
> notify sweeps) per shared DB. A secondary UI-only instance (e.g. a dev app on
> the same DB) must set `ARK_CONTROL_LOOP=0`, or the two will double-launch
> pending projects.

### 3.8 Bootstrap admin credentials (to run the provider setup scripts)

The `setup_ark_launcher_*` scripts in [§4](#4-gcp-provider-setup-operator-side)/[§5](#5-aws-provider-setup-operator-side)
**create** the launcher identities, so whoever runs them needs cloud-**admin**
rights (create a service account / IAM user, grant roles, mint a key). This is a
**one-time** privilege used only at setup and later rotation — the running web
app never uses it. Neither script hardcodes a credential; each just uses whatever
the CLI currently resolves. So supply a **non-personal, short-lived** admin
identity rather than a named individual's password/keys.

**GCP.** The bootstrap identity needs, on the central project:
`resourcemanager.projects.setIamPolicy`, `iam.serviceAccounts.create`, and
`iam.serviceAccountKeys.create` (all in `roles/owner`, or the narrower
`roles/resourcemanager.projectIamAdmin` + `roles/iam.serviceAccountAdmin`).
Non-personal ways to hold it:

- **Cloud Shell / a bootstrap VM with an attached SA (simplest).** Create an
  `ark-bootstrap` SA with those roles, attach it to a Compute Engine VM (or open
  Cloud Shell as a project member), and run the script there — credentials come
  from the metadata server, nothing lands on a laptop.
  ```bash
  gcloud config set project $PROJECT_ID
  gcloud auth list        # confirm the ACTIVE identity is the SA, not a human user
  ```
- **SA impersonation (for laptop runs).** Grant a **group** (not a person)
  `roles/iam.serviceAccountTokenCreator` on `ark-bootstrap`, then have operators
  impersonate it — permissions live on the SA; who may use it is group
  membership, revocable without touching the SA:
  ```bash
  gcloud config set auth/impersonate_service_account \
    ark-bootstrap@$PROJECT_ID.iam.gserviceaccount.com
  ```
- **Workload Identity Federation** if you drive bootstrap from CI (e.g. GitHub
  Actions OIDC → impersonate `ark-bootstrap`) — fully keyless.

**AWS.** The bootstrap identity needs `iam:CreateUser`, `iam:CreateAccessKey`,
`iam:PutUserPolicy`, `iam:GetUser`, and `sts:GetCallerIdentity` (an admin /
PowerUser-plus-IAM permission set). Non-personal ways to hold it:

- **IAM Identity Center (SSO) permission set (recommended).** Create an
  `ARK-Bootstrap-Admin` permission set, assign it to a group, then run the setup
  script with that profile:
  ```bash
  aws configure sso                                       # one-time, per operator
  aws sso login --profile ark-bootstrap
  aws sts get-caller-identity --profile ark-bootstrap     # confirm before §5.3
  AWS_PROFILE=ark-bootstrap scripts/setup_ark_launcher_aws.sh
  ```
  Short-lived STS creds tied to a group. SSO is discouraged for the *launcher*
  (it can't self-refresh unattended — see [§5.2](#52-base-credentials-the-launcher-acts-as)),
  but the bootstrap run is interactive and one-off, so that caveat doesn't apply.
- **Assume a bootstrap role.** An `ark-bootstrap-admin` IAM role operators assume
  via a profile (`role_arn` + `source_profile`) or `aws sts assume-role` —
  permissions on the role, membership managed separately.
- **AWS CloudShell / an EC2 instance profile.** Run the script from CloudShell or
  a bootstrap EC2 host whose instance role carries the IAM permissions above — no
  keys anywhere.

> Whichever option you pick, the launcher scripts read the **ambient** CLI
> credential — so make sure `gcloud auth list` / `aws sts get-caller-identity`
> (or the right `--profile` / `AWS_PROFILE`) resolves to the bootstrap identity
> **before** running §4.2 / §5.3. These bootstrap creds are distinct from the
> long-lived *launcher* creds ([§5.2](#52-base-credentials-the-launcher-acts-as),
> [§7.6](#76-bootstrapping-a-new-host)) that the running app uses.

---

## 4. GCP provider setup (operator side)

GCP onboarding is key-less: SkyPilot boots clusters in each **tenant's** project
from a pre-baked ARK image, launching as your central `ark-launcher` service
account, which each tenant has granted access to their project.

### 4.1 Enable the Compute API (on your central project)

```bash
export PROJECT_ID=your-central-gcp-project
gcloud services enable compute.googleapis.com --project=$PROJECT_ID
```

Ensure SkyPilot's GCP provider is installed in the web app's env and can reach
the cloud:

```bash
pip install 'skypilot[gcp]'     # (use 'skypilot[gcp,aws,kubernetes]' for more clouds)
sky check                        # verify SkyPilot can reach your configured clouds
```

### 4.2 Create the central launcher service account

> Run this as a bootstrap **admin** identity (it creates an SA, grants roles, and
> mints a key) — see [§3.8](#38-bootstrap-admin-credentials-to-run-the-provider-setup-scripts)
> for non-personal options. Confirm `gcloud auth list` shows it before running.

```bash
scripts/setup_ark_launcher_sa.sh $PROJECT_ID
```

This creates the `ark-launcher@$PROJECT_ID.iam.gserviceaccount.com` service
account, mints a key at `~/.config/ark/ark-launcher-sa-key.json` (0600, outside
the repo), and activates it locally so `sky launch` and the host's SkyPilot API
server run **as the SA** rather than an expiring human login. Idempotent — it
won't mint a second key if one exists.

### 4.3 Bake the ARK machine image

Clusters boot from a pre-baked image (Conda, LaTeX/TeX Live, Node.js,
`ark-base`) for fast start-up. Build it once in the central project:

```bash
scripts/build_ark_gcp_image.sh $PROJECT_ID [ZONE] [VERSION]
# Custom-mode VPC (no default network)? pass NETWORK/SUBNET:
#   NETWORK=my-vpc SUBNET=my-subnet scripts/build_ark_gcp_image.sh $PROJECT_ID us-central1-a v1
```

It spins up a temporary VM, runs `setup_ark_host.sh`, and saves a Machine Image
tagged with the `ark-job` family. Optionally verify with
`scripts/check_gcp_image_env.sh`.

### 4.4 Wire it into `webapp.env`

```bash
CLOUD_GCP_PROJECT=your-central-gcp-project           # holds the baked image
CLOUD_LAUNCHER_SA=ark-launcher@your-central-gcp-project.iam.gserviceaccount.com
CLOUD_LAUNCHER_SA_KEY=~/.config/ark/ark-launcher-sa-key.json
CLOUD_CONDA_ENV=ark-base
# Only if tenant orgs enforce Domain Restricted Sharing (see below):
# CLOUD_LAUNCHER_ORG_CUSTOMER_ID=C0abc1234
```

Restart the web app. On startup it logs which identity SkyPilot will launch as
(`ensure_launcher_credentials`); watch for a warning that it would otherwise
launch as a user account.

> **Domain Restricted Sharing (DRS).** If a tenant's org enforces
> `constraints/iam.allowedPolicyMemberDomains`, cross-org members (your launcher
> SA) are rejected until the tenant allowlists your org. Set
> `CLOUD_LAUNCHER_ORG_CUSTOMER_ID` to your launcher SA org's directory customer
> id; the generated grant script then allowlists it before binding roles. Blank
> ⇒ the script emits a discovery helper instead.

Blank `CLOUD_GCP_PROJECT` ⇒ GCP SkyPilot runs are disabled.

---

## 5. AWS provider setup (operator side)

AWS onboarding is key-less too, but where GCP grants a cross-*project* role, AWS
grants a cross-*account* role: each tenant creates an `ark-launcher` **role** in
their account whose trust policy names your central launcher identity, which
then **assumes** it (STS AssumeRole) to boot clusters. No access key leaves the
tenant's account. There is **no baked AMI step** — AWS clusters boot a stock
image and run the launcher's `setup_commands` (slower first boot).

### 5.1 Install the AWS SDK + CLI (into the web app's env)

Install into the **same env the web app runs in** (e.g. `ark-prod`), so both the
launch path and the onboarding **Verify** probe can reach AWS:

```bash
pip install 'skypilot[aws]'      # SkyPilot's AWS provider (boto3 + botocore)
pip install 'botocore[crt]'      # AWS CRT signing — required by SkyPilot's AWS auth
```

Also install the AWS CLI (the setup script uses it).

### 5.2 Base credentials the launcher acts as

On an **unattended** host the launcher identity must use credentials that don't
require an interactive refresh. Two good options:

- **Static IAM-user keys (recommended).** You don't create these by hand — the
  `setup_ark_launcher_aws.sh` script in
  [§5.3](#53-create-the-central-launcher-identity) mints them into the
  `ark-launcher` profile (matching `CLOUD_LAUNCHER_AWS_PROFILE` below). They
  don't expire; rotate them on your own schedule.
- **EC2 instance role.** If the host runs on EC2, skip a profile entirely and set
  `CLOUD_LAUNCHER_AWS_CREDENTIAL_SOURCE=Ec2InstanceMetadata` — the instance
  metadata service refreshes credentials automatically. Attach the
  assume-tenant-roles permission (created in §5.3) to that instance role.

> **Do not use IAM Identity Center / SSO for the launcher on an unattended host.**
> `aws sso login` issues a short-lived session token, and boto3 will **not**
> re-run the browser login when it expires — cloud launches then fail every few
> hours until a human re-authenticates. SSO is fine only for an interactive dev
> box; the static keys / instance role above have no such expiry.

To *run* the setup script in §5.3 you need **admin** credentials able to create
IAM users. Don't tie this to a named user's password — see
[§3.8](#38-bootstrap-admin-credentials-to-run-the-provider-setup-scripts) for
non-personal options (an SSO permission set, an assumed role, or CloudShell).
These are used only to create the launcher identity; the running web app never
uses them.

```bash
aws sts get-caller-identity     # confirm the bootstrap identity resolves before §5.3
```

### 5.3 Create the central launcher identity

```bash
scripts/setup_ark_launcher_aws.sh             # uses the creds from 5.2
```

This creates the `ark-launcher` IAM identity, grants it `sts:AssumeRole` on every
tenant `ark-launcher` role, writes/reuses the base `~/.aws` profile, and prints
the ARN to put in `webapp.env`. Idempotent.

### 5.4 Wire it into `webapp.env`

```bash
CLOUD_LAUNCHER_ROLE_ARN=arn:aws:iam::<account>:user/ark-launcher
CLOUD_LAUNCHER_AWS_PROFILE=ark-launcher
# CLOUD_AWS_REGION=us-east-1                                  # default region
# CLOUD_LAUNCHER_AWS_CREDENTIAL_SOURCE=Ec2InstanceMetadata   # on EC2, use host role
# CLOUD_LAUNCHER_AWS_EXTERNAL_ID=...                         # optional confused-deputy protection
```

> **Existing installs:** the AWS block is only auto-written into *freshly
> created* `webapp.env` files. If yours predates AWS support, add these lines by
> hand. Until `CLOUD_LAUNCHER_ROLE_ARN` resolves, the dashboard shows
> *"(launcher identity not configured on server)"*.

Restart the web app. Blank `CLOUD_LAUNCHER_ROLE_ARN` (and no credential source)
⇒ AWS SkyPilot runs are disabled.

---

## 6. Onboarding a client tenant

This is what a tenant does in the dashboard once your host is configured — you
enable it; they self-serve. Know this flow so you can support them.

**GCP** — *Settings → Compute*:
1. Enter their **GCP Project ID**.
2. Run the shown `gcloud ... add-iam-policy-binding` commands on *their* project
   (grants your `ark-launcher` SA the required roles; the panel lists exact roles).
3. Click **Verify access** — a real probe that fails loudly if a binding is missing.

**AWS** — *Settings → Compute → AWS*:
1. Enter their 12-digit **AWS Account ID** and a **Region**.
2. Run the shown `aws iam create-role` + `attach-role-policy` script on *their*
   account (creates the `ark-launcher` role trusting your launcher identity, with
   SkyPilot's `AmazonEC2FullAccess` / `IAMFullAccess` / `AmazonS3FullAccess`).
3. Click **Verify access** — does a real `sts:AssumeRole`.

No key ever leaves the tenant's cloud. Per-user cloud settings are encrypted at
rest with your `SECRET_KEY`. Once verified, tenants pick an **Orchestrator
backend** (`skypilot`/`local`) and **Experiment backend** (`skypilot`/`local`)
per project.

---

## 7. Operations

### 7.1 Releases & upgrades

`ark webapp release` tags the current code, updates the pinned prod worktree
(`~/.ark/prod/`), installs into `ark-prod`, and the running app recycles itself
within ~30s when it notices the new `.deployed-tag` marker.

**Shared prod for a team** — one production instance from a group-writable
directory, with each member releasing from their own clone. Set in each
member's shell rc:

```bash
export ARK_RELEASE_ROOT=/shared/path/ARK    # prod worktree, DB, projects
export ARK_CONDA_ROOT=/shared/path/conda    # shared conda (ark-prod / ark-base)
export ARK_TOOLS_BIN=/shared/path/tools/bin # shared OpenHands CLI
umask 002                                   # keep new files group-writable
```

With these set, `ark webapp release` from **any** member's clone deploys the
shared instance; `ark webapp install` bakes the shared paths (and shared DB
under `$ARK_RELEASE_ROOT/.ark/data`) into the unit.

### 7.2 Admin controls

Admins (emails in `ADMIN_EMAILS`) get an **Admin Console** in the dashboard:

- **Maintenance banner** — publish a notice shown to all users (info/warning/
  critical). File-backed at `<ark_root>/ark_webapp/maintenance.json`; clearing
  removes the file. Endpoints: `GET`/`POST /api/admin/maintenance`.
- **Disable submissions** — a gate that blocks new project submissions
  (file flag `<ark_root>/ark_webapp/disabled`; `POST /api/admin/disable|enable`).
- **Stop all** — cancel every active job across all users (`POST /api/admin/killall`).

### 7.3 Concurrency & queueing

Two independent FIFO lanes so admins and regular users never block each other
(see `website/dashboard/routes.py`): a regular lane
(`MAX_CONCURRENT_PER_USER`, `MAX_CONCURRENT_REGULAR_GLOBAL`) and an admin lane
(`MAX_CONCURRENT_ADMIN_GLOBAL`). Pending projects promote as lanes free up.

### 7.4 Cost control (cloud runs)

Every SkyPilot cluster launches with an **autostop-down** window: idle past the
window ⇒ it terminates itself, even if your web app process died. Experiment
clusters always autostop-down (tunable, not disable-able); orchestrator clusters
default to a window as a crash safety-net (`idle_minutes_to_autostop`). After any
unexpected shutdown, verify no stray clusters remain: `sky status`.

### 7.5 Backups

Back up the DB (`DB_PATH` file or your Postgres) and `PROJECTS_ROOT`. The SA key
(`~/.config/ark/ark-launcher-sa-key.json`) and `~/.aws` profiles are host-local
credentials — protect them, and they can be regenerated by re-running the setup
scripts.

### 7.6 Bootstrapping a new host

The launcher identities themselves live in the cloud and are created once
([§4](#4-gcp-provider-setup-operator-side)/[§5](#5-aws-provider-setup-operator-side));
they are independent of any particular host. A new host only needs the local
credential artifacts that let it *act* as those identities:

- **GCP** — the SA key at `~/.config/ark/ark-launcher-sa-key.json`.
- **AWS** — the `ark-launcher` profile in `~/.aws/credentials` (or, on EC2, an
  instance role via `CLOUD_LAUNCHER_AWS_CREDENTIAL_SOURCE`).

Provision a new host either by placing those artifacts on it (preserve `0600`
permissions) or by re-running `setup_ark_launcher_sa.sh` /
`setup_ark_launcher_aws.sh` there. The scripts are idempotent — they skip
creating identities that already exist — but they mint a **fresh credential**
when none is present locally, so mind the provider key caps:

- A GCP service account allows up to **10 keys**, so re-running to mint an extra
  key is harmless. Prune or rotate old keys as needed.
- An AWS IAM user allows only **2 access keys**. Re-running on additional hosts
  fails once that limit is reached — prefer copying the profile, or delete a
  stale key before minting a new one.

---

## 8. Reference

### 8.1 `webapp.env` variables

| Variable | Purpose |
|:--|:--|
| `BASE_URL` | Public URL; used in emails, magic links, `${BASE_URL}` expansion. |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM/RELAY` | Magic-link login email. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Optional Google OAuth. |
| `ALLOWED_EMAILS` | Exact login allowlist (takes precedence over domains). |
| `EMAIL_DOMAINS` | Allow login by email domain. |
| `ADMIN_EMAILS` | Admins (disable submissions, kill-all, maintenance banner). |
| `PROJECTS_ROOT` | Where project working dirs live. |
| `SECRET_KEY` | Session signing **and** encryption of stored cloud creds. |
| `DB_PATH` | SQLite file path (single-node). |
| `DATABASE_URL` | Postgres DSN; overrides `DB_PATH` for a concurrent control plane. |
| `CONTROL_PLANE_URL` | `/v1` base for remote orchestrators (e.g. `${BASE_URL}/v1`). |
| `SLURM_PARTITION/ACCOUNT/CONDA_ENV/GRES/CPUS_PER_TASK` | Optional SLURM backend. |
| `PROJECT_BASE_CONDA_ENV` | Base env cloned per project (default `ark-base`). |
| `CLOUD_GCP_PROJECT` | Central project holding the baked image (blank ⇒ GCP off). |
| `CLOUD_LAUNCHER_SA` | Central launcher service-account email. |
| `CLOUD_LAUNCHER_SA_KEY` | Path to the SA key (ADC fallback). |
| `CLOUD_LAUNCHER_ORG_CUSTOMER_ID` | Launcher SA org id (for DRS allowlisting). |
| `CLOUD_CONDA_ENV` | Base env cloned per project on the remote. |
| `CLOUD_LAUNCHER_ROLE_ARN` | Central AWS launcher identity ARN (blank ⇒ AWS off). |
| `CLOUD_AWS_REGION` | Default AWS region for launches. |
| `CLOUD_LAUNCHER_AWS_PROFILE` | Base `~/.aws` profile the launcher acts as. |
| `CLOUD_LAUNCHER_AWS_CREDENTIAL_SOURCE` | Use host role (`Ec2InstanceMetadata`/`Environment`) instead of a profile. |
| `CLOUD_LAUNCHER_AWS_EXTERNAL_ID` | Optional STS ExternalId (confused-deputy protection). |

### 8.2 Setup scripts

| Script | Runs where | Does |
|:--|:--|:--|
| `scripts/setup_ark_host.sh` | a VM/host | Installs all system deps (TeX Live, Miniforge, Node) for bare-metal/VM runs. |
| `scripts/build_ark_gcp_image.sh` | operator | Bakes the GCP Machine Image (`ark-job` family). |
| `scripts/check_gcp_image_env.sh` | operator | Spins a VM to verify a baked image, then tears down. |
| `scripts/setup_ark_launcher_sa.sh` | operator | Creates + activates the central GCP launcher SA. |
| `scripts/setup_ark_launcher_aws.sh` | operator | Creates the central AWS launcher identity + assume-role grant. |

### 8.3 Ports & paths

- Web app: `9527` (prod), `1027` (dev). One process serves homepage + dashboard + `/v1`.
- Config: `.ark/webapp.env`. DB: `DB_PATH` / `DATABASE_URL`. Projects: `PROJECTS_ROOT`.
- Admin flags: `<ark_root>/ark_webapp/{disabled,maintenance.json}`.
- Launcher creds: `~/.config/ark/ark-launcher-sa-key.json` (GCP), `~/.aws` (AWS).

### 8.4 `config.yaml` (per-project, advanced / CLI)

The dashboard generates `config.yaml` from Settings. For manual/CLI projects see
[`config.example.yaml`](../config.example.yaml). Both cloud backends provision
via SkyPilot and share the same resource keys (`cloud`, `region`,
`accelerators`, `instance_type`, `use_spot`, `disk_size`, `image_id`,
`cluster_name`, `setup_commands`, `idle_minutes_to_autostop`):

```yaml
orchestrator_compute_backend:      # runs python -m ark.orchestrator on a cluster
  type: skypilot
  cloud: gcp                       # or aws; omit → SkyPilot auto-selects
  # idle_minutes_to_autostop: 60   # crash safety-net autostop
  setup_commands:
    - cd ~/sky_workdir && pip install -e '.[research]'

experiment_compute_backend:        # GPU experiments (or type: local to reuse the orchestrator)
  type: skypilot
  cloud: gcp
  accelerators: L4:1               # "<NAME>:<COUNT>"
  use_spot: true
```

A `skypilot` orchestrator cannot drive `slurm` experiments (no network path to
on-prem SLURM).

---

## 9. Troubleshooting

- **Web app launches jobs as a user account, not the SA (GCP).** SkyPilot uses
  ADC, not the gcloud active account. Ensure `CLOUD_LAUNCHER_SA_KEY` points at a
  valid key; the startup log (`ensure_launcher_credentials`) warns when it would
  launch as a user. Re-run `scripts/setup_ark_launcher_sa.sh`.
- **Tenant grant rejected on GCP.** Likely Domain Restricted Sharing — set
  `CLOUD_LAUNCHER_ORG_CUSTOMER_ID` (see [§4.4](#44-wire-it-into-webappenv)).
- **AWS shows "(launcher identity not configured on server)".**
  `CLOUD_LAUNCHER_ROLE_ARN` doesn't resolve — set it explicitly or ensure the
  profile is reachable; re-run `scripts/setup_ark_launcher_aws.sh`.
- **AWS Verify fails with an auth/signing error.** Missing `botocore[crt]`, or
  `skypilot[aws]` isn't in the web app's env — install both into the **same** env.
- **Remote orchestrators 403 on status reports (behind Cloudflare).** The 403 is
  Cloudflare's edge (error 1010) blocking urllib's default User-Agent — it is
  **not** an auth failure. Allow the orchestrator's requests at the edge (WAF
  rule / bypass) or front the `/v1` API with a path/host Cloudflare doesn't
  challenge.
- **Two instances double-launch pending projects.** Only one process may own the
  control loop per DB — set `ARK_CONTROL_LOOP=0` on every secondary (UI-only)
  instance.
- **Stray cloud clusters after a crash.** `sky status`; the autostop-down window
  reaps idle clusters on its own, but verify after unexpected shutdowns.
</content>
</invoke>
