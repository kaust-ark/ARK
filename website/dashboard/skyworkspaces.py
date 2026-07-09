"""Render per-user SkyPilot *workspaces* into the host's ``~/.sky/config.yaml``.

Multi-tenant SkyPilot (see SKYPILOT_PLAN.md) isolates each user's compute in a
SkyPilot *workspace*. For GCP the workspace pins their ``project_id``; for AWS it
pins an ``aws.profile`` naming a per-user profile that assumes a role in the
user's account (there are no "projects" on AWS — isolation is per-account, via
cross-account STS AssumeRole). One central launcher identity provisions into every
user's cloud — the user grants access via IAM (GCP) / a trust policy (AWS), so no
per-user key material ever touches this DB. A launch selects the user's workspace
per-call (ark/compute/_sky.py::active_workspace), which routes it into their
project/account.

Workspaces live in the API server's SkyPilot config file, and the (local) server
hot-reloads on change. This module is the single writer of the ARK-managed slice
of that file: it owns only the ``ws-*`` workspace entries and leaves every other
key (a hand-authored ``default:`` workspace, ``gcp.vpc_name``, …) untouched. It is
likewise the single writer of the ARK-managed ``[profile ws-*]`` sections of
``~/.aws/config`` (``render_aws_profiles``), which back the AWS workspaces'
``profile`` references; every hand-authored profile (``[default]``, …) is preserved.

The user's GCP project id / AWS account id are stored (non-secret) in their
encrypted keys blob under ``gcp_project`` / ``aws_account_id``; the render
functions read them for every user and rewrite the managed entries atomically.
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


def _user_keys(user, get_user_keys) -> dict:
    """The user's decrypted keys blob, or ``{}``. ``get_user_keys`` is injected
    (routes._get_user_keys) to avoid importing routes here. A single undecryptable
    blob must not sink the whole render."""
    try:
        return get_user_keys(user) or {}
    except Exception:
        return {}


def _clean(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def build_workspaces(users, get_user_keys) -> dict:
    """Build the ``ws-<id> -> {gcp: {...}, aws: {...}}`` map for every user that
    has a GCP project and/or an AWS account configured. Each workspace carries a
    block per configured cloud (a user can have both); the launch's ``cloud`` field
    picks which applies. Users with neither are omitted (they fall back to the
    'default' workspace / host credentials). The AWS block pins an
    ``aws.profile`` (rendered into ~/.aws/config by ``render_aws_profiles``) whose
    name matches the workspace name."""
    workspaces: dict = {}
    for user in users:
        keys = _user_keys(user, get_user_keys)
        ws_name = workspace_name_for(user.id)
        entry: dict = {}
        project = _clean(keys.get("gcp_project"))
        if project:
            entry["gcp"] = {"project_id": project}
        if _clean(keys.get("aws_account_id")):
            entry["aws"] = {"profile": ws_name}
        if entry:
            workspaces[ws_name] = entry
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


# ── AWS per-user profiles ────────────────────────────────────────────────────
# An AWS SkyPilot workspace references a named ~/.aws profile (SkyPilot's only
# per-workspace AWS knob). We render one managed profile per user — same name as
# their workspace (``ws-<id>``) — that assumes the tenant's ``ark-launcher`` role
# via the central launcher's source creds. This is the AWS analog of pinning
# ``gcp.project_id`` in the SkyPilot config above.
_AWS_MANAGED_SECTION_PREFIX = f"profile {_WS_PREFIX}"  # "profile ws-"


def aws_config_path() -> Path:
    """The AWS config file the SkyPilot API server reads. Honors ``AWS_CONFIG_FILE``
    (used in tests / non-default homes), else ``~/.aws/config``."""
    val = os.environ.get("AWS_CONFIG_FILE")
    return Path(val).expanduser() if val else Path("~/.aws/config").expanduser()


def _aws_profile_body(account_id: str, region: str, settings) -> Optional[dict]:
    """The key/values for one managed ``[profile ws-<id>]`` section, or None if
    the account id is malformed (skip the user rather than emit a broken profile)."""
    from website.dashboard.aws_access import tenant_role_arn, launcher_external_id

    arn = tenant_role_arn(account_id)
    if not arn:
        return None
    body = {"role_arn": arn}
    reg = region or (getattr(settings, "cloud_aws_region", "") or "").strip()
    if reg:
        body["region"] = reg
    cred_source = (getattr(settings, "cloud_launcher_aws_credential_source", "") or "").strip()
    base_profile = (getattr(settings, "cloud_launcher_aws_profile", "") or "").strip()
    if cred_source:
        body["credential_source"] = cred_source
    elif base_profile:
        body["source_profile"] = base_profile
    ext = launcher_external_id(settings)
    if ext:
        body["external_id"] = ext
    return body


def render_aws_profiles(db_path: str, *, get_user_keys=None, list_users=None,
                        settings=None) -> int:
    """Re-render the ARK-managed ``[profile ws-*]`` sections into ``~/.aws/config``
    from all users' AWS accounts. Preserves every non-managed section
    (``[default]``, hand-authored profiles). Returns the number of profiles
    written. Best-effort: logs and returns 0 on failure so a settings-save or
    startup never breaks because the config file is unwritable — mirrors
    ``render_sky_workspaces``."""
    try:
        import configparser

        from website.dashboard.db import get_session
        if settings is None:
            from website.dashboard.config import get_settings
            settings = get_settings()
        if get_user_keys is None:
            from website.dashboard.routes import _get_user_keys as get_user_keys
        if list_users is None:
            from website.dashboard.db import list_users as list_users

        with get_session(db_path) as session:
            users = list_users(session)
            managed: dict = {}
            for user in users:
                keys = _user_keys(user, get_user_keys)
                account = _clean(keys.get("aws_account_id"))
                if not account:
                    continue
                body = _aws_profile_body(account, _clean(keys.get("aws_region")) or "",
                                         settings)
                if body:
                    managed[f"profile {workspace_name_for(user.id)}"] = body

        path = aws_config_path()

        # If there's nothing to manage and no file yet, don't create an empty one.
        if not managed and not path.exists():
            return 0

        path.parent.mkdir(parents=True, exist_ok=True)

        parser = configparser.ConfigParser()
        parser.optionxform = str  # preserve key case (don't rewrite hand-authored keys)
        if path.exists():
            try:
                parser.read(path)
            except configparser.Error as exc:
                logger.warning("aws config %s is unparseable, refusing to clobber: %s",
                               path, exc)
                return 0

        # Drop stale ARK-managed sections (users who removed their account / left),
        # keep every hand-authored section, then apply the fresh set.
        for section in [s for s in parser.sections()
                        if s.startswith(_AWS_MANAGED_SECTION_PREFIX)]:
            parser.remove_section(section)
        for section, body in managed.items():
            parser.add_section(section)
            for k, v in body.items():
                parser.set(section, k, v)

        # Atomic write — the SkyPilot server reads this live; a torn file would
        # break every subsequent AWS launch.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".ini")
        try:
            with os.fdopen(fd, "w") as fh:
                parser.write(fh)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

        logger.info("Rendered %d AWS profile(s) to %s", len(managed), path)
        return len(managed)
    except Exception as exc:  # never let a render failure break the caller
        logger.warning("Failed to render AWS profiles (non-fatal): %s", exc)
        return 0
