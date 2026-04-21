#!/bin/sh
# =============================================================================
# ARK Webapp — Docker entrypoint
#
# Bootstraps the /data directory structure, creates a minimal webapp.env stub
# if one was not mounted, then launches uvicorn via `ark webapp`.
# =============================================================================
set -e

# ── 1. Ensure persistent data directories exist ───────────────────────────────
mkdir -p /data/.ark /data/projects

# ── 2. Create a minimal webapp.env stub if none was mounted ──────────────────
# The app can boot with an empty file; real values come from ENV variables
# passed to `docker run` (or docker-compose env_file / environment:).
WEBAPP_ENV=/data/.ark/webapp.env
if [ ! -f "$WEBAPP_ENV" ]; then
    cat > "$WEBAPP_ENV" <<'EOF'
# ARK webapp configuration
# Set these env vars via docker run --env or docker-compose environment:
# BASE_URL, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
# SECRET_KEY, ALLOWED_EMAILS, EMAIL_DOMAINS, ADMIN_EMAILS,
# GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET

# Defaults — override with environment variables
BASE_URL=http://localhost:9527
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
EOF
    echo "[entrypoint] Created minimal /data/.ark/webapp.env — set real values via env vars."
fi

# ── 3. Symlink /data/.ark into /app/.ark so ark.paths.get_config_dir() finds it ──
# ARK resolves its config dir as <ARK_ROOT>/.ark where ARK_ROOT = /app (the
# directory containing pyproject.toml). We symlink so that both the file-based
# config reader and the env-var overrides work transparently.
if [ ! -e /app/.ark ]; then
    ln -s /data/.ark /app/.ark
    echo "[entrypoint] Linked /app/.ark → /data/.ark"
fi

# ── 4. Launch the webapp ──────────────────────────────────────────────────────
echo "[entrypoint] Starting ARK webapp: ark webapp $*"
exec python -m ark.cli webapp "$@"
