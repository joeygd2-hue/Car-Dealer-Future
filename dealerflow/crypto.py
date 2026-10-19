"""Server-side encryption for data at rest: MFA secrets and stored attachments (Fernet = AES-128-CBC + HMAC-SHA256).

Key: ``DF_ENCRYPTION_KEY`` (a Fernet key: ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``).
It is required in production. Outside production, when it is unset, a key is derived from ``DF_SECRET_KEY`` so
development and tests work; rotating ``DF_SECRET_KEY`` would then make encrypted values unreadable.

Encrypted files start with ``MAGIC`` so files written before encryption was introduced stay readable.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

MAGIC = b"DFENC1:"


class DecryptionError(RuntimeError):
    pass


def _fernet() -> Fernet:
    s = get_settings()
    key = s.encryption_key.get_secret_value().strip()
    if key:
        return Fernet(key.encode())
    if s.env == "production":
        raise DecryptionError("DF_ENCRYPTION_KEY is not set")
    derived = hashlib.sha256(b"dealerflow-encryption:" + s.secret_key.get_secret_value().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_text(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # wrong key or tampered value
        raise DecryptionError("unable to decrypt stored value") from exc


def encrypt_bytes(data: bytes) -> bytes:
    return MAGIC + _fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    if not data.startswith(MAGIC):
        return data  # written before at-rest encryption existed
    try:
        return _fernet().decrypt(data[len(MAGIC):])
    except InvalidToken as exc:
        raise DecryptionError("unable to decrypt stored file") from exc
