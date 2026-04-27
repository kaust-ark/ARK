"""Cloudflare Access policy management via REST API.

Reads/writes the include list on the "KAUST + invited" policy of the
"ARK Dashboard" application. Used by `ark access {list,add,remove,...}`.

Config: ~/.ark/cf-access.env with CF_API_TOKEN, CF_ACCOUNT_ID, CF_APP_ID,
CF_POLICY_ID. chmod 600 (contains an API token with Access:Edit scope).
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path


_API_BASE = "https://api.cloudflare.com/client/v4"


def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context using certifi or the system CA bundle.

    Conda-packaged Python often ships without a CA bundle, so
    ssl.create_default_context() fails to verify. Fall through in order
    of preference: certifi (pip-installed) -> system bundle -> default.
    """
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if Path(path).exists():
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def _load_config() -> dict:
    """Read CF creds from ~/.ark/cf-access.env."""
    cfg_path = Path.home() / ".ark" / "cf-access.env"
    if not cfg_path.exists():
        raise RuntimeError(
            f"Missing config at {cfg_path}.\n"
            f"Create it with:\n"
            f"  CF_API_TOKEN=cfut_...\n"
            f"  CF_ACCOUNT_ID=...\n"
            f"  CF_APP_ID=...\n"
            f"  CF_POLICY_ID=...\n"
            f"Then: chmod 600 {cfg_path}"
        )
    cfg = {}
    for line in cfg_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    required = ("CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_APP_ID", "CF_POLICY_ID")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"Config {cfg_path} missing: {', '.join(missing)}")
    return cfg


def _api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """Make a Cloudflare API call and return the parsed JSON result."""
    url = f"{_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # CF returns useful JSON even on 4xx
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            raise RuntimeError(f"CF API {method} {path} HTTP {e.code}: {body_text}") from e
    if not payload.get("success"):
        errs = payload.get("errors") or [{"message": "unknown error"}]
        raise RuntimeError(f"CF API error: {errs}")
    return payload["result"]


def _policy_path(cfg: dict) -> str:
    """Path for the reusable-policy endpoint (not the app-scoped one).

    Our policy is shared across apps (reusable=true), so it must be
    read/written via /accounts/{ACC}/access/policies/{POL} rather than
    the app-scoped /accounts/{ACC}/access/apps/{APP}/policies/{POL}.
    """
    return f"/accounts/{cfg['CF_ACCOUNT_ID']}/access/policies/{cfg['CF_POLICY_ID']}"


def _get_policy(cfg: dict) -> dict:
    return _api("GET", _policy_path(cfg), cfg["CF_API_TOKEN"])


def _put_policy(cfg: dict, policy: dict) -> dict:
    # CF PUT requires the full policy body. Strip read-only fields.
    body = {
        k: v
        for k, v in policy.items()
        if k
        in (
            "decision",
            "name",
            "include",
            "exclude",
            "require",
            "session_duration",
            "purpose_justification_required",
            "purpose_justification_prompt",
            "approval_required",
            "approval_groups",
            "isolation_required",
        )
        and v is not None
    }
    return _api("PUT", _policy_path(cfg), cfg["CF_API_TOKEN"], body)


def _extract_include_summary(include: list) -> tuple[list[str], list[str]]:
    """Return (emails, domains) from the include list."""
    emails, domains = [], []
    for rule in include:
        if "email" in rule:
            emails.append(rule["email"]["email"])
        elif "email_domain" in rule:
            domains.append(rule["email_domain"]["domain"])
    return emails, domains


def _has_email(include: list, email: str) -> bool:
    email = email.lower()
    return any(
        r.get("email", {}).get("email", "").lower() == email
        for r in include
        if "email" in r
    )


def _has_domain(include: list, domain: str) -> bool:
    domain = domain.lower().lstrip("@")
    return any(
        r.get("email_domain", {}).get("domain", "").lower() == domain
        for r in include
        if "email_domain" in r
    )


# ── public commands ──────────────────────────────────────────────────────────


def cmd_list() -> int:
    cfg = _load_config()
    pol = _get_policy(cfg)
    emails, domains = _extract_include_summary(pol.get("include", []))
    print(f"Policy: {pol['name']!r}  (app: ARK Dashboard)")
    print(f"Action: {pol['decision']}")
    print()
    print("Allowed email domains:")
    if domains:
        for d in sorted(domains):
            print(f"  @{d}")
    else:
        print("  (none)")
    print()
    print("Allowed emails:")
    if emails:
        for e in sorted(emails):
            print(f"  {e}")
    else:
        print("  (none)")
    return 0


def _notify_added(emails: list[str]) -> None:
    """Email each newly-added user that they've been granted access."""
    if not emails:
        return
    try:
        from website.dashboard.config import get_settings  # noqa: WPS433
        from website.dashboard.notify import send_access_granted_email  # noqa: WPS433
    except Exception as e:
        print(f"  (skipped notify: {e})", file=sys.stderr)
        return
    settings = get_settings()
    if not (settings.smtp_user and settings.smtp_password) and not getattr(settings, "smtp_relay", ""):
        print("  (skipped notify: SMTP not configured)", file=sys.stderr)
        return
    dashboard_url = f"{settings.base_url.rstrip('/')}/dashboard"
    for email in emails:
        ok = send_access_granted_email(settings, email, dashboard_url)
        print(f"  {'✉' if ok else '✗'} notify {email}")


def cmd_add(emails: list[str], notify: bool = True) -> int:
    cfg = _load_config()
    pol = _get_policy(cfg)
    include = pol.get("include", [])
    added, skipped = [], []
    for raw in emails:
        email = raw.strip().lower()
        if "@" not in email:
            print(f"  skip (not an email): {raw}", file=sys.stderr)
            skipped.append(raw)
            continue
        if _has_email(include, email):
            print(f"  already allowed: {email}")
            skipped.append(email)
            continue
        include.append({"email": {"email": email}})
        added.append(email)
    if not added:
        print("nothing to add.")
        return 0
    pol["include"] = include
    _put_policy(cfg, pol)
    for e in added:
        print(f"  + {e}")
    print(f"\nadded {len(added)}  skipped {len(skipped)}")
    if notify:
        _notify_added(added)
    return 0


def cmd_remove(emails: list[str]) -> int:
    cfg = _load_config()
    pol = _get_policy(cfg)
    include = pol.get("include", [])
    targets = {e.strip().lower() for e in emails}
    new_include = []
    removed = []
    for rule in include:
        email = rule.get("email", {}).get("email", "").lower()
        if email and email in targets:
            removed.append(email)
            continue
        new_include.append(rule)
    not_found = targets - set(removed)
    if not removed:
        print("nothing removed.")
        if not_found:
            for e in not_found:
                print(f"  not in allowlist: {e}")
        return 0
    pol["include"] = new_include
    _put_policy(cfg, pol)
    for e in removed:
        print(f"  - {e}")
    for e in not_found:
        print(f"  not found: {e}")
    print(f"\nremoved {len(removed)}")
    return 0


def cmd_add_domain(domains: list[str]) -> int:
    cfg = _load_config()
    pol = _get_policy(cfg)
    include = pol.get("include", [])
    added = []
    for raw in domains:
        d = raw.strip().lower().lstrip("@")
        if not d or "." not in d:
            print(f"  skip (not a domain): {raw}", file=sys.stderr)
            continue
        if _has_domain(include, d):
            print(f"  already allowed: @{d}")
            continue
        include.append({"email_domain": {"domain": d}})
        added.append(d)
    if not added:
        print("nothing to add.")
        return 0
    pol["include"] = include
    _put_policy(cfg, pol)
    for d in added:
        print(f"  + @{d}")
    return 0


def cmd_remove_domain(domains: list[str]) -> int:
    cfg = _load_config()
    pol = _get_policy(cfg)
    include = pol.get("include", [])
    targets = {d.strip().lower().lstrip("@") for d in domains}
    new_include = []
    removed = []
    for rule in include:
        domain = rule.get("email_domain", {}).get("domain", "").lower()
        if domain and domain in targets:
            removed.append(domain)
            continue
        new_include.append(rule)
    if not removed:
        print("nothing removed.")
        not_found = targets - set(removed)
        for d in not_found:
            print(f"  not in allowlist: @{d}")
        return 0
    pol["include"] = new_include
    _put_policy(cfg, pol)
    for d in removed:
        print(f"  - @{d}")
    return 0
