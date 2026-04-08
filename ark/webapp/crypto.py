"""Encryption utility for the ARK webapp using Fernet (AES-128)."""

import base64
import hashlib
from cryptography.fernet import Fernet
from typing import Optional

def _get_fernet() -> Fernet:
    """Derive a Fernet key from the application's SECRET_KEY."""
    from ark.webapp.config import get_settings
    settings = get_settings()
    # Fernet requires a 32-byte URL-safe base64-encoded key.
    # We use SHA256 of the SECRET_KEY to ensure we always have 32 bytes.
    key_bytes = hashlib.sha256(settings.secret_key.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)

def encrypt_text(plain_text: str) -> str:
    """Encrypt plain text to a URL-safe base64 string."""
    if not plain_text:
        return ""
    f = _get_fernet()
    return f.encrypt(plain_text.encode()).decode()

def decrypt_text(cipher_text: Optional[str]) -> str:
    """Decrypt a URL-safe base64 string to plain text."""
    if not cipher_text:
        return ""
    try:
        f = _get_fernet()
        return f.decrypt(cipher_text.encode()).decode()
    except Exception:
        # If decryption fails (e.g. key changed), return empty to avoid crashes
        return ""
