"""Shared HTTP client with retries, backoff and client-side rate limiting."""

from __future__ import annotations

import random
import threading
import time
from typing import Any

import httpx

from ..config import get_settings
from .errors import CredentialError, IntegrationError, PermanentError, RateLimitedError


class RateLimiter:
    """Simple token bucket: ``rate`` requests per ``per`` seconds."""

    def __init__(self, rate: int, per: float):
        self.capacity = rate
        self.tokens = float(rate)
        self.per = per
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.capacity / self.per)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) * self.per / self.capacity
            time.sleep(wait)


_limiters: dict[str, RateLimiter] = {}


def limiter(name: str, rate: int, per: float) -> RateLimiter:
    if name not in _limiters:
        _limiters[name] = RateLimiter(rate, per)
    return _limiters[name]


def request(
    method: str,
    url: str,
    *,
    provider: str,
    client: httpx.Client | None = None,
    retries: int = 3,
    rate: tuple[int, float] = (10, 1.0),
    timeout: float = 20.0,
    **kwargs: Any,
) -> httpx.Response:
    """Make a request; retry on 429/5xx/network errors with jittered exponential backoff.

    Raises CredentialError on 401/403, RateLimitedError if 429 persists, PermanentError on other 4xx.
    """
    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("User-Agent", get_settings().http_user_agent)
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            limiter(provider, *rate).acquire()
            try:
                resp = client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
            else:
                if resp.status_code in (401, 403):
                    raise CredentialError(f"{provider}: HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code == 429:
                    retry_after = _retry_after(resp)
                    last_exc = RateLimitedError(f"{provider}: rate limited", retry_after)
                    if attempt < retries:
                        time.sleep(min(retry_after or 2**attempt, 30))
                        continue
                    raise last_exc
                if resp.status_code >= 500:
                    last_exc = IntegrationError(f"{provider}: HTTP {resp.status_code}: {resp.text[:300]}")
                elif resp.status_code >= 400:
                    raise PermanentError(f"{provider}: HTTP {resp.status_code}: {resp.text[:500]}")
                else:
                    return resp
            if attempt < retries:
                time.sleep(min(2**attempt + random.random(), 20))
        raise IntegrationError(f"{provider}: request failed after {retries + 1} attempts: {last_exc}")
    finally:
        if own:
            client.close()


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None
