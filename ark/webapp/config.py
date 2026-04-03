"""Load ARK webapp configuration from .ark/webapp.env."""

from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path

from ark.paths import get_ark_root, get_config_dir


def _env_file() -> Path:
    return get_config_dir() / "webapp.env"

_DEFAULTS = {
    "BASE_URL": f"http://{socket.gethostname()}:8423",
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
    "SLURM_CONDA_ENV": "ark",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
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
    hostname = socket.gethostname()
    _root = get_ark_root()
    content = f"""\
# ARK Web App configuration
# Edit this file, then restart: ark webapp

BASE_URL=http://{hostname}:8423

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
# Redirect URI to register: {BASE_URL}/auth/google/callback
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

PROJECTS_ROOT={_root / 'ark_webapp' / 'projects'}
SECRET_KEY={secrets.token_hex(32)}
DB_PATH={_root / 'ark_webapp' / 'webapp.db'}

# Optional SLURM settings (auto-detected if blank)
SLURM_PARTITION=
SLURM_ACCOUNT=
SLURM_CONDA_ENV=ark
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
        self.projects_root: Path = Path(merged.get("PROJECTS_ROOT") or str(_root / "ark_webapp" / "projects"))
        self.secret_key: str = merged.get("SECRET_KEY", _DEFAULTS["SECRET_KEY"])
        # ARK_WEBAPP_DB_PATH env var takes priority (used for dev/prod separation)
        self.db_path: str = (
            os.environ.get("ARK_WEBAPP_DB_PATH")
            or merged.get("DB_PATH")
            or str(_root / "ark_webapp" / "webapp.db")
        )
        self.slurm_partition: str = merged.get("SLURM_PARTITION", "")
        self.slurm_account: str = merged.get("SLURM_ACCOUNT", "")
        self.slurm_conda_env: str = merged.get("SLURM_CONDA_ENV", "ark")
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
