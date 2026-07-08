"""Render per-user SkyPilot *workspaces* into the host's ``~/.sky/config.yaml``.

Multi-tenant SkyPilot (see SKYPILOT_PLAN.md) isolates each user's compute in a
SkyPilot *workspace* that pins their GCP ``project_id``. One central "ark-launcher"
service account (scripts/setup_ark_launcher_sa.sh) provisions into every user's
project — the user grants that SA access via IAM, so no per-user key material ever
touches this DB. A launch selects the user's workspace per-call
(ark/compute/_sky.py::active_workspace), which routes it into their project.

Workspaces live in the API server's SkyPilot config file, and the (local) server
hot-reloads on change. This module is the single writer of the ARK-managed slice
of that file: it owns only the ``ws-*`` workspace entries and leaves every other
key (a hand-authored ``default:`` workspace, ``gcp.vpc_name``, …) untouched.

The user's GCP project id is stored (non-secret) in their encrypted keys blob
under ``gcp_project``; ``render_sky_workspaces`` reads it for every user and
rewrites the managed entries atomically.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("website.dashboard")

# All ARK-managed workspace names carry this prefix so a rewrite touches only our
# entries and never a hand-authored workspace (e.g. 'default').
_WS_PREFIX = "ws-"


def workspace_name_for(user_id: str) -> str:
    """Deterministic workspace name for a user. Stable across restarts so a
    re-render reuses (not duplicates) the entry, and so the launcher can derive
    the same name from the user id without a DB round-trip."""
    return f"{_WS_PREFIX}{user_id}"


def sky_config_path() -> Path:
    """The SkyPilot global config file this host reads. Honors SkyPilot's config
    env overrides (used in tests / non-default homes), else ``~/.sky/config.yaml``."""
    for env in ("SKYPILOT_GLOBAL_CONFIG", "SKYPILOT_CONFIG"):
        val = os.environ.get(env)
        if val:
            return Path(val).expanduser()
    return Path("~/.sky/config.yaml").expanduser()


def _user_gcp_project(user, get_user_keys) -> Optional[str]:
    """The user's configured GCP project id, or None. ``get_user_keys`` is
    injected (routes._get_user_keys) to avoid importing routes here."""
    try:
        keys = get_user_keys(user)
    except Exception:  # a single undecryptable blob must not sink the whole render
        return None
    proj = (keys or {}).get("gcp_project")
    return proj.strip() if isinstance(proj, str) and proj.strip() else None


def build_workspaces(users, get_user_keys) -> dict:
    """Build the ``ws-<id> -> {gcp: {project_id: ...}}`` map for every user that
    has a GCP project configured. Users without one are omitted (they fall back
    to the 'default' workspace / host credentials)."""
    workspaces: dict = {}
    for user in users:
        project = _user_gcp_project(user, get_user_keys)
        if project:
            workspaces[workspace_name_for(user.id)] = {"gcp": {"project_id": project}}
    return workspaces


def render_sky_workspaces(db_path: str, *, get_user_keys=None, list_users=None) -> int:
    """Re-render the ARK-managed ``ws-*`` workspaces into the SkyPilot config file
    from all users' GCP projects. Preserves every non-managed key. Returns the
    number of workspaces written. Best-effort: logs and returns 0 on failure so a
    settings-save or startup never breaks because the config file is unwritable.

    ``get_user_keys`` / ``list_users`` are injected (defaulting to the webapp's
    own) so this module doesn't import routes at module load (avoids a cycle)."""
    try:
        from website.dashboard.db import get_session
        if get_user_keys is None:
            from website.dashboard.routes import _get_user_keys as get_user_keys
        if list_users is None:
            from website.dashboard.db import list_users as list_users

        with get_session(db_path) as session:
            users = list_users(session)
            managed = build_workspaces(users, get_user_keys)

        path = sky_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing config (preserve everything we don't own).
        existing: dict = {}
        if path.exists():
            try:
                existing = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError as exc:
                logger.warning("sky config %s is unparseable, refusing to clobber: %s",
                               path, exc)
                return 0
            if not isinstance(existing, dict):
                logger.warning("sky config %s is not a mapping, refusing to clobber", path)
                return 0

        ws = dict(existing.get("workspaces") or {})
        # Drop stale ARK-managed entries (users who removed their project / left),
        # keep hand-authored ones (e.g. 'default'), then apply the fresh set.
        ws = {k: v for k, v in ws.items() if not k.startswith(_WS_PREFIX)}
        ws.update(managed)
        if ws:
            existing["workspaces"] = ws
        else:
            existing.pop("workspaces", None)

        # Atomic write — SkyPilot hot-reloads on change; a torn file would break
        # every subsequent launch.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w") as fh:
                yaml.safe_dump(existing, fh, default_flow_style=False, sort_keys=False)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

        logger.info("Rendered %d SkyPilot workspace(s) to %s", len(managed), path)
        return len(managed)
    except Exception as exc:  # never let a render failure break the caller
        logger.warning("Failed to render SkyPilot workspaces (non-fatal): %s", exc)
        return 0
