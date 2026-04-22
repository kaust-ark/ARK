"""Generate share links for webapp projects or user dashboards.

Share link shapes:
  ark share create <project_id_or_name>  → one project, read-only detail view
  ark share user <email>                 → auto-login as that user (full access)
  ark share alias list|delete            → manage short-URL aliases

Both ``create`` and ``user`` accept an optional ``--alias <slug>`` flag that
registers a short alias in the DB and prints a pretty URL like
``idea2paper.org/dashboard/share/icml`` alongside the long signed URL.

User-share is a shortcut for magic-link login — anyone with the URL signs in
as the referenced account with the same privileges a normal login would give.
Cap API spend at the provider level (OpenAI/Anthropic hard limit) before
handing the URL out.

Project-share grants read-only access to one project only, with writes
blocked both client- and server-side.

CF Access must have a Bypass policy for /dashboard/share/* so the request
reaches the webapp without Google SSO.

Signed tokens are signed with settings.secret_key and carry an embedded
expiry; rotating settings.secret_key invalidates all outstanding long URLs at
once. Aliases live in the DB and are revoked individually by deleting the row
(``ark share alias delete <name>``) or by waiting for their per-row expiry.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta

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


def _validate_alias(alias: str) -> str:
    """Normalize + validate a user-supplied alias slug."""
    from website.dashboard.db import ALIAS_PATTERN  # noqa: WPS433
    s = (alias or "").strip().lower()
    if not re.match(ALIAS_PATTERN, s):
        raise RuntimeError(
            f"Invalid alias {alias!r}. Use 2-64 chars: lowercase alphanumeric, "
            "dash, underscore; must start with alphanumeric."
        )
    return s


def _register_alias(alias: str, kind: str, ident: str, ttl_days: int) -> tuple[str, datetime]:
    """Upsert an alias row pointing at (kind, ident). Returns (alias, expires_at)."""
    from website.dashboard.config import get_settings  # noqa: WPS433
    from website.dashboard.db import get_session, upsert_share_alias  # noqa: WPS433

    alias = _validate_alias(alias)
    expires = datetime.utcnow() + timedelta(days=int(ttl_days))
    s = get_settings()
    with get_session(s.db_path) as sess:
        upsert_share_alias(sess, alias=alias, kind=kind, ident=ident,
                           expires_at=expires, created_by="cli")
    return alias, expires


def cmd_create(project_ref: str, ttl_days: int, alias: str | None = None) -> int:
    from website.dashboard.auth import make_share_token  # noqa: WPS433

    try:
        pid, label = _resolve_project(project_ref)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    token = make_share_token(pid, _load_secret_key(), ttl_days=ttl_days)
    long_url = f"{_load_base_url()}/share/{token}"

    alias_info = ""
    short_url = ""
    if alias:
        try:
            alias_name, _exp = _register_alias(alias, "project", pid, ttl_days)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        short_url = f"{_load_base_url()}/share/{alias_name}"
        alias_info = f"Alias:   {alias_name}"

    print(f"Kind:    project")
    print(f"Project: {label}  ({pid})")
    print(f"Expires: in {ttl_days} days")
    if alias_info:
        print(alias_info)
        print(f"Short:   {short_url}")
    print(f"URL:     {long_url}")
    print()
    print("Reviewer opens this URL and lands on the project's detail view (read-only).")
    if alias:
        print(f"Revoke the short link with: ark share alias delete {alias_name}")
    print("To revoke ALL signed share links, rotate SECRET_KEY in ~/.ark/webapp.env")
    print("and restart the webapp.")
    return 0


def cmd_user(email: str, ttl_days: int, alias: str | None = None) -> int:
    from website.dashboard.auth import make_user_share_token  # noqa: WPS433

    try:
        uid, display_email, is_new = _get_or_create_user(email)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    token = make_user_share_token(uid, _load_secret_key(), ttl_days=ttl_days)
    long_url = f"{_load_base_url()}/share/{token}"

    alias_info = ""
    short_url = ""
    if alias:
        try:
            alias_name, _exp = _register_alias(alias, "user", uid, ttl_days)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        short_url = f"{_load_base_url()}/share/{alias_name}"
        alias_info = f"Alias:   {alias_name}"

    print(f"Kind:    user")
    print(f"User:    {display_email}  ({uid}){'  [new account created]' if is_new else ''}")
    print(f"Expires: in {ttl_days} days")
    if alias_info:
        print(alias_info)
        print(f"Short:   {short_url}")
    print(f"URL:     {long_url}")
    print()
    print("Opening this URL LOGS THE VISITOR IN as the user above, with full")
    print("webapp access identical to a magic-link login. They can start, stop,")
    print("and delete projects under this account — cap API spend at the provider")
    print("level before sharing. Assign/reassign projects through the webapp or")
    print("DB to control what the shared account owns.")
    if alias:
        print(f"Revoke the short link with: ark share alias delete {alias_name}")
    print("To revoke ALL signed share links, rotate SECRET_KEY in ~/.ark/webapp.env")
    print("and restart the webapp.")
    return 0


def cmd_alias_list() -> int:
    from website.dashboard.config import get_settings  # noqa: WPS433
    from website.dashboard.db import get_session, list_share_aliases  # noqa: WPS433

    s = get_settings()
    with get_session(s.db_path) as sess:
        rows = list_share_aliases(sess)
    if not rows:
        print("No share aliases registered.")
        return 0
    base = _load_base_url()
    now = datetime.utcnow()
    print(f"{'ALIAS':<20} {'KIND':<8} {'IDENT':<40} {'EXPIRES':<20} URL")
    for r in rows:
        status = "EXPIRED" if r.expires_at <= now else r.expires_at.strftime("%Y-%m-%d %H:%M")
        print(f"{r.alias:<20} {r.kind:<8} {r.ident:<40} {status:<20} {base}/share/{r.alias}")
    return 0


def cmd_alias_delete(alias: str) -> int:
    from website.dashboard.config import get_settings  # noqa: WPS433
    from website.dashboard.db import get_session, delete_share_alias  # noqa: WPS433

    s = get_settings()
    with get_session(s.db_path) as sess:
        n = delete_share_alias(sess, alias.strip().lower())
    if n:
        print(f"Deleted alias {alias!r}.")
        return 0
    print(f"No alias named {alias!r}.", file=sys.stderr)
    return 1
