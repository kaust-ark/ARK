"""Load ARK webapp configuration from ~/.ark_webapp.env."""

from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path

_ENV_FILE = Path.home() / ".ark_webapp.env"

_DEFAULTS = {
    "BASE_URL": f"http://{socket.gethostname()}:8422",
    "EMAIL_DOMAINS": "",
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASSWORD": "",
    "SMTP_RELAY": "",
    "SMTP_FROM": "ark@localhost",
    "PROJECTS_ROOT": str(Path.home() / "ark_web_projects"),
    "SECRET_KEY": secrets.token_hex(32),
    "DB_PATH": str(Path.home() / ".ark_webapp.db"),
    "SLURM_PARTITION": "",
    "SLURM_ACCOUNT": "",
    "SLURM_CONDA_ENV": "ark",
}


def _load_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_default_env():
    """Create ~/.ark_webapp.env with placeholder values on first run."""
    hostname = socket.gethostname()
    content = f"""\
# ARK Web App configuration
# Edit this file, then restart: ark webapp

BASE_URL=http://{hostname}:8422

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

PROJECTS_ROOT={Path.home() / 'ark_web_projects'}
SECRET_KEY={secrets.token_hex(32)}
DB_PATH={Path.home() / '.ark_webapp.db'}

# Optional SLURM settings (auto-detected if blank)
SLURM_PARTITION=
SLURM_ACCOUNT=
SLURM_CONDA_ENV=ark
"""
    _ENV_FILE.write_text(content)
    print(f"Created config: {_ENV_FILE}")
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
        self.projects_root: Path = Path(merged.get("PROJECTS_ROOT", _DEFAULTS["PROJECTS_ROOT"]))
        self.secret_key: str = merged.get("SECRET_KEY", _DEFAULTS["SECRET_KEY"])
        self.db_path: str = merged.get("DB_PATH", _DEFAULTS["DB_PATH"])
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

        self.projects_root.mkdir(parents=True, exist_ok=True)


# Module-level singleton
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        if not _ENV_FILE.exists():
            _write_default_env()
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings
