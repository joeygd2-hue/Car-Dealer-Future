"""TOTP multi-factor authentication (RFC 6238, SHA-1, 6 digits, 30 s) using only the standard library.

Works with any authenticator app (Google Authenticator, 1Password, Authy…). Secrets are stored
Fernet-encrypted; recovery codes are stored as SHA-256 hashes and are single-use. A code counter is
remembered per user so the same code cannot be replayed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    return base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)


def hotp(secret_b32: str, counter: int) -> str:
    digest = hmac.new(_key(secret_b32), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(code).zfill(DIGITS)


def totp(secret_b32: str, at: float | None = None) -> str:
    return hotp(secret_b32, int((at if at is not None else time.time()) // STEP))


def verify(secret_b32: str, code: str, *, last_counter: int | None, at: float | None = None, window: int = 1) -> int | None:
    """Return the matched counter (to store as last used) or None. Rejects replays of used counters."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    now_counter = int((at if at is not None else time.time()) // STEP)
    for c in range(now_counter - window, now_counter + window + 1):
        if last_counter is not None and c <= last_counter:
            continue
        if hmac.compare_digest(hotp(secret_b32, c), code):
            return c
    return None


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret_b32}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"


def new_recovery_codes(n: int = 8) -> tuple[list[str], list[str]]:
    codes = [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(n)]
    return codes, [hashlib.sha256(c.encode()).hexdigest() for c in codes]


def use_recovery_code(stored_hashes: list[str], code: str) -> list[str] | None:
    """Return the remaining hashes if ``code`` matched (single use), else None."""
    h = hashlib.sha256((code or "").strip().lower().encode()).hexdigest()
    if h in (stored_hashes or []):
        return [x for x in stored_hashes if x != h]
    return None
