"""Shared constants for the ARK dashboard webapp."""

import os

# URL path prefix where the dashboard is mounted inside the outer FastAPI app.
# Used by routes.py for building URLs and by app.py for the mount call.
# Overridable via ARK_DASHBOARD_PREFIX (e.g. "/dev/dashboard" for the dev
# deployment behind the same Cloudflare Tunnel).
DASHBOARD_PREFIX = os.environ.get("ARK_DASHBOARD_PREFIX", "/dashboard").rstrip("/") or "/dashboard"
