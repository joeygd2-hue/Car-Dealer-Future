"""Server-side encryption for stored provider credentials (Fernet / AES-128-CBC + HMAC)."""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class CredentialEncryptionError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise CredentialEncryptionError(
            "ENCRYPTION_KEY is not set. Generate one with: "
            "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return Fernet(key.encode())


def encrypt_json(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_json(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as exc:  # wrong key or tampered value
        raise CredentialEncryptionError("Unable to decrypt stored credentials") from exc
