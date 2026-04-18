"""Generate signed share links for webapp projects or user dashboards.

Share link shapes:
  ark share create <project_id_or_name>  → one project, read-only detail view
  ark share user <email>                 → auto-login as that user (full access)

User-share is a shortcut for magic-link login — anyone with the URL signs in
as the referenced account with the same privileges a normal login would give.
Cap API spend at the provider level (OpenAI/Anthropic hard limit) before
handing the URL out.

Project-share grants read-only access to one project only, with writes
blocked both client- and server-side.

CF Access must have a Bypass policy for /dashboard/share/* so the request
reaches the webapp without Google SSO.

Tokens are signed with settings.secret_key and carry an embedded expiry.
Rotate settings.secret_key to invalidate ALL outstanding links at once.
"""

from __future__ import annotations

import sys

from ark.paths import get_config_dir


def _load_secret_key() -> str:
    """Pull secret_key from the same source the running webapp uses so tokens
    generated on the CLI verify against the webapp's signer."""
    sys.path.insert(0, str(get_config_dir().parent.parent))  # ensure repo root
    from website.dashboard.config import get_settings  # noqa: WPS433
    return get_settings().secret_key


def _load_base_url() -> str:
    from website.dashboard.config import get_settings  # noqa: WPS433
    from website.dashboard.constants import DASHBOARD_PREFIX  # noqa: WPS433
    s = get_settings()
    return f"{s.base_url.rstrip('/')}{DASHBOARD_PREFIX}"


def _resolve_project(project_id_or_name: str) -> tuple[str, str]:
    from website.dashboard.config import get_settings  # noqa: WPS433
    from website.dashboard.db import get_session, get_project, get_all_projects  # noqa: WPS433

    s = get_settings()
    with get_session(s.db_path) as sess:
        p = get_project(sess, project_id_or_name)
        if p:
            return p.id, (p.title or p.name)
        for proj in get_all_projects(sess):
            if proj.name == project_id_or_name:
                return proj.id, (proj.title or proj.name)
    raise RuntimeError(f"No project found for {project_id_or_name!r}")


def _get_or_create_user(email: str) -> tuple[str, str, bool]:
    """Return (user_id, display_email, created). Creates the user if missing."""
    from website.dashboard.config import get_settings  # noqa: WPS433
    from website.dashboard.db import get_session, get_or_create_user_by_email  # noqa: WPS433

    email_norm = email.strip().lower()
    if "@" not in email_norm:
        raise RuntimeError(f"Not a valid email: {email!r}")
    s = get_settings()
    with get_session(s.db_path) as sess:
        user, is_new = get_or_create_user_by_email(sess, email_norm)
        return user.id, user.email, is_new


def cmd_create(project_ref: str, ttl_days: int) -> int:
    from website.dashboard.auth import make_share_token  # noqa: WPS433

    try:
        pid, label = _resolve_project(project_ref)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    token = make_share_token(pid, _load_secret_key(), ttl_days=ttl_days)
    url = f"{_load_base_url()}/share/{token}"
    print(f"Kind:    project")
    print(f"Project: {label}  ({pid})")
    print(f"Expires: in {ttl_days} days")
    print(f"URL:     {url}")
    print()
    print("Reviewer opens this URL and lands on the project's detail view (read-only).")
    print("To revoke ALL outstanding share links, rotate SECRET_KEY in ~/.ark/webapp.env")
    print("and restart the webapp.")
    return 0


def cmd_user(email: str, ttl_days: int) -> int:
    from website.dashboard.auth import make_user_share_token  # noqa: WPS433

    try:
        uid, display_email, is_new = _get_or_create_user(email)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    token = make_user_share_token(uid, _load_secret_key(), ttl_days=ttl_days)
    url = f"{_load_base_url()}/share/{token}"
    print(f"Kind:    user")
    print(f"User:    {display_email}  ({uid}){'  [new account created]' if is_new else ''}")
    print(f"Expires: in {ttl_days} days")
    print(f"URL:     {url}")
    print()
    print("Opening this URL LOGS THE VISITOR IN as the user above, with full")
    print("webapp access identical to a magic-link login. They can start, stop,")
    print("and delete projects under this account — cap API spend at the provider")
    print("level before sharing. Assign/reassign projects through the webapp or")
    print("DB to control what the shared account owns.")
    print("To revoke ALL outstanding share links, rotate SECRET_KEY in ~/.ark/webapp.env")
    print("and restart the webapp.")
    return 0
