"""Magic link authentication via itsdangerous."""

from __future__ import annotations

from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature


def make_token(email: str, secret: str) -> str:
    return URLSafeTimedSerializer(secret).dumps(email, salt="magic-link")


def verify_token(token: str, secret: str, max_age: int = 2592000) -> str | None:
    try:
        return URLSafeTimedSerializer(secret).loads(token, salt="magic-link", max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None
