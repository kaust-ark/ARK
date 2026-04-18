"""Magic link authentication + project share tokens via itsdangerous."""

from __future__ import annotations

import time

from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature


def make_token(email: str, secret: str) -> str:
    return URLSafeTimedSerializer(secret).dumps(email, salt="magic-link")


def verify_token(token: str, secret: str, max_age: int = 2592000) -> str | None:
    try:
        return URLSafeTimedSerializer(secret).loads(token, salt="magic-link", max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None


# Share tokens embed per-token expiry in the payload so each link can have its
# own lifetime without changing the global serializer max_age. Rotate
# settings.secret_key to invalidate ALL outstanding share tokens at once.
_SHARE_SALT = "project-share"
_SHARE_ABS_MAX_AGE = 86400 * 3650  # 10 years — sanity ceiling; real expiry is in payload


def make_share_token(project_id: str, secret: str, ttl_days: int = 90) -> str:
    payload = {"pid": project_id, "exp": int(time.time()) + int(ttl_days) * 86400}
    return URLSafeTimedSerializer(secret).dumps(payload, salt=_SHARE_SALT)


def verify_share_token(token: str, secret: str) -> str | None:
    """Return the project_id if the token is valid and not expired, else None."""
    try:
        data = URLSafeTimedSerializer(secret).loads(
            token, salt=_SHARE_SALT, max_age=_SHARE_ABS_MAX_AGE,
        )
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    exp = data.get("exp", 0)
    if not pid or not isinstance(exp, int) or exp < int(time.time()):
        return None
    return pid
