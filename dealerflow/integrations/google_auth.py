"""Google OAuth access tokens from a stored refresh token (installed-app / web OAuth client)."""

from __future__ import annotations

import threading
import time

from ..config import get_settings
from .errors import CredentialError, NotConfiguredError
from .http import request

TOKEN_URL = "https://oauth2.googleapis.com/token"
_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}


def access_token() -> str:
    s = get_settings()
    if not (s.google_client_id and s.google_client_secret.get_secret_value() and s.google_refresh_token.get_secret_value()):
        raise NotConfiguredError("Google OAuth is not configured (DF_GOOGLE_CLIENT_ID / _SECRET / _REFRESH_TOKEN)")
    with _lock:
        tok = _cache.get("google")
        if tok and tok[1] > time.time() + 60:
            return tok[0]
        try:
            resp = request("POST", TOKEN_URL, provider="google_oauth", retries=2, data={
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret.get_secret_value(),
                "refresh_token": s.google_refresh_token.get_secret_value(),
                "grant_type": "refresh_token",
            })
        except CredentialError:
            raise
        except Exception as exc:  # token endpoint returns 400 invalid_grant for revoked/expired tokens
            if "invalid_grant" in str(exc):
                raise CredentialError("Google refresh token expired or revoked; re-run scripts/google_oauth.py") from exc
            raise
        body = resp.json()
        _cache["google"] = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return body["access_token"]


def clear_cache() -> None:
    _cache.clear()
