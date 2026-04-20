"""Load ARK webapp configuration from .ark/webapp.env."""

from __future__ import annotations

import os
import secrets
import json
from pathlib import Path

from ark.paths import get_ark_root, get_config_dir, get_primary_ip


def _env_file() -> Path:
    return get_config_dir() / "webapp.env"

_DEFAULTS = {
    "BASE_URL": f"http://{get_primary_ip()}:9527",
    "EMAIL_DOMAINS": "",
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASSWORD": "",
    "SMTP_RELAY": "",
    "SMTP_FROM": "ark@localhost",
    "PROJECTS_ROOT": "",  # resolved lazily
    "SECRET_KEY": secrets.token_hex(32),
    "DB_PATH": "",  # resolved lazily
    "SLURM_PARTITION": "",
    "SLURM_ACCOUNT": "",
    "SLURM_CONDA_ENV": "ark-base",
    # Base conda env that each new project's per-project env is cloned from.
    # ark-base is the project template env with runtime deps (numpy, pandas,
    # matplotlib, anthropic, etc.) but NOT the ARK code itself.
    # ark-dev and ark-prod are for developing/deploying the ARK webapp.
    # Set to empty to disable per-project env provisioning entirely.
    "PROJECT_BASE_CONDA_ENV": "ark-base",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    # Kubernetes compute plane (EKS MVP)
    "K8S_ENABLED": "false",          # "true" to activate; overrides local/slurm
    "K8S_NAMESPACE": "ark-jobs",     # namespace where Job objects are created
    "K8S_JOB_IMAGE": "",             # e.g. 123456789.dkr.ecr.us-east-1.amazonaws.com/ark-job:latest
    "K8S_PVC_NAME": "ark-data-pvc",  # EFS PVC that provides /data to both webapp and job pods
    "K8S_SERVICE_ACCOUNT": "",       # k8s ServiceAccount for IRSA (empty = no SA annotation)
    "K8S_CPU_REQUEST": "2",
    "K8S_CPU_LIMIT": "4",
    "K8S_MEMORY_REQUEST": "8Gi",
    "K8S_MEMORY_LIMIT": "16Gi",
    "K8S_GPU_COUNT": "0",            # >0 adds nvidia.com/gpu resource request
    "K8S_NODE_SELECTOR": "",         # JSON: {"node.kubernetes.io/lifecycle": "spot"}
    "K8S_TOLERATIONS": "",           # JSON array of toleration objects
    "K8S_AUTH_MODE": "auto",         # "auto" | "irsa" | "kubeconfig"
    # S3 transfer bridge (required when webapp and cluster are in different environments)
    "K8S_S3_BUCKET": "",             # bucket for project upload/results download
    "K8S_S3_PREFIX": "ark-jobs",     # key prefix inside the bucket
    "K8S_S3_REGION": "",             # override region if bucket is in a different region than the cluster
}


def _load_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    if _env_file().exists():
        for line in _env_file().read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_default_env():
    """Create .ark/webapp.env with placeholder values on first run."""
    ip = get_primary_ip()
    _root = get_ark_root()
    _config_dir = get_config_dir()
    content = f"""\
# ARK Web App configuration
# Edit this file, then restart: ark webapp

BASE_URL=http://{ip}:9527

# SMTP — required for magic link login
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_RELAY=
SMTP_FROM=ark@localhost

# Allowed emails (comma-separated). If set, only these addresses can log in.
ALLOWED_EMAILS=

# Or restrict by domain only (comma-separated, e.g. kaust.edu.sa). Ignored if ALLOWED_EMAILS is set.
EMAIL_DOMAINS=

# Admin emails (comma-separated). Admins can disable/enable the webapp and kill all jobs.
ADMIN_EMAILS=

# Google OAuth (optional). Get credentials at console.cloud.google.com → APIs & Services → Credentials.
# Redirect URI to register: <BASE_URL>/auth/google/callback  (use the BASE_URL value above)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

PROJECTS_ROOT={_root / '.ark' / 'data' / 'projects'}
SECRET_KEY={secrets.token_hex(32)}
DB_PATH={_root / '.ark' / 'data' / 'webapp.db'}

# Optional SLURM settings (auto-detected if blank)
SLURM_PARTITION=
SLURM_ACCOUNT=
SLURM_CONDA_ENV=ark-base

# Kubernetes compute plane
# K8S_ENABLED=false
# K8S_NAMESPACE=ark-jobs
# K8S_JOB_IMAGE=
# K8S_S3_BUCKET=
"""
    _env_file().write_text(content)
    print(f"Created config: {_env_file()}")
    print("Edit it to set SMTP credentials, then re-run `ark webapp`.")


class Settings:
    def __init__(self):
        file_env = _load_env_file()
        merged = {**_DEFAULTS, **file_env, **os.environ}

        self.base_url: str = merged.get("BASE_URL", _DEFAULTS["BASE_URL"]).rstrip("/")
        self.smtp_host: str = merged.get("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port: int = int(merged.get("SMTP_PORT", "587"))
        self.smtp_user: str = merged.get("SMTP_USER", "")
        self.smtp_password: str = merged.get("SMTP_PASSWORD", "")
        self.smtp_relay: str = merged.get("SMTP_RELAY", "")
        self.smtp_from: str = merged.get("SMTP_FROM", "ark@localhost")
        _root = get_ark_root()
        _config_dir = get_config_dir()
        self.projects_root: Path = Path(
            os.environ.get("PROJECTS_ROOT")
            or merged.get("PROJECTS_ROOT")
            or str(_root / ".ark" / "data" / "projects")
        )
        self.secret_key: str = merged.get("SECRET_KEY", _DEFAULTS["SECRET_KEY"])
        # ARK_WEBAPP_DB_PATH env var takes priority (used for dev/prod separation)
        self.db_path: str = (
            os.environ.get("ARK_WEBAPP_DB_PATH")
            or merged.get("DB_PATH")
            or str(_root / ".ark" / "data" / "webapp.db")
        )
        self.slurm_partition: str = merged.get("SLURM_PARTITION", "")
        self.slurm_account: str = merged.get("SLURM_ACCOUNT", "")
        self.slurm_conda_env: str = merged.get("SLURM_CONDA_ENV", "ark-base")
        self.project_base_conda_env: str = merged.get("PROJECT_BASE_CONDA_ENV", "ark-base")
        self.slurm_gres: str = merged.get("SLURM_GRES", "")
        self.slurm_cpus_per_task: int = int(merged.get("SLURM_CPUS_PER_TASK", "4"))
        raw_domains = merged.get("EMAIL_DOMAINS", "")
        self.email_domains: list[str] = [d.strip() for d in raw_domains.split(",") if d.strip()]
        raw_allowed = merged.get("ALLOWED_EMAILS", "")
        self.allowed_emails: list[str] = [e.strip().lower() for e in raw_allowed.split(",") if e.strip()]
        raw_admins = merged.get("ADMIN_EMAILS", "")
        self.admin_emails: list[str] = [e.strip().lower() for e in raw_admins.split(",") if e.strip()]

        self.google_client_id: str = merged.get("GOOGLE_CLIENT_ID", "")
        self.google_client_secret: str = merged.get("GOOGLE_CLIENT_SECRET", "")

        self.k8s_enabled: bool = merged.get("K8S_ENABLED", "false").lower() == "true"
        self.k8s_namespace: str = merged.get("K8S_NAMESPACE", "ark-jobs")
        self.k8s_job_image: str = merged.get("K8S_JOB_IMAGE", "")
        self.k8s_pvc_name: str = merged.get("K8S_PVC_NAME", "ark-data-pvc")
        self.k8s_service_account: str = merged.get("K8S_SERVICE_ACCOUNT", "")
        self.k8s_cpu_request: str = merged.get("K8S_CPU_REQUEST", "2")
        self.k8s_cpu_limit: str = merged.get("K8S_CPU_LIMIT", "4")
        self.k8s_memory_request: str = merged.get("K8S_MEMORY_REQUEST", "8Gi")
        self.k8s_memory_limit: str = merged.get("K8S_MEMORY_LIMIT", "16Gi")
        self.k8s_gpu_count: int = int(merged.get("K8S_GPU_COUNT", "0"))
        self.k8s_node_selector: dict = json.loads(merged.get("K8S_NODE_SELECTOR", "{}") or "{}")
        self.k8s_tolerations: list = json.loads(merged.get("K8S_TOLERATIONS", "[]") or "[]")
        self.k8s_auth_mode: str = merged.get("K8S_AUTH_MODE", "auto")
        self.k8s_s3_bucket: str = merged.get("K8S_S3_BUCKET", "")
        self.k8s_s3_prefix: str = merged.get("K8S_S3_PREFIX", "ark-jobs")
        self.k8s_s3_region: str = merged.get("K8S_S3_REGION", "")

        self.projects_root.mkdir(parents=True, exist_ok=True)


# Module-level singleton
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        if not _env_file().exists():
            _write_default_env()
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings
