"""Generate signed share links for webapp projects.

A share link grants public, read-only access to a single project through a
URL like https://idea2paper.org/dashboard/share/<token>. The CF Access app
for /dashboard must have a Bypass policy for /dashboard/share/* so reviewers
reach the webapp without signing in.

Tokens are signed with settings.secret_key and carry an embedded expiry.
Rotate settings.secret_key to invalidate ALL outstanding links at once.
"""

from __future__ import annotations

import sys

from ark.paths import get_config_dir


def _load_secret_key() -> str:
    """Pull secret_key from the same source the running webapp uses.

    We re-run the webapp settings loader so a link generated on the CLI will
    verify successfully against the webapp's signer.
    """
    sys.path.insert(0, str(get_config_dir().parent.parent))  # ensure repo root
    from website.dashboard.config import get_settings  # noqa: WPS433
    return get_settings().secret_key


def _load_base_url() -> str:
    from website.dashboard.config import get_settings  # noqa: WPS433
    from website.dashboard.constants import DASHBOARD_PREFIX  # noqa: WPS433
    s = get_settings()
    return f"{s.base_url.rstrip('/')}{DASHBOARD_PREFIX}"


def _resolve_project(project_id_or_name: str) -> tuple[str, str]:
    """Resolve user input to (project_id, project_title_or_name) from the webapp DB."""
    from website.dashboard.config import get_settings  # noqa: WPS433
    from website.dashboard.db import get_session, get_project, get_all_projects  # noqa: WPS433

    s = get_settings()
    with get_session(s.db_path) as sess:
        # Try exact ID match first.
        p = get_project(sess, project_id_or_name)
        if p:
            return p.id, (p.title or p.name)
        # Fall back to name match.
        for proj in get_all_projects(sess):
            if proj.name == project_id_or_name:
                return proj.id, (proj.title or proj.name)
    raise RuntimeError(f"No project found for {project_id_or_name!r}")


def cmd_create(project_ref: str, ttl_days: int) -> int:
    from website.dashboard.auth import make_share_token  # noqa: WPS433

    try:
        pid, label = _resolve_project(project_ref)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    token = make_share_token(pid, _load_secret_key(), ttl_days=ttl_days)
    url = f"{_load_base_url()}/share/{token}"
    print(f"Project: {label}  ({pid})")
    print(f"Expires: in {ttl_days} days")
    print(f"URL:     {url}")
    print()
    print("Reviewer opens this URL and lands on a read-only view of the project.")
    print("To revoke all outstanding share links, rotate SECRET_KEY in ~/.ark/webapp.env")
    print("and restart the webapp.")
    return 0
