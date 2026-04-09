"""Encryption utility for the ARK webapp using Fernet (AES-128)."""

import base64
import hashlib
from typing import Optional
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

def _get_fernet(user_id: str) -> Fernet:
    """Derive a user-specific Fernet key using PBKDF2HMAC."""
    from ark.webapp.config import get_settings
    settings = get_settings()
    
    # PBKDF2HMAC is a high-security key derivation function.
    # We use the user_id as the salt and the global SECRET_KEY as input.
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=user_id.encode(),
        iterations=100000,
    )
    key_bytes = kdf.derive(settings.secret_key.encode())
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)

def encrypt_text(plain_text: str, user_id: str) -> str:
    """Encrypt plain text to a URL-safe base64 string using a user-specific key."""
    if not plain_text or not user_id:
        return ""
    f = _get_fernet(user_id)
    return f.encrypt(plain_text.encode()).decode()

def decrypt_text(cipher_text: Optional[str], user_id: str) -> str:
    """Decrypt a URL-safe base64 string using a user-specific key."""
    if not cipher_text or not user_id:
        return ""
    try:
        f = _get_fernet(user_id)
        return f.decrypt(cipher_text.encode()).decode()
    except Exception:
        # Decryption failure is expected if the SECRET_KEY changed or 
        # (after this update) if trying to decrypt old globally-encrypted keys.
        return ""
