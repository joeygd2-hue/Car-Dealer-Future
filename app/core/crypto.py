"""AES-256-GCM encryption for stored provider credentials. Secrets stay server-side."""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.errors import AppError


def _key() -> bytes:
    raw = base64.b64decode(os.environ.get("APP_ENCRYPTION_KEY", "") or b"")
    if len(raw) != 32:
        raise AppError("APP_ENCRYPTION_KEY must be a base64-encoded 32-byte key to store credentials",
                       status=500, code="encryption_not_configured")
    return raw


def encrypt_secret(plaintext: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode(), b"cdf-v1")
    return "v1:" + base64.b64encode(nonce + ct).decode()


def decrypt_secret(token: str) -> str:
    version, data = token.split(":", 1)
    if version != "v1":
        raise ValueError("unknown secret format")
    raw = base64.b64decode(data)
    return AESGCM(_key()).decrypt(raw[:12], raw[12:], b"cdf-v1").decode()


def generate_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()
